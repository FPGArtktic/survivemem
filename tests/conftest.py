# SPDX-License-Identifier: GPL-3.0-or-later
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from survivemem import db as dbmod


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Every test gets its own HOME, XDG dirs, DB, config and git configuration."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test\n\temail = test@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SURVIVEMEM_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("SURVIVEMEM_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    for var in ("SURVIVEMEM_PROJECT", "SURVIVEMEM_OLLAMA_URL", "PAGER"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = dbmod.connect(tmp_path / "memory.db")
    yield conn
    conn.close()
