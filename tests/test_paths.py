# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from survivemem import paths


def test_xdg_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in (
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "SURVIVEMEM_DB",
        "SURVIVEMEM_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.db_path() == tmp_path / ".local/share/survivemem/memory.db"
    assert paths.config_path() == tmp_path / ".config/survivemem/config.toml"
    assert paths.log_path() == tmp_path / ".local/state/survivemem/hooks.log"


def test_xdg_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.delenv("SURVIVEMEM_DB", raising=False)
    assert paths.db_path() == tmp_path / "d/survivemem/memory.db"
    assert paths.log_path() == tmp_path / "s/survivemem/hooks.log"
    monkeypatch.setenv("SURVIVEMEM_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("SURVIVEMEM_CONFIG", str(tmp_path / "c.toml"))
    assert paths.db_path() == tmp_path / "x.db"
    assert paths.config_path() == tmp_path / "c.toml"
    assert paths.evidence_dir("01AB") == tmp_path / "evidence/01AB"


def test_relative_xdg_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    monkeypatch.delenv("SURVIVEMEM_DB", raising=False)
    assert paths.db_path() == tmp_path / ".local/share/survivemem/memory.db"
