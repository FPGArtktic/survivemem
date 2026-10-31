# SPDX-License-Identifier: GPL-3.0-or-later
"""Profile B daemon (§13): MCP over Streamable HTTP plus the hook endpoint; it alone owns the DB.

Every request except /health needs `Authorization: Bearer <token>`. The token lives next to the
DB with mode 0600: loopback is reachable by every local user, the DB file is not.
"""

import hmac
import json
import logging
import os
from typing import Any, cast

import anyio
import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from survivemem import db as dbmod
from survivemem import hooks, identity, logfile, mcp_server, version
from survivemem.auth import ensure_token, in_container, token_path

log = logging.getLogger(__name__)

DEFAULT_BIND = "127.0.0.1:8765"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
PUBLIC_PATHS = frozenset({"/health"})


def parse_bind(bind: str) -> tuple[str, int]:
    """host:port; anything but loopback is refused unless inside a container (where the
    host-side port publishing is the boundary) or SURVIVEMEM_ALLOW_REMOTE=1 (unsupported)."""
    host, sep, port = bind.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"--bind expects host:port, got {bind!r}")
    host = host.strip("[]")
    allowed = host in LOOPBACK_HOSTS or in_container()
    if not allowed and os.environ.get("SURVIVEMEM_ALLOW_REMOTE") != "1":
        raise ValueError(
            f"refusing to listen on {host}: loopback only "
            "(SURVIVEMEM_ALLOW_REMOTE=1 overrides, unsupported)"
        )
    return host, int(port)


async def health(request: Request) -> Response:
    return JSONResponse(
        {"version": version(), "schema_version": dbmod.SCHEMA_VERSION, "profile": "B"}
    )


async def hook(request: Request) -> Response:
    event = request.path_params["event"]
    handler = hooks.HANDLERS.get(event)
    if handler is None:
        return JSONResponse({"error": f"unknown hook event {event!r}"}, status_code=404)
    payload: Any = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse({"error": "payload must be an object"}, status_code=400)
    try:
        out = await anyio.to_thread.run_sync(handler, cast(hooks.HookInput, payload))
    except identity.NeedRoot:
        return JSONResponse({"need": "root"}, status_code=409)
    except Exception:  # request boundary: the client falls back to its own DB access
        log.exception("daemon hook %s failed", event)
        return JSONResponse({"error": "hook failed, see the daemon's hooks.log"}, status_code=500)
    return JSONResponse({"stdout": out})


class TokenGuard:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in PUBLIC_PATHS:
            sent = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(sent, self.expected):
                body = json.dumps({"error": "missing or wrong bearer token"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_app(token: str) -> ASGIApp:
    server = mcp_server.build_server()
    server.custom_route("/health", methods=["GET"])(health)
    server.custom_route("/hook/{event}", methods=["POST"])(hook)
    return TokenGuard(server.streamable_http_app(host="127.0.0.1"), token)


def serve_http(bind: str) -> None:
    host, port = parse_bind(bind)
    logfile.configure()
    os.environ["SURVIVEMEM_DAEMON"] = "1"  # MCP tools: no cwd repository here
    dbmod.connect().close()  # migrate once, before the first request
    app = build_app(ensure_token())
    log.info("survivemem %s daemon on %s:%d, token %s", version(), host, port, token_path())
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
