# SPDX-License-Identifier: GPL-3.0-or-later
"""Row types shared by store, CLI and MCP server, and their JSON form (§10.4)."""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

TIMESTAMP_FIELDS = frozenset(
    {"created_at", "updated_at", "deleted_at", "added_at", "last_seen", "closed_at"}
)


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    content: str
    kind: str
    project: str | None
    scope: str
    source: str
    refs: str | None
    anchor: str | None
    created_at: int
    updated_at: int
    deleted_at: int | None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Hit:
    memory: Memory
    score: float
    matched_by: tuple[str, ...]
    bm25: float | None = None
    vec_distance: float | None = None


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    label: str
    created_at: int


@dataclass(frozen=True, slots=True)
class Identity:
    identity: str
    project_id: str
    kind: str
    added_at: int


@dataclass(frozen=True, slots=True)
class Checkout:
    id: str
    project_id: str
    path: str
    label: str | None
    last_branch: str | None
    last_head: str | None
    last_config: str | None
    last_seen: int
    present: int


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project: Project
    primary_identity: str | None
    memories: int
    candidates: int
    last_activity: int | None
    family: str | None = None
    checkouts: tuple[Checkout, ...] = field(default=())


def to_json(obj: Any) -> Any:
    """Dataclass → JSON-ready dict with *_iso companions for every timestamp."""
    if isinstance(obj, list | tuple):
        return [to_json(o) for o in obj]
    if not hasattr(obj, "__dataclass_fields__"):
        return obj
    return _with_iso(asdict(obj))


def _with_iso(value: Any) -> Any:
    if isinstance(value, list):
        return [_with_iso(v) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "refs" and isinstance(item, str):
            item = json.loads(item)
        out[key] = _with_iso(item)
        if key in TIMESTAMP_FIELDS:
            out[f"{key}_iso"] = iso(item)
    return out


def memory_json(m: Memory) -> dict[str, Any]:
    data: dict[str, Any] = to_json(m)
    data["tags"] = list(m.tags)
    return data


def hit_json(h: Hit) -> dict[str, Any]:
    data = memory_json(h.memory)
    data["score"] = round(h.score, 6)
    data["matched_by"] = list(h.matched_by)
    if h.bm25 is not None:
        data["bm25"] = round(h.bm25, 6)
    if h.vec_distance is not None:
        data["vec_distance"] = round(h.vec_distance, 6)
    return data
