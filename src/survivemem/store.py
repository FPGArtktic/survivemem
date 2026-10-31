# SPDX-License-Identifier: GPL-3.0-or-later
"""CRUD and search over memories, projects, identities, checkouts and workspace links."""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from survivemem.db import KINDS, SOURCES, new_id, now_ms, transaction
from survivemem.model import (
    Checkout,
    Hit,
    Identity,
    Memory,
    Project,
    ProjectSummary,
    SessionState,
)
from survivemem.redact import contains_secret


class NotFound(LookupError):
    pass


class Ambiguous(LookupError):
    pass


class Invalid(ValueError):
    pass


REF_TYPES: dict[str, tuple[str, ...]] = {
    "symbol": ("path",),
    "doc": ("doc_id",),
    "file": ("path",),
    "investigation": ("id",),
    "url": ("url",),
}
RRF_K = 60
CANDIDATE_POOL = 50
TRIGRAM_THRESHOLD = 3

# ---------------------------------------------------------------------------------------------
# SQL

MEMORY_COLS = (
    "m.id, m.content, m.kind, m.project, m.scope, m.source, m.refs, m.anchor, m.created_at, "
    "m.updated_at, m.deleted_at, "
    "(SELECT group_concat(t.tag, char(31)) FROM memory_tags t WHERE t.memory_id = m.id) AS tags"
)
# :all bypasses every filter; otherwise the project set (plus NULL when :global) and, unless
# :any_scope, the scope set decide visibility.
SQL_VIEW = (
    "(:all = 1 OR (:global = 1 AND m.project IS NULL)"
    " OR m.project IN (SELECT value FROM json_each(:projects)))"
    " AND (:all = 1 OR :any_scope = 1 OR m.scope IN (SELECT value FROM json_each(:scopes)))"
)
SQL_LIVE = "m.deleted_at IS NULL AND m.kind != 'candidate'"

SQL_MEMORY_INSERT = (
    "INSERT INTO memories (id, content, kind, project, scope, source, refs, anchor,"
    " created_at, updated_at) VALUES (:id, :content, :kind, :project, :scope, :source, :refs,"
    " :anchor, :now, :now)"
)
SQL_TAG_INSERT = "INSERT OR IGNORE INTO memory_tags (memory_id, tag) VALUES (?, ?)"
SQL_TAG_DELETE = "DELETE FROM memory_tags WHERE memory_id = ? AND tag = ?"
SQL_MEMORY_GET = (
    "SELECT " + MEMORY_COLS + " FROM memories m WHERE m.id = ? AND m.deleted_at IS NULL"
)
SQL_MEMORY_GET_INCLUDING_DELETED = "SELECT " + MEMORY_COLS + " FROM memories m WHERE m.id = ?"
SQL_MEMORY_PREFIX = "SELECT id FROM memories WHERE id LIKE ? || '%' ORDER BY id LIMIT 2"
SQL_MEMORY_UPDATE = (
    "UPDATE memories SET content = :content, kind = :kind, project = :project, scope = :scope,"
    " refs = :refs, anchor = :anchor, updated_at = :now WHERE id = :id AND deleted_at IS NULL"
)
SQL_MEMORY_SOFT_DELETE = (
    "UPDATE memories SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL"
)
SQL_MEMORY_RESTORE = "UPDATE memories SET deleted_at = NULL, updated_at = ? WHERE id = ?"
SQL_MEMORY_LIST = (
    "SELECT "
    + MEMORY_COLS
    + " FROM memories m WHERE m.deleted_at IS NULL AND "
    + SQL_VIEW
    + " AND (:kind IS NULL OR m.kind = :kind)"
    " AND (:kind = 'candidate' OR m.kind != 'candidate')"
    " AND (:tag IS NULL OR EXISTS (SELECT 1 FROM memory_tags t WHERE t.memory_id = m.id"
    " AND t.tag = :tag))"
    " AND (:since IS NULL OR m.updated_at >= :since)"
    " ORDER BY m.updated_at DESC, m.id LIMIT :limit"
)
SQL_SEARCH_FTS = (
    "SELECT " + MEMORY_COLS + ", bm25(memories_fts) AS rank FROM memories_fts"
    " JOIN memories m ON m.rid = memories_fts.rowid"
    " WHERE memories_fts MATCH :q AND "
    + SQL_LIVE
    + " AND "
    + SQL_VIEW
    + " ORDER BY rank LIMIT :pool"
)
SQL_SEARCH_TRI = (
    "SELECT " + MEMORY_COLS + ", bm25(memories_tri) AS rank FROM memories_tri"
    " JOIN memories m ON m.rid = memories_tri.rowid"
    " WHERE memories_tri MATCH :q AND "
    + SQL_LIVE
    + " AND "
    + SQL_VIEW
    + " ORDER BY rank LIMIT :pool"
)
SQL_KIND_COUNTS = (
    "SELECT kind, count(*) AS n FROM memories WHERE deleted_at IS NULL GROUP BY kind ORDER BY kind"
)
SQL_FTS_REBUILD = (
    "INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');"
    " INSERT INTO memories_tri(memories_tri) VALUES ('rebuild')"
)
SQL_FTS_CHECK = "INSERT INTO memories_fts(memories_fts, rank) VALUES ('integrity-check', 1)"
SQL_TRI_CHECK = "INSERT INTO memories_tri(memories_tri, rank) VALUES ('integrity-check', 1)"

