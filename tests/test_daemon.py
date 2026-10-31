# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client  # the SDK builds its client here

from survivemem import auth, daemon, doctor, remote
from tests.helpers import clone, make_repo


def test_parse_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SURVIVEMEM_IN_CONTAINER", raising=False)
    monkeypatch.setattr(auth, "in_container", lambda: False)
    monkeypatch.setattr(daemon, "in_container", lambda: False)
    assert daemon.parse_bind("127.0.0.1:8765") == ("127.0.0.1", 8765)
    assert daemon.parse_bind("[::1]:9000") == ("::1", 9000)
    with pytest.raises(ValueError, match="loopback only"):
        daemon.parse_bind("0.0.0.0:8765")
    with pytest.raises(ValueError, match="host:port"):
        daemon.parse_bind("8765")
    monkeypatch.setenv("SURVIVEMEM_ALLOW_REMOTE", "1")
    assert daemon.parse_bind("0.0.0.0:8765") == ("0.0.0.0", 8765)
    monkeypatch.delenv("SURVIVEMEM_ALLOW_REMOTE")
    monkeypatch.setattr(daemon, "in_container", lambda: True)
    assert daemon.parse_bind("0.0.0.0:8765") == ("0.0.0.0", 8765)


def test_token_is_private_and_stable() -> None:
    token = auth.ensure_token()
    assert auth.ensure_token() == token == auth.read_token()
    assert stat.S_IMODE(auth.token_path().stat().st_mode) == 0o600


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server(tmp_path: Path) -> Iterator[dict[str, str]]:
    """A daemon process with its own DB; yields the client-side environment."""
    port = free_port()
    data = tmp_path / "daemon-data"
    env = dict(
        os.environ,
        SURVIVEMEM_DB=str(data / "memory.db"),
        XDG_STATE_HOME=str(tmp_path / "daemon-state"),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "survivemem", "serve", "--http", "--bind", f"127.0.0.1:{port}"],
        env=env,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=0.2)
            break
        except OSError:
            time.sleep(0.05)
    try:
        yield {
            "SURVIVEMEM_DAEMON_URL": url,
            "SURVIVEMEM_DAEMON_TOKEN_FILE": str(data / "daemon.token"),
            "SURVIVEMEM_DB": str(tmp_path / "client-never-used.db"),
        }
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def remote_hook(event: str, payload: dict[str, Any], env: dict[str, str]) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "survivemem", "hook", event, "--remote"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=dict(os.environ, **env),
        check=True,
    )
    return proc.stdout


def mcp_call(env: dict[str, str], name: str, args: dict[str, Any]) -> Any:
    token = Path(env["SURVIVEMEM_DAEMON_TOKEN_FILE"]).read_text().strip()

    async def go() -> Any:
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        async with (
            http,
            Client(
                streamable_http_client(f"{env['SURVIVEMEM_DAEMON_URL']}/mcp", http_client=http)
            ) as client,
        ):
            result = await client.call_tool(name, args)
            return result.structured_content

    return anyio.run(go)


def test_daemon_serves_mcp_and_hooks(server: dict[str, str], tmp_path: Path) -> None:
    health = json.loads(urllib.request.urlopen(f"{server['SURVIVEMEM_DAEMON_URL']}/health").read())
    assert health["profile"] == "B" and health["schema_version"] >= 3
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(
            urllib.request.Request(
                f"{server['SURVIVEMEM_DAEMON_URL']}/hook/prompt", data=b"{}", method="POST"
            )
        )
    assert denied.value.code == 401

    upstream = make_repo(tmp_path / "upstream", {"origin": "https://example.com/up/tool.git"})
    first = {"prompt": "hello", "cwd": str(upstream), "session_id": "s1"}
    assert remote_hook("prompt", first, server) == ""  # first contact: 409 → root sent
    stored = mcp_call(
        server, "store_memory", {"content": "uart console is ttyS0", "kind": "environment"}
    )
    assert stored["project_label"] == "upstream"  # the project the hooks saw last

    fork = clone(upstream, tmp_path / "fork", "https://example.com/me/tool.git")
    out = remote_hook(
        "prompt", {"prompt": "uart console", "cwd": str(fork), "session_id": "s2"}, server
    )
    assert "uart console is ttyS0" in out  # fork adopted by root commit, through the daemon
    assert not Path(server["SURVIVEMEM_DB"]).exists()  # the client never opened a DB


def test_remote_falls_back_to_local_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from survivemem import db as dbmod
    from survivemem import paths, store

    auth.ensure_token()
    monkeypatch.setenv("SURVIVEMEM_DAEMON_URL", f"http://127.0.0.1:{free_port()}")
    conn = dbmod.connect(paths.db_path())
    store.add_memory(conn, "local fallback fact about jtag", "fact")
    conn.close()
    out = io.StringIO()
    payload = json.dumps({"prompt": "jtag", "cwd": str(tmp_path)})
    assert remote.run("prompt", io.StringIO(payload), out) == 0
    assert "local fallback fact about jtag" in out.getvalue()
    assert "running locally" in paths.log_path().read_text()


def test_doctor_profile(server: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SURVIVEMEM_DAEMON_URL", server["SURVIVEMEM_DAEMON_URL"])
    check = doctor.check_profile()
    assert check.level == "ok" and check.detail.startswith("B (daemon)")
    monkeypatch.setenv("SURVIVEMEM_DAEMON_URL", f"http://127.0.0.1:{free_port()}")
    assert doctor.check_profile().level == "warn"
    monkeypatch.delenv("SURVIVEMEM_DAEMON_URL")
    assert doctor.check_profile().detail.startswith("A (native)")
