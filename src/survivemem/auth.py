# SPDX-License-Identifier: GPL-3.0-or-later
"""Daemon token and container detection, kept light: hook clients import this on every call."""

import os
import secrets
from pathlib import Path

from survivemem import paths


def in_container() -> bool:
    return (
        os.environ.get("SURVIVEMEM_IN_CONTAINER") == "1"
        or Path("/run/.containerenv").exists()
        or Path("/.dockerenv").exists()
    )


def token_path() -> Path:
    override = os.environ.get("SURVIVEMEM_DAEMON_TOKEN_FILE")
    return Path(override) if override else paths.db_path().parent / "daemon.token"


def read_token() -> str | None:
    try:
        return token_path().read_text().strip() or None
    except OSError:
        return None


def ensure_token() -> str:
    found = read_token()
    if found:
        return found
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token