SQL_PROJECT_INSERT = "INSERT INTO projects (id, label, created_at) VALUES (?, ?, ?)"
SQL_PROJECT_BY_ID = "SELECT id, label, created_at FROM projects WHERE id = ?"
SQL_PROJECT_BY_LABEL = "SELECT id, label, created_at FROM projects WHERE label = ? ORDER BY id"
SQL_PROJECT_PREFIX = "SELECT id FROM projects WHERE id LIKE ? || '%' ORDER BY id LIMIT 2"
SQL_PROJECT_LABELS = "SELECT id, label FROM projects"
SQL_PROJECT_RENAME = "UPDATE projects SET label = ? WHERE id = ?"
SQL_PROJECT_DELETE = "DELETE FROM projects WHERE id = ?"
SQL_PROJECT_SUMMARIES = """
SELECT p.id, p.label, p.created_at,
  (SELECT i.identity FROM project_identities i WHERE i.project_id = p.id
    ORDER BY CASE i.kind WHEN 'name' THEN 0 WHEN 'remote' THEN 1 WHEN 'root' THEN 2 ELSE 3 END,
      i.added_at, i.identity LIMIT 1) AS primary_identity,
  (SELECT count(*) FROM memories m WHERE m.project = p.id AND m.deleted_at IS NULL
    AND m.kind != 'candidate') AS memories,
  (SELECT count(*) FROM memories m WHERE m.project = p.id AND m.deleted_at IS NULL
    AND m.kind = 'candidate') AS candidates,
  max(p.created_at,
    coalesce((SELECT max(m.updated_at) FROM memories m WHERE m.project = p.id), 0),
    coalesce((SELECT max(c.last_seen) FROM checkouts c WHERE c.project_id = p.id), 0))
    AS last_activity
FROM projects p ORDER BY last_activity DESC, p.id
"""
SQL_MOVE_MEMORIES = "UPDATE memories SET project = ? WHERE project = ?"
SQL_MOVE_IDENTITIES = "UPDATE project_identities SET project_id = ? WHERE project_id = ?"
SQL_MOVE_CHECKOUTS = "UPDATE checkouts SET project_id = ? WHERE project_id = ?"
SQL_MOVE_LINKS_PARENT = "UPDATE OR IGNORE project_links SET parent = ? WHERE parent = ?"
SQL_MOVE_LINKS_CHILD = "UPDATE OR IGNORE project_links SET child = ? WHERE child = ?"
SQL_LINKS_DELETE_PROJECT = "DELETE FROM project_links WHERE parent = ? OR child = ?"

SQL_IDENTITY_ATTACH = (
    "INSERT OR IGNORE INTO project_identities (identity, project_id, kind, added_at)"
    " VALUES (?, ?, ?, ?)"
)
SQL_IDENTITY_STEAL = (
    "INSERT INTO project_identities (identity, project_id, kind, added_at) VALUES (?, ?, ?, ?)"
    " ON CONFLICT(identity) DO UPDATE SET project_id = excluded.project_id,"
    " added_at = excluded.added_at"
)
SQL_IDENTITY_GET = (
    "SELECT identity, project_id, kind, added_at FROM project_identities WHERE identity = ?"
)
SQL_IDENTITY_DELETE = "DELETE FROM project_identities WHERE identity = ?"
SQL_IDENTITIES_OF = (
    "SELECT identity, project_id, kind, added_at FROM project_identities WHERE project_id = ?"
    " ORDER BY CASE kind WHEN 'name' THEN 0 WHEN 'remote' THEN 1 WHEN 'root' THEN 2 ELSE 3 END,"
    " added_at, identity"
)

