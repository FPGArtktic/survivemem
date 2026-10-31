# SPDX-License-Identifier: GPL-3.0-or-later
"""Profile B end to end with the real image. Opt-in: needs podman (or docker) and builds the
image when it is missing.  SURVIVEMEM_CONTAINER_TEST=1 uv run pytest tests/test_container.py"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import make_repo
from tests.test_daemon import free_port

IMAGE = "localhost/survivemem:latest"
ROOT = Path(__file__).resolve().parent.parent
RUNTIME = shutil.which("podman") or shutil.which("docker")
# captured at import, before conftest points HOME and XDG_* at a temporary directory: the
# container runtime needs the real ones to find its image store
HOST_ENV = dict(os.environ)

pytestmark = pytest.mark.skipif(
    os.environ.get("SURVIVEMEM_CONTAINER_TEST") != "1" or RUNTIME is None,
    reason="set SURVIVEMEM_CONTAINER_TEST=1 and install podman or docker",
)


def runtime(*args: str) -> str:
    assert RUNTIME is not None
    return subprocess.run(
        [RUNTIME, *args], capture_output=True, text=True, check=True, env=HOST_ENV
    ).stdout


@pytest.fixture(scope="module")
def image() -> str:
    if not runtime("images", "-q", IMAGE).strip():
        runtime("build", "-t", IMAGE, str(ROOT))
    return IMAGE


@pytest.fixture
def container(image: str, tmp_path: Path) -> Iterator[tuple[str, Path]]:
    data = tmp_path / "data"
    data.mkdir()
    port = free_port()
    name = f"survivemem-test-{port}"
    runtime(
        "run",
        "-d",
        "--name",
        name,
        "--userns=keep-id",
        "-v",
        f"{data}:/data",
        "-p",
        f"127.0.0.1:{port}:8765",
        image,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"{url}/health", timeout=0.5)
                break
            except OSError:
                time.sleep(0.25)
        yield url, data
    finally:
        runtime("rm", "-f", name)


def test_hooks_and_cli_against_the_container(container: tuple[str, Path], tmp_path: Path) -> None:
    url, data = container
    health = json.loads(urllib.request.urlopen(f"{url}/health").read())
    assert health["profile"] == "B"
    token = data / "daemon.token"
    assert token.exists() and token.stat().st_mode & 0o077 == 0
    repo = make_repo(tmp_path / "board", {"origin": "https://example.com/board.git"})
    env = dict(
        os.environ,
        SURVIVEMEM_DAEMON_URL=url,
        SURVIVEMEM_DAEMON_TOKEN_FILE=str(token),
        SURVIVEMEM_DB=str(tmp_path / "client-never-used.db"),
    )

    def hook(prompt: str) -> str:
        payload = json.dumps({"prompt": prompt, "cwd": str(repo), "session_id": "c1"})
        return subprocess.run(
            [sys.executable, "-m", "survivemem", "hook", "prompt", "--remote"],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout

    assert hook("hello") == ""
    # the host CLI reads and writes the same DB through the bind mount
    host = dict(os.environ, SURVIVEMEM_DB=str(data / "memory.db"))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "survivemem",
            "add",
            "the board's JTAG chain has two devices",
            "--project",
            "board",
            "--json",
        ],
        env=host,
        check=True,
        capture_output=True,
    )
    assert "JTAG chain has two devices" in hook("jtag chain")
    assert not (tmp_path / "client-never-used.db").exists()
