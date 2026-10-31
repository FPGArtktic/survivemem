# SPDX-License-Identifier: GPL-3.0-or-later
"""Export (JSONL, Markdown) and idempotent import of memories and their projects (§10.5)."""

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from survivemem import store
from survivemem.model import Memory, Project, iso, memory_json, to_json

log = logging.getLogger(__name__)

KIND_ORDER = ("decision", "pitfall", "fact", "environment", "project", "preference", "candidate")
KIND_HEADINGS = {
    "decision": "decisions",
    "pitfall": "pitfalls",
    "fact": "facts",
    "environment": "environment",
    "project": "project notes",
    "preference": "preferences",
    "candidate": "candidates",
}


@dataclass(slots=True)
class ImportResult:
    memories_added: int = 0
    memories_skipped: int = 0
    projects_added: int = 0
    errors: list[str] = field(default_factory=list)


def project_record(db: sqlite3.Connection, project: Project) -> dict[str, Any]:
    data: dict[str, Any] = to_json(project)
    data["record"] = "project"
    data["identities"] = [i.identity for i in store.identities_of(db, project.id)]
    return data


def records(
    db: sqlite3.Connection, view: store.View, kind: str | None = None
) -> Iterator[dict[str, Any]]:
    """Project records first (so an import can recreate them), then the memories."""
    memories = store.list_memories(db, view, kind=kind)
    labels = store.project_labels(db)
    for project_id in sorted({m.project for m in memories if m.project}):
        yield project_record(db, store.get_project(db, project_id))
    for m in memories:
        yield memory_json(m) | {"project_label": labels.get(m.project or "", None)}


def to_jsonl(items: Iterable[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(i, ensure_ascii=False) for i in items)


def md_line(m: Memory) -> str:
    tags = " ".join(f"#{t}" for t in m.tags)
    content = m.content.replace("\n", "\n  ")
    line = f"- [{m.id}] {(iso(m.updated_at) or '')[:10]} — {content}"
    return f"{line} {tags}" if tags else line


def to_markdown(db: sqlite3.Connection, memories: Sequence[Memory], today: datetime) -> str:
    lines = [f"# survivemem export — {today.astimezone(UTC):%Y-%m-%d}"]
    groups: dict[str | None, list[Memory]] = {}
    for m in memories:
        groups.setdefault(m.project, []).append(m)
    labels = store.project_labels(db)
    projects = sorted((p for p in groups if p), key=lambda p: (labels.get(p, p), p))
    for project in [*projects, None] if None in groups else projects:
        title = f"Project: {labels.get(project, project)} (id {project})" if project else "Global"
        lines += ["", f"## {title}"]
        for kind in KIND_ORDER:
            items = [m for m in groups[project] if m.kind == kind]
            if items:
                lines += [f"### {KIND_HEADINGS[kind]}", *(md_line(m) for m in items)]
    return "\n".join(lines)


def import_lines(db: sqlite3.Connection, lines: Iterable[str]) -> ImportResult:
    result = ImportResult()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            _import_one(db, json.loads(line), result)
        except (ValueError, KeyError, TypeError) as e:
            result.errors.append(f"line {number}: {e}")
    return result


def _import_one(db: sqlite3.Connection, data: Any, result: ImportResult) -> None:
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    if data.get("record") == "project":
        recorded = Project(str(data["id"]), str(data["label"]), int(data["created_at"]))
        result.projects_added += store.import_project(db, recorded)
        store.attach_identities(db, recorded.id, [str(i) for i in data.get("identities", [])])
        return
    project_id = data.get("project")
    if isinstance(project_id, str) and store.find_project(db, project_id) is None:
        label = str(data.get("project_label") or project_id)
        store.import_project(db, Project(project_id, label, int(data.get("created_at") or 0)))
        result.projects_added += 1
    if store.import_memory(db, data):
        result.memories_added += 1
    else:
        result.memories_skipped += 1
