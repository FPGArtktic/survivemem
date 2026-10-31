# SPDX-License-Identifier: GPL-3.0-or-later
"""`survivemem hook <event> --remote` (Profile B): forward the hook to the daemon.

The client adds what the daemon cannot see: the repository probe (git state, identities computed
with the user's SSH and identity config) and, for Stop/PreCompact, the compacted transcript.
When the daemon is unreachable, the hook runs locally against the DB as in Profile A.
"""

import http.client
import io
import json
import logging
import os
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from survivemem import auth, hooks, identity, logfile, transcript

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8765"
TIMEOUT = 4.0  # inside the 5 s hook timeout
TIMEOUTS = {"precompact": 28.0}
TRANSCRIPT_EVENTS = frozenset({"stop", "precompact"})


class Unavailable(Exception):
    pass


def daemon_url() -> str:
    return os.environ.get("SURVIVEMEM_DAEMON_URL", DEFAULT_URL).rstrip("/")


def run(event: str, stdin: TextIO, stdout: TextIO) -> int:
    logfile.configure()
    raw = stdin.read()
    try:
        out = forward(event, raw)
    except Exception:  # hook boundary: any trouble with the daemon means "run it locally"
        log.warning("daemon unavailable for hook %s, running locally", event, exc_info=True)
        return hooks.run(event, io.StringIO(raw), stdout)
    if out:
        stdout.write(out if out.endswith("\n") else out + "\n")
    return 0


def envelope(
    event: str, payload: dict[str, Any], observed: identity.Probe, with_root: bool
) -> dict[str, Any]:
    data = dict(payload)
    data[hooks.PROBE_KEY] = identity.probe_to_json(observed, with_root)
    path = payload.get("transcript_path")
    if event in TRANSCRIPT_EVENTS and isinstance(path, str) and path:
        data[hooks.TRANSCRIPT_KEY] = transcript.compact(transcript.tail(Path(path)))
    return data


def post(url: str, body: dict[str, Any], token: str, timeout: float) -> tuple[int, Any]:
    """One JSON POST over loopback. http.client, not httpx: it imports in a fraction of the time
    (this runs on every hook call) and never consults proxy settings."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(
        parts.hostname or "127.0.0.1", parts.port or 80, timeout=timeout
    )
    try:
        conn.request(
            "POST",
            parts.path,
            json.dumps(body, ensure_ascii=False).encode(),
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        data = response.read()
    finally:
        conn.close()
    return response.status, json.loads(data or b"{}")


def forward(event: str, raw: str) -> str:
    payload: Any = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        raise ValueError("hook payload is not an object")
    token = auth.read_token()
    if token is None:
        raise Unavailable(f"no daemon token at {auth.token_path()}")
    observed = identity.probe(Path(str(payload.get("cwd") or os.getcwd())), hooks.config())
    url = f"{daemon_url()}/hook/{event}"
    timeout = TIMEOUTS.get(event, TIMEOUT)
    status, answer = post(url, envelope(event, payload, observed, False), token, timeout)
    if status == 409:  # first contact: the daemon needs the root commit
        status, answer = post(url, envelope(event, payload, observed, True), token, timeout)
    if status != 200 or not isinstance(answer, dict):
        raise Unavailable(f"daemon answered {status}: {answer}")
    return str(answer.get("stdout") or "")


def health(timeout: float = 1.0) -> dict[str, Any]:
    """GET /health of the daemon (no token needed); raises OSError when unreachable."""
    parts = urlsplit(daemon_url())
    conn = http.client.HTTPConnection(
        parts.hostname or "127.0.0.1", parts.port or 80, timeout=timeout
    )
    try:
        conn.request("GET", "/health")
        data = conn.getresponse().read()
    finally:
        conn.close()
    found = json.loads(data or b"{}")
    return found if isinstance(found, dict) else {}
