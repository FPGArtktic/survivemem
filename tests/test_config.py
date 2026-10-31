# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from survivemem.config import Config, ConfigError, load_config


def test_missing_file_means_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "absent.toml") == Config()


def test_full_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[identity]
remote = "upstream"
root_match = "adopt"
[identity.hosts]
"gitlab.lan:12345" = "gitlab.lan"
[identity.family.u-boot]
root_match = "adopt"
[loop_guard]
mode = "deny"
[capture]
tools = ["mcp__rag__*"]
[compaction]
summarize = true
[share]
enabled = true
"""
    )
    cfg = load_config(path)
    assert cfg.identity_remote == "upstream" and cfg.root_match == "adopt"
    assert cfg.hosts == {"gitlab.lan:12345": "gitlab.lan"}
    assert cfg.family_root_match == {"u-boot": "adopt"}
    assert cfg.loop_guard_mode == "deny"
    assert cfg.capture_tools == ("mcp__rag__*",)
    assert cfg.compaction_summarize and cfg.share_enabled


@pytest.mark.parametrize(
    "text",
    [
        '[loop_guard]\nmode = "explode"\n',
        '[identity]\nroot_match = "x"\n',
        "identity = 3\n",
        "not toml [",
    ],
)
def test_invalid_config(tmp_path: Path, text: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_config(path)


def test_ollama_url_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SURVIVEMEM_OLLAMA_URL", "http://127.0.0.1:9999")
    assert load_config(tmp_path / "absent.toml").ollama_url == "http://127.0.0.1:9999"