CHECKOUT_COLS = (
    "id, project_id, path, label, last_branch, last_head, last_config, last_seen, present"
)
SQL_CHECKOUT_UPSERT = (
    "INSERT INTO checkouts (" + CHECKOUT_COLS + ") VALUES (:id, :project_id, :path, NULL,"
    " :branch, :head, :config, :now, 1) ON CONFLICT(path) DO UPDATE SET"
    " project_id = excluded.project_id, last_branch = excluded.last_branch,"
    " last_head = excluded.last_head, last_config = excluded.last_config,"
    " last_seen = excluded.last_seen, present = 1"
)
SQL_CHECKOUT_GET = "SELECT " + CHECKOUT_COLS + " FROM checkouts WHERE id = ?"
SQL_CHECKOUT_BY_PATH = "SELECT " + CHECKOUT_COLS + " FROM checkouts WHERE path = ?"
SQL_CHECKOUT_PREFIX = "SELECT id FROM checkouts WHERE id LIKE ? || '%' ORDER BY id LIMIT 2"
SQL_CHECKOUTS_ALL = "SELECT " + CHECKOUT_COLS + " FROM checkouts ORDER BY last_seen DESC, id"
SQL_CHECKOUTS_OF = (
    "SELECT " + CHECKOUT_COLS + " FROM checkouts WHERE project_id = ? ORDER BY last_seen DESC, id"
)
SQL_CHECKOUT_LABEL = "UPDATE checkouts SET label = ? WHERE id = ?"
SQL_CHECKOUT_MARK_GONE = "UPDATE checkouts SET present = 0 WHERE id = ?"
SQL_CHECKOUT_PURGE = (
    "DELETE FROM checkouts WHERE present = 0 AND NOT EXISTS (SELECT 1 FROM memories m"
    " WHERE m.scope = 'checkout:' || checkouts.id AND m.deleted_at IS NULL)"
)

STATE_COLS = "session_id, project, state, updated_at"
SQL_STATE_GET = "SELECT " + STATE_COLS + " FROM session_state WHERE session_id = ?"
SQL_STATE_SAVE = (
    "INSERT INTO session_state (" + STATE_COLS + ") VALUES (:sid, :project, :state, :now)"
    " ON CONFLICT(session_id) DO UPDATE SET project = coalesce(excluded.project,"
    " session_state.project), state = excluded.state, updated_at = excluded.updated_at"
)
SQL_STATE_TOUCH = (
    "INSERT INTO session_state (" + STATE_COLS + ") VALUES (:sid, :project, '{}', :now)"
    " ON CONFLICT(session_id) DO UPDATE SET project = coalesce(excluded.project,"
    " session_state.project), updated_at = excluded.updated_at"
)
SQL_STATE_LATEST = (
    "SELECT " + STATE_COLS + " FROM session_state WHERE project IS ?"
    " ORDER BY updated_at DESC, session_id LIMIT 1"
)
SQL_STATE_LIST = "SELECT " + STATE_COLS + " FROM session_state ORDER BY updated_at DESC, session_id"
SQL_STATE_PURGE = "DELETE FROM session_state WHERE updated_at < ?"
SQL_STATE_PROJECT_MOVE = "UPDATE session_state SET project = ? WHERE project = ?"

SQL_LINK_INSERT = "INSERT OR IGNORE INTO project_links (parent, child) VALUES (?, ?)"
SQL_LINK_DELETE = "DELETE FROM project_links WHERE parent = ? AND child = ?"
SQL_LINKED = (
    "SELECT child AS id FROM project_links WHERE parent = :p"
    " UNION SELECT parent FROM project_links WHERE child = :p"
)
SQL_LINKS_ALL = "SELECT parent, child FROM project_links ORDER BY parent, child"

# ---------------------------------------------------------------------------------------------
# Views


@dataclass(frozen=True, slots=True)
class View:
    """Which memories are visible: a project set (plus global rows) and a scope set."""

    projects: tuple[str, ...] = ()
    include_global: bool = True
    scopes: tuple[str, ...] | None = None
    all: bool = False

    def params(self) -> dict[str, Any]:
        return {
            "all": int(self.all),
            "global": int(self.include_global),
            "projects": json.dumps(list(self.projects)),
            "any_scope": int(self.scopes is None),
            "scopes": json.dumps(list(self.scopes or ())),
        }


