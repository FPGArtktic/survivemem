# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON Schema of the objects the CLI (--json) and the MCP server return (§10.4)."""

from typing import Any

from survivemem.db import KINDS, SOURCES
from survivemem.store import REF_TYPES

DRAFT = "https://json-schema.org/draft/2020-12/schema"  # an identifier, never fetched

Schema = dict[str, Any]


def _ts(nullable: bool = False) -> Schema:
    return {"type": ["integer", "null"] if nullable else "integer", "description": "unix ms"}


def _iso(nullable: bool = False) -> Schema:
    return {"type": ["string", "null"] if nullable else "string", "description": "ISO-8601 UTC"}


def _str(nullable: bool = False) -> Schema:
    return {"type": ["string", "null"] if nullable else "string"}


REF: Schema = {
    "type": "object",
    "required": ["type"],
    "properties": {"type": {"enum": list(REF_TYPES)}},
    "description": "pointer to the source of a memory, never its content (§15)",
}

MEMORY: Schema = {
    "type": "object",
    "required": [
        "id",
        "content",
        "kind",
        "project",
        "scope",
        "source",
        "refs",
        "anchor",
        "created_at",
        "created_at_iso",
        "updated_at",
        "updated_at_iso",
        "deleted_at",
        "deleted_at_iso",
        "tags",
    ],
    "properties": {
        "id": {"type": "string", "description": "ULID"},
        "content": _str(),
        "kind": {"enum": list(KINDS)},
        "project": _str(nullable=True),
        "project_label": _str(),
        "scope": {"type": "string", "pattern": "^(project|branch:.+|checkout:.+)$"},
        "source": {"enum": list(SOURCES)},
        "refs": {"type": ["array", "null"], "items": REF},
        "anchor": _str(nullable=True),
        "created_at": _ts(),
        "created_at_iso": _iso(),
        "updated_at": _ts(),
        "updated_at_iso": _iso(),
        "deleted_at": _ts(nullable=True),
        "deleted_at_iso": _iso(nullable=True),
        "tags": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "number", "description": "search only: RRF score, higher is better"},
        "matched_by": {"type": "array", "items": {"enum": ["fts", "trigram", "vec"]}},
        "bm25": {"type": "number"},
        "vec_distance": {"type": "number"},
    },
}

CHECKOUT: Schema = {
    "type": "object",
    "required": [
        "id",
        "path",
        "label",
        "branch",
        "head",
        "config",
        "present",
        "last_seen",
        "last_seen_iso",
        "current",
    ],
    "properties": {
        "id": _str(),
        "path": _str(),
        "label": _str(nullable=True),
        "branch": _str(nullable=True),
        "head": _str(nullable=True),
        "config": _str(nullable=True),
        "present": {"type": "boolean"},
        "last_seen": _ts(),
        "last_seen_iso": _iso(),
        "current": {"type": "boolean"},
    },
}

PROJECT: Schema = {
    "type": "object",
    "required": [
        "id",
        "label",
        "created_at",
        "created_at_iso",
        "primary_identity",
        "family",
        "memories",
        "candidates",
        "last_activity",
        "last_activity_iso",
        "current",
        "checkouts",
    ],
    "properties": {
        "id": _str(),
        "label": _str(),
        "created_at": _ts(),
        "created_at_iso": _iso(),
        "primary_identity": _str(nullable=True),
        "family": _str(nullable=True),
        "memories": {"type": "integer"},
        "candidates": {"type": "integer"},
        "last_activity": _ts(nullable=True),
        "last_activity_iso": _iso(nullable=True),
        "current": {"type": "boolean"},
        "checkouts": {"type": "array", "items": CHECKOUT},
        "identities": {"type": "array", "items": {"type": "object"}},
    },
}

_LIST: Schema = {"type": "array", "items": {"type": "string"}}

CHECKPOINT: Schema = {
    "type": "object",
    "description": "session_state.state (§16); every field optional",
    "properties": {
        "goal": _str(),
        "done": _LIST,
        "in_progress": _str(),
        "next": _LIST,
        "decisions": _LIST,
        "files": _LIST,
        "blockers": _LIST,
        "commands": _LIST,
        "recent_prompts": _LIST,
        "last_answer": _str(),
        "summary": _str(),
        "source": {"enum": ["model", "hook"]},
    },
}


def document() -> Schema:
    return {
        "$schema": DRAFT,
        "title": "survivemem objects",
        "$defs": {
            "memory": MEMORY,
            "project": PROJECT,
            "checkout": CHECKOUT,
            "checkpoint": CHECKPOINT,
            "ref": REF,
        },
    }
