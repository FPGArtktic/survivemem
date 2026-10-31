# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON payloads shared by the CLI (--json) and the MCP server, so both say the same thing."""

import sqlite3
from typing import Any

from survivemem import db as dbmod
from survivemem import identity, paths, store
from survivemem.config import Config
from survivemem.model import Checkout, ProjectSummary, iso


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