ALL = View(all=True)
GLOBAL_ONLY = View(include_global=True)


def scopes_for(branch: str | None, checkout_id: str | None) -> tuple[str, ...]:
    scopes = ["project"]
    if branch and branch != "detached":
        scopes.append(f"branch:{branch}")
    if checkout_id:
        scopes.append(f"checkout:{checkout_id}")
    return tuple(scopes)


def visible_view(
    db: sqlite3.Connection,
    project: str | None,
    branch: str | None = None,
    checkout_id: str | None = None,
) -> View:
    """§7 step 2: own project, workspace-linked projects, global rows, current scopes."""
    projects = (project, *linked_projects(db, project)) if project else ()
    return View(projects=projects, scopes=scopes_for(branch, checkout_id))


# ---------------------------------------------------------------------------------------------
# Memories


def _split_tags(raw: str | None) -> tuple[str, ...]:
    return tuple(sorted(raw.split("\x1f"))) if raw else ()


def _memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        content=row["content"],
        kind=row["kind"],
        project=row["project"],
        scope=row["scope"],
        source=row["source"],
        refs=row["refs"],
        anchor=row["anchor"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
        tags=_split_tags(row["tags"]),
    )


def normalise_tags(tags: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for tag in tags:
        tag = tag.strip().lstrip("#").strip()
        if tag:
            seen[tag] = None
    return tuple(seen)


def validate_scope(scope: str) -> str:
    if scope == "project":
        return scope
    kind, _, value = scope.partition(":")
    if kind in ("branch", "checkout") and value:
        return scope
    raise Invalid(f"scope must be 'project', 'branch:<name>' or 'checkout:<id>', got {scope!r}")


def validate_refs(refs: Any) -> str | None:
    """Canonical JSON for a refs array (§15); unknown types and missing keys are rejected."""
    if refs is None:
        return None
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except json.JSONDecodeError as e:
            raise Invalid(f"refs is not valid JSON: {e}") from e
    if isinstance(refs, dict):
        refs = [refs]
    if not isinstance(refs, list):
        raise Invalid("refs must be a JSON array of objects")
    for ref in refs:
        if not isinstance(ref, dict):
            raise Invalid("each ref must be an object")
        required = REF_TYPES.get(ref.get("type", ""))
        if required is None:
            raise Invalid(f"unknown ref type {ref.get('type')!r}; allowed: {', '.join(REF_TYPES)}")
        missing = [key for key in required if not ref.get(key)]
        if missing:
            raise Invalid(f"ref of type {ref['type']!r} needs {', '.join(missing)}")
    return json.dumps(refs, ensure_ascii=False, separators=(",", ":")) if refs else None


def _check_content(content: str) -> str:
    content = content.strip()
    if not content:
        raise Invalid("content is empty")
    if contains_secret(content):
        raise Invalid("content looks like it contains a secret (token, key or password)")
    return content


def _check_choice(value: str, allowed: Sequence[str], what: str) -> str:
    if value not in allowed:
        raise Invalid(f"{what} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def add_memory(
    db: sqlite3.Connection,
    content: str,
    kind: str,
    *,
    project: str | None = None,
    scope: str = "project",
    source: str = "cli",
    tags: Iterable[str] = (),
    refs: Any = None,
    anchor: str | None = None,
) -> Memory:
    mid = new_id()
    params = {
        "id": mid,
        "content": _check_content(content),
        "kind": _check_choice(kind, KINDS, "kind"),
        "project": project,
        "scope": validate_scope(scope),
        "source": _check_choice(source, SOURCES, "source"),
        "refs": validate_refs(refs),
        "anchor": anchor,
        "now": now_ms(),
    }
    with transaction(db):
        db.execute(SQL_MEMORY_INSERT, params)
        db.executemany(SQL_TAG_INSERT, [(mid, t) for t in normalise_tags(tags)])
    return get_memory(db, mid)


def _resolve_prefix(db: sqlite3.Connection, sql: str, ref: str, what: str) -> str:
    prefix = ref.strip().upper()
    if not prefix.isalnum():
        raise NotFound(f"{what} {ref!r} not found")
    rows = db.execute(sql, (prefix,)).fetchall()
    if not rows:
        raise NotFound(f"{what} {ref!r} not found")
    if len(rows) > 1:
        raise Ambiguous(f"{what} prefix {ref!r} is ambiguous")
    return str(rows[0][0])


def resolve_memory_id(db: sqlite3.Connection, ref: str) -> str:
    return _resolve_prefix(db, SQL_MEMORY_PREFIX, ref, "memory")


def get_memory(db: sqlite3.Connection, ref: str) -> Memory:
    row = db.execute(SQL_MEMORY_GET, (resolve_memory_id(db, ref),)).fetchone()
    if row is None:
        raise NotFound(f"memory {ref!r} not found (deleted?)")
    return _memory(row)


def get_memory_including_deleted(db: sqlite3.Connection, ref: str) -> Memory:
    row = db.execute(SQL_MEMORY_GET_INCLUDING_DELETED, (resolve_memory_id(db, ref),)).fetchone()
    return _memory(row)


@dataclass(frozen=True, slots=True)
class Edit:
    content: str | None = None
    kind: str | None = None
    add_tags: tuple[str, ...] = ()
    rm_tags: tuple[str, ...] = ()
    project: str | None = None
    make_global: bool = False
    scope: str | None = None
    refs: Any = None
    anchor: str | None = None


def edit_memory(db: sqlite3.Connection, ref: str, edit: Edit) -> Memory:
    old = get_memory(db, ref)
    params = {
        "id": old.id,
        "content": _check_content(edit.content) if edit.content is not None else old.content,
        "kind": _check_choice(edit.kind, KINDS, "kind") if edit.kind else old.kind,
        "project": None if edit.make_global else (edit.project or old.project),
        "scope": validate_scope(edit.scope) if edit.scope else old.scope,
        "refs": validate_refs(edit.refs) if edit.refs is not None else old.refs,
        "anchor": edit.anchor if edit.anchor is not None else old.anchor,
        "now": now_ms(),
    }
    with transaction(db):
        db.execute(SQL_MEMORY_UPDATE, params)
        db.executemany(SQL_TAG_INSERT, [(old.id, t) for t in normalise_tags(edit.add_tags)])
        db.executemany(SQL_TAG_DELETE, [(old.id, t) for t in normalise_tags(edit.rm_tags)])
    return get_memory(db, old.id)


def forget_memory(db: sqlite3.Connection, ref: str) -> Memory:
    memory = get_memory(db, ref)
    now = now_ms()
    db.execute(SQL_MEMORY_SOFT_DELETE, (now, now, memory.id))
    return get_memory_including_deleted(db, memory.id)


def restore_memory(db: sqlite3.Connection, ref: str) -> Memory:
    memory = get_memory_including_deleted(db, ref)
    db.execute(SQL_MEMORY_RESTORE, (now_ms(), memory.id))
    return get_memory(db, memory.id)


def list_memories(
    db: sqlite3.Connection,
    view: View,
    *,
    kind: str | None = None,
    tag: str | None = None,
    since: int | None = None,
    limit: int | None = None,
) -> list[Memory]:
    params = view.params() | {
        "kind": kind,
        "tag": tag,
        "since": since,
        "limit": -1 if limit is None else limit,
    }
    return [_memory(row) for row in db.execute(SQL_MEMORY_LIST, params)]


def kind_counts(db: sqlite3.Connection) -> dict[str, int]:
    return {row["kind"]: row["n"] for row in db.execute(SQL_KIND_COUNTS)}


def rebuild_fts(db: sqlite3.Connection) -> None:
    for statement in SQL_FTS_REBUILD.split(";"):
        db.execute(statement)


def fts_ok(db: sqlite3.Connection) -> bool:
    try:
        db.execute(SQL_FTS_CHECK)
        db.execute(SQL_TRI_CHECK)
    except sqlite3.DatabaseError:
        return False
    return True


# ---------------------------------------------------------------------------------------------
# Search (§7, §15)

TERM_RE = re.compile(r"[\w\-./:]+")
EDGE_PUNCT = "-./:"
STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have how i if
    in into is it its me my no not of on or our please should so that the their then there
    these this to us was we were what when where which who why will with would you your
    a ale bo by czy dla do i jak jaki jest jestem już ma mam mi na nie o od po przez sie się
    są tak tego to w we z za że""".split()
)
MAX_TERMS = 32


def fts_terms(text: str) -> list[str]:
    terms: dict[str, None] = {}
    for raw in TERM_RE.findall(text.lower()):
        term = raw.strip(EDGE_PUNCT)
        if len(term) >= 2 and term not in STOPWORDS:
            terms[term] = None
    return list(terms)[:MAX_TERMS]


def _fts_match(terms: Sequence[str]) -> str:
    # Prefix match: unicode61 keeps '.' and ':' inside tokens, so "LTS." at the end of a
    # sentence is indexed as "lts." and an exact "lts" would miss it.
    return " OR ".join(f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in terms)


def _tri_match(terms: Sequence[str]) -> str | None:
    usable = [f'"{t}"' for t in terms if len(t) >= 3]
    return " OR ".join(usable) if usable else None


def _ranked(db: sqlite3.Connection, sql: str, match: str, view: View) -> list[tuple[Memory, float]]:
    params = view.params() | {"q": match, "pool": CANDIDATE_POOL}
    return [(_memory(row), row["rank"]) for row in db.execute(sql, params)]


Lexical = list[tuple[Memory, float, str]]


def lexical_search(db: sqlite3.Connection, query: str, view: View) -> Lexical:
    """Exact-token FTS first, trigram substring hits only when FTS finds fewer than three."""
    terms = fts_terms(query)
    if not terms:
        return []
    hits = [(m, rank, "fts") for m, rank in _ranked(db, SQL_SEARCH_FTS, _fts_match(terms), view)]
    tri = _tri_match(terms)
    if len(hits) < TRIGRAM_THRESHOLD and tri:
        seen = {m.id for m, _, _ in hits}
        hits += [
            (m, rank, "trigram")
            for m, rank in _ranked(db, SQL_SEARCH_TRI, tri, view)
            if m.id not in seen
        ]
    return hits


def fuse(
    lexical: Sequence[tuple[Memory, float, str]],
    semantic: Sequence[tuple[Memory, float]] = (),
    limit: int = 8,
) -> list[Hit]:
    """Reciprocal Rank Fusion (k=60) over the lexical and, when present, the vector ranking."""
    scores: dict[str, float] = {}
    memories: dict[str, Memory] = {}
    matched: dict[str, list[str]] = {}
    bm25: dict[str, float] = {}
    distance: dict[str, float] = {}
    for rank, (memory, score, how) in enumerate(lexical, 1):
        scores[memory.id] = scores.get(memory.id, 0.0) + 1.0 / (RRF_K + rank)
        memories[memory.id] = memory
        matched.setdefault(memory.id, []).append(how)
        bm25[memory.id] = score
    for rank, (memory, dist) in enumerate(semantic, 1):
        scores[memory.id] = scores.get(memory.id, 0.0) + 1.0 / (RRF_K + rank)
        memories[memory.id] = memory
        matched.setdefault(memory.id, []).append("vec")
        distance[memory.id] = dist
    order = sorted(scores, key=lambda mid: (-scores[mid], mid))[:limit]
    return [
        Hit(memories[mid], scores[mid], tuple(matched[mid]), bm25.get(mid), distance.get(mid))
        for mid in order
    ]


def search(db: sqlite3.Connection, query: str, view: View, limit: int = 8) -> list[Hit]:
    return fuse(lexical_search(db, query, view), limit=limit)


# ---------------------------------------------------------------------------------------------
# Projects and identities


def _project(row: sqlite3.Row) -> Project:
    return Project(id=row["id"], label=row["label"], created_at=row["created_at"])


def _identity(row: sqlite3.Row) -> Identity:
    return Identity(
        identity=row["identity"],
        project_id=row["project_id"],
        kind=row["kind"],
        added_at=row["added_at"],
    )


def identity_kind(identity: str) -> str:
    kind = identity.partition(":")[0]
    if kind not in ("remote", "root", "path", "name") or ":" not in identity:
        raise Invalid(f"identity must start with remote:, root:, path: or name:, got {identity!r}")
    return kind


def create_project(db: sqlite3.Connection, label: str) -> Project:
    project = Project(id=new_id(), label=label, created_at=now_ms())
    db.execute(SQL_PROJECT_INSERT, (project.id, project.label, project.created_at))
    return project


def get_project(db: sqlite3.Connection, ref: str) -> Project:
    """By exact id, unique label, or unique id prefix."""
    row = db.execute(SQL_PROJECT_BY_ID, (ref,)).fetchone()
    if row:
        return _project(row)
    rows = db.execute(SQL_PROJECT_BY_LABEL, (ref,)).fetchall()
    if len(rows) > 1:
        raise Ambiguous(f"label {ref!r} names {len(rows)} projects; use the id")
    if rows:
        return _project(rows[0])
    row = db.execute(
        SQL_PROJECT_BY_ID, (_resolve_prefix(db, SQL_PROJECT_PREFIX, ref, "project"),)
    ).fetchone()
    return _project(row)


def find_project(db: sqlite3.Connection, ref: str) -> Project | None:
    try:
        return get_project(db, ref)
    except NotFound:
        return None


def project_labels(db: sqlite3.Connection) -> dict[str, str]:
    return {row["id"]: row["label"] for row in db.execute(SQL_PROJECT_LABELS)}


def project_summaries(db: sqlite3.Connection) -> list[ProjectSummary]:
    return [
        ProjectSummary(
            project=_project(row),
            primary_identity=row["primary_identity"],
            memories=row["memories"],
            candidates=row["candidates"],
            last_activity=row["last_activity"],
        )
        for row in db.execute(SQL_PROJECT_SUMMARIES)
    ]


def rename_project(db: sqlite3.Connection, ref: str, label: str) -> Project:
    project = get_project(db, ref)
    db.execute(SQL_PROJECT_RENAME, (label, project.id))
    return get_project(db, project.id)


def merge_projects(db: sqlite3.Connection, src_ref: str, dst_ref: str) -> Project:
    src, dst = get_project(db, src_ref), get_project(db, dst_ref)
    if src.id == dst.id:
        raise Invalid("cannot merge a project into itself")
    with transaction(db):
        for sql in MERGE_STATEMENTS:
            db.execute(sql, (dst.id, src.id))
        db.execute(SQL_LINKS_DELETE_PROJECT, (src.id, src.id))
        db.execute(SQL_LINK_DELETE, (dst.id, dst.id))
        db.execute(SQL_PROJECT_DELETE, (src.id,))
    return dst


MERGE_STATEMENTS: tuple[str, ...] = (
    SQL_MOVE_MEMORIES,
    SQL_MOVE_IDENTITIES,
    SQL_MOVE_CHECKOUTS,
    SQL_MOVE_LINKS_PARENT,
    SQL_MOVE_LINKS_CHILD,
    SQL_STATE_PROJECT_MOVE,
)


def find_identity(db: sqlite3.Connection, identity: str) -> Identity | None:
    row = db.execute(SQL_IDENTITY_GET, (identity,)).fetchone()
    return _identity(row) if row else None


def attach_identities(db: sqlite3.Connection, project_id: str, identities: Iterable[str]) -> None:
    """Record identities that no project owns yet; never steals."""
    now = now_ms()
    db.executemany(
        SQL_IDENTITY_ATTACH, [(i, project_id, identity_kind(i), now) for i in identities]
    )


def alias_add(db: sqlite3.Connection, ref: str, identity: str, *, steal: bool) -> Identity:
    project = get_project(db, ref)
    kind = identity_kind(identity)
    owner = find_identity(db, identity)
    if owner and owner.project_id != project.id and not steal:
        raise Invalid(f"{identity} belongs to project {owner.project_id}; pass --yes to move it")
    db.execute(SQL_IDENTITY_STEAL, (identity, project.id, kind, now_ms()))
    found = find_identity(db, identity)
    assert found is not None
    return found


def alias_rm(db: sqlite3.Connection, identity: str) -> Identity:
    found = find_identity(db, identity)
    if found is None:
        raise NotFound(f"identity {identity!r} not found")
    db.execute(SQL_IDENTITY_DELETE, (identity,))
    return found


def identities_of(db: sqlite3.Connection, project_id: str) -> list[Identity]:
    return [_identity(row) for row in db.execute(SQL_IDENTITIES_OF, (project_id,))]


# ---------------------------------------------------------------------------------------------
# Checkouts


def _checkout(row: sqlite3.Row) -> Checkout:
    return Checkout(*tuple(row))


@dataclass(frozen=True, slots=True)
class CheckoutState:
    path: str
    branch: str | None
    head: str | None
    config: str | None


def checkout_id(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:12]


def upsert_checkout(db: sqlite3.Connection, project_id: str, state: CheckoutState) -> Checkout:
    params = {
        "id": checkout_id(state.path),
        "project_id": project_id,
        "path": state.path,
        "branch": state.branch,
        "head": state.head,
        "config": state.config,
        "now": now_ms(),
    }
    db.execute(SQL_CHECKOUT_UPSERT, params)
    return get_checkout_by_path(db, state.path)


def get_checkout(db: sqlite3.Connection, ref: str) -> Checkout:
    cid = _resolve_prefix(db, SQL_CHECKOUT_PREFIX, ref.lower(), "clone").lower()
    return _checkout(db.execute(SQL_CHECKOUT_GET, (cid,)).fetchone())


def get_checkout_by_path(db: sqlite3.Connection, path: str) -> Checkout:
    row = db.execute(SQL_CHECKOUT_BY_PATH, (path,)).fetchone()
    if row is None:
        raise NotFound(f"no clone recorded at {path}")
    return _checkout(row)


def list_checkouts(db: sqlite3.Connection, project_id: str | None = None) -> list[Checkout]:
    if project_id is None:
        return [_checkout(row) for row in db.execute(SQL_CHECKOUTS_ALL)]
    return [_checkout(row) for row in db.execute(SQL_CHECKOUTS_OF, (project_id,))]


def label_checkout(db: sqlite3.Connection, ref: str, label: str) -> Checkout:
    checkout = get_checkout(db, ref)
    db.execute(SQL_CHECKOUT_LABEL, (label or None, checkout.id))
    return get_checkout(db, checkout.id)


def mark_checkout_gone(db: sqlite3.Connection, checkout_id_: str) -> None:
    db.execute(SQL_CHECKOUT_MARK_GONE, (checkout_id_,))


def purge_checkouts(db: sqlite3.Connection) -> int:
    return db.execute(SQL_CHECKOUT_PURGE).rowcount


# ---------------------------------------------------------------------------------------------
# Workspace links (§15)


def link_projects(db: sqlite3.Connection, parent: str, child: str) -> None:
    if parent == child:
        raise Invalid("a project cannot be linked to itself")
    db.execute(SQL_LINK_INSERT, (parent, child))


def unlink_projects(db: sqlite3.Connection, parent: str, child: str) -> None:
    db.execute(SQL_LINK_DELETE, (parent, child))


def linked_projects(db: sqlite3.Connection, project: str) -> tuple[str, ...]:
    return tuple(row["id"] for row in db.execute(SQL_LINKED, {"p": project}))


def all_links(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(row["parent"], row["child"]) for row in db.execute(SQL_LINKS_ALL)]


# ---------------------------------------------------------------------------------------------
# Session state (§16): ephemeral, per session, never searched or exported


def _state(row: sqlite3.Row) -> SessionState:
    try:
        data = json.loads(row["state"])
    except json.JSONDecodeError:
        data = {}
    return SessionState(
        row["session_id"], row["project"], data if isinstance(data, dict) else {}, row["updated_at"]
    )


def get_state(db: sqlite3.Connection, session_id: str) -> SessionState | None:
    row = db.execute(SQL_STATE_GET, (session_id,)).fetchone()
    return _state(row) if row else None


def save_state(
    db: sqlite3.Connection, session_id: str, project: str | None, state: dict[str, Any]
) -> SessionState:
    params = {
        "sid": session_id,
        "project": project,
        "now": now_ms(),
        "state": json.dumps(state, ensure_ascii=False),
    }
    db.execute(SQL_STATE_SAVE, params)
    found = get_state(db, session_id)
    assert found is not None
    return found


def touch_session(db: sqlite3.Connection, session_id: str, project: str | None) -> None:
    """Mark the session as the most recently active one of its project."""
    db.execute(SQL_STATE_TOUCH, {"sid": session_id, "project": project, "now": now_ms()})


def latest_state(db: sqlite3.Connection, project: str | None) -> SessionState | None:
    row = db.execute(SQL_STATE_LATEST, (project,)).fetchone()
    return _state(row) if row else None


def current_session(db: sqlite3.Connection, project: str | None) -> str:
    """MCP servers and the CLI are not told the Claude Code session id: use the session the
    prompt hook saw last in this project, or a stable per-project id when there is none."""
    latest = latest_state(db, project)
    return latest.session_id if latest else f"mcp-{project or 'global'}"


def list_states(db: sqlite3.Connection) -> list[SessionState]:
    return [_state(row) for row in db.execute(SQL_STATE_LIST)]


def purge_states(db: sqlite3.Connection, older_than_ms: int) -> int:
    return db.execute(SQL_STATE_PURGE, (older_than_ms,)).rowcount
