# SPDX-License-Identifier: GPL-3.0-or-later
"""XDG path resolution (Linux only)."""

import os
from pathlib import Path

APP = "survivemem"


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / default


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def db_path() -> Path:
    override = os.environ.get("SURVIVEMEM_DB")
    if override:
        return Path(override).expanduser()
    return data_dir() / "memory.db"


def config_path() -> Path:
    override = os.environ.get("SURVIVEMEM_CONFIG")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / APP / "config.toml"


def log_path() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP / "hooks.log"


def evidence_dir(inv_id: str) -> Path:
    # Next to the DB, so SURVIVEMEM_DB relocates evidence together with the data it points to.
    return db_path().parent / "evidence" / inv_id
