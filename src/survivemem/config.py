# SPDX-License-Identifier: GPL-3.0-or-later
"""config.toml loading. A missing file means defaults; an invalid value is an error."""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from survivemem import paths

DEFAULT_DEBUG_COMMANDS = (
    r"bitbake|devtool|make|dmesg|journalctl|openocd|gdb|quartus_sh|quartus_pgm|quartus_sta"
    r"|quartus_fit|quartus_map|jtagconfig|system-console|nios2-|niosv|bmaptool|dd\b"
)
DEFAULT_CAPTURE_TOOLS = ("mcp__jcodemunch__*", "mcp__rag__*", "mcp__jdocmunch__*")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

ROOT_MATCH_MODES = ("propose", "adopt")
LOOP_GUARD_MODES = ("ask", "warn", "deny")


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Config:
    identity_remote: str = "origin"
    root_match: str = "propose"
    hosts: Mapping[str, str] = field(default_factory=dict)
    family_root_match: Mapping[str, str] = field(default_factory=dict)
    loop_guard_mode: str = "ask"
    debug_commands: str = DEFAULT_DEBUG_COMMANDS
    capture_tools: tuple[str, ...] = DEFAULT_CAPTURE_TOOLS
    compaction_summarize: bool = False
    compaction_model: str = "llama3.2"
    embed_model: str = "bge-m3"
    share_enabled: bool = False
    ollama_url: str = DEFAULT_OLLAMA_URL


def _table(doc: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = doc.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _choice(value: Any, allowed: tuple[str, ...], key: str) -> str:
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {', '.join(allowed)}, got {value!r}")
    return str(value)


def _families(identity: Mapping[str, Any]) -> dict[str, str]:
    result = {}
    for label, table in _table(identity, "family").items():
        if not isinstance(table, dict):
            raise ConfigError(f"[identity.family.{label}] must be a table")
        key = f"identity.family.{label}.root_match"
        result[label] = _choice(table.get("root_match", "propose"), ROOT_MATCH_MODES, key)
    return result


def parse_config(doc: Mapping[str, Any]) -> Config:
    identity = _table(doc, "identity")
    guard = _table(doc, "loop_guard")
    capture = _table(doc, "capture")
    compaction = _table(doc, "compaction")
    embed = _table(doc, "embed")
    share = _table(doc, "share")
    defaults = Config()
    return Config(
        identity_remote=str(identity.get("remote", defaults.identity_remote)),
        root_match=_choice(
            identity.get("root_match", "propose"), ROOT_MATCH_MODES, "identity.root_match"
        ),
        hosts={str(k): str(v) for k, v in _table(identity, "hosts").items()},
        family_root_match=_families(identity),
        loop_guard_mode=_choice(guard.get("mode", "ask"), LOOP_GUARD_MODES, "loop_guard.mode"),
        debug_commands=str(capture.get("debug_commands", defaults.debug_commands)),
        capture_tools=tuple(str(t) for t in capture.get("tools", defaults.capture_tools)),
        compaction_summarize=bool(compaction.get("summarize", False)),
        compaction_model=str(compaction.get("model", defaults.compaction_model)),
        embed_model=str(embed.get("model", defaults.embed_model)),
        share_enabled=bool(share.get("enabled", False)),
        ollama_url=os.environ.get("SURVIVEMEM_OLLAMA_URL", DEFAULT_OLLAMA_URL),
    )


def load_config(path: Path | None = None) -> Config:
    path = path or paths.config_path()
    try:
        with path.open("rb") as f:
            doc = tomllib.load(f)
    except FileNotFoundError:
        doc = {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse_config(doc)
