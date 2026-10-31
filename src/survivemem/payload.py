# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON payloads shared by the CLI (--json) and the MCP server, so both say the same thing."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from survivemem import db as dbmod
from survivemem import identity, paths, store
from survivemem.config import Config
from survivemem.model import Checkout, ProjectSummary, iso, memory_json


def checkout_payload(c: Checkout, current_id: str | None) -> dict[str, Any]:
    return {
        "id": c.id,
        "path": c.path,
        "label": c.label,
        "branch": c.last_branch,
        "head": c.last_head,
        "config": c.last_config,
        "present": bool(c.present),
        "last_seen": c.last_seen,
        "last_seen_iso": iso(c.last_seen),
        "current": c.id == current_id,
    }


def _family(db: sqlite3.Connection, project_id: str) -> str | None:
    known = identity.families()
    for ident in store.identities_of(db, project_id):
        if ident.kind == "root" and ident.identity[5:] in known:
            return known[ident.identity[5:]]
    return None


def project_payload(
    db: sqlite3.Connection, s: ProjectSummary, here: identity.Resolution | None
) -> dict[str, Any]:
    current_checkout = here.checkout.id if here and here.checkout else None
    return {
        "id": s.project.id,
        "label": s.project.label,
        "created_at": s.project.created_at,
        "created_at_iso": iso(s.project.created_at),
        "primary_identity": s.primary_identity,
        "family": _family(db, s.project.id),
        "memories": s.memories,
        "candidates": s.candidates,
        "last_activity": s.last_activity,
        "last_activity_iso": iso(s.last_activity),
        "current": bool(here and here.project_id == s.project.id),
        "checkouts": [
            checkout_payload(c, current_checkout) for c in store.list_checkouts(db, s.project.id)
        ],
    }


def projects_payload(
    db: sqlite3.Connection, here: identity.Resolution | None
) -> list[dict[str, Any]]:
    return [project_payload(db, s, here) for s in store.project_summaries(db)]


def status_payload(db: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    from survivemem import embed  # keeps httpx out of the hook import path

    vec = embed.status(db)
    path = paths.db_path()
    counts = store.kind_counts(db)
    return {
        "db_path": str(path),
        "db_bytes": path.stat().st_size if path.exists() else 0,
        "schema_version": dbmod.schema_version(db),
        "counts": counts,
        "memories": sum(n for kind, n in counts.items() if kind != "candidate"),
        "candidates": counts.get("candidate", 0),
        "projects": len(store.project_labels(db)),
        "fts_ok": store.fts_ok(db),
        "vec": {
            "extension": vec.extension,
            "table": vec.table,
            "embedded": vec.embedded,
            "enabled": vec.extension and vec.table,
        },
        "ollama": {
            "url": cfg.ollama_url,
            "reachable": embed.ollama_reachable(cfg),
            "embed_model": cfg.embed_model,
        },
    }


HOOK_EVENTS = {
    "UserPromptSubmit": "prompt",
    "PreToolUse": "pretool",
    "PostToolUse": "posttool",
    "PostToolUseFailure": "posttool",
    "Stop": "stop",
    "PreCompact": "precompact",
    "SessionStart": "session-start",
}


def _json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _hook_commands(settings: dict[str, Any], event: str) -> list[str]:
    hooks = settings.get("hooks")
    groups = hooks.get(event, []) if isinstance(hooks, dict) else []
    return [
        str(h.get("command", ""))
        for g in groups
        if isinstance(g, dict)
        for h in g.get("hooks", [])
        if isinstance(h, dict)
    ]


def _mcp_registered(cwd: Path) -> bool:
    """User scope and per-project entries in ~/.claude.json, and the project's .mcp.json."""
    user = _json_file(Path.home() / ".claude.json")
    scopes = [user, _json_file(cwd / ".mcp.json")]
    projects = user.get("projects")
    if isinstance(projects, dict):
        scopes += [p for p in projects.values() if isinstance(p, dict)]
    return any(
        isinstance(s.get("mcpServers"), dict) and "survivemem" in s["mcpServers"] for s in scopes
    )


def integration_payload(cwd: Path) -> dict[str, Any]:
    """Which Claude Code settings files register survivemem hooks, and whether MCP is set up."""
    home = Path.home()
    files = [
        home / ".claude/settings.json",
        cwd / ".claude/settings.json",
        cwd / ".claude/settings.local.json",
    ]
    settings = [(f, _json_file(f)) for f in files]
    hooks = {
        event: any(
            f"survivemem hook {name}" in c for _, s in settings for c in _hook_commands(s, event)
        )
        for event, name in HOOK_EVENTS.items()
    }
    commands = [c for _, s in settings for event in HOOK_EVENTS for c in _hook_commands(s, event)]
    return {
        "hooks": hooks,
        "mcp": _mcp_registered(cwd),
        "remote_hooks": any("survivemem hook" in c and "--remote" in c for c in commands),
        "settings_files": [str(f) for f, s in settings if s],
    }


def brief_payload(
    db: sqlite3.Connection, here: identity.Resolution, cfg: Config, view: store.View
) -> dict[str, Any]:
    """The one call an AI client makes to orient itself (§10.4)."""
    recent = store.list_memories(db, view, limit=20)
    labels = store.project_labels(db)
    return {
        "projects": projects_payload(db, here),
        "current_project": here.project.label if here.project else None,
        "recent": [
            memory_json(m) | {"project_label": labels.get(m.project or "", "global")}
            for m in recent
        ],
        "candidates": len(store.list_memories(db, view, kind="candidate")),
        "status": status_payload(db, cfg),
    }
