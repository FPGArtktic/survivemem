# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP server over stdio (§8). The project is resolved from the server's working directory."""

import functools
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from survivemem import db as dbmod
from survivemem import embed, identity, investigate, payload, state, store, version
from survivemem.config import Config, ConfigError, load_config
from survivemem.db import transaction
from survivemem.model import Investigation, SessionState, hit_json, iso, memory_json, to_json

log = logging.getLogger(__name__)

Session = tuple[sqlite3.Connection, Config, identity.Resolution]

INSTRUCTIONS = """\
survivemem is the user's local long-term memory. Search it before answering questions about
the user's preferences, environment or earlier project decisions. Store a memory only when the
user explicitly asks you to remember something or confirms a fact you proposed; never capture
silently. Store distilled conclusions (≤ 400 chars) with refs pointing at the source, never raw
tool output. The project is derived from the working directory.
"""

DOMAIN_ERRORS = (
    store.Invalid,
    store.NotFound,
    store.Ambiguous,
    dbmod.DBError,
    ConfigError,
    sqlite3.Error,
)


@contextmanager
def session() -> Iterator[Session]:
    cfg = load_config()
    with closing(dbmod.connect()) as db:
        if os.environ.get("SURVIVEMEM_DAEMON") == "1":
            yield db, cfg, _latest_session_project(db)
        else:
            yield db, cfg, identity.resolve_tolerant(db, Path.cwd(), cfg)


def _latest_session_project(db: sqlite3.Connection) -> identity.Resolution:
    """Profile B: the daemon has no repository around it and HTTP MCP clients do not send their
    directory, so tools act on the project of the session the hooks saw last."""
    project = store.latest_active_project(db)
    found = store.find_project(db, project) if project else None
    return identity.Resolution(found, "session" if found else "none")


def _project_id(db: sqlite3.Connection, res: identity.Resolution, ref: str | None) -> str | None:
    return store.get_project(db, ref).id if ref else res.project_id


def _view(db: sqlite3.Connection, res: identity.Resolution, pid: str | None) -> store.View:
    """Visible memories for project `pid`; branch/clone scopes only when it is the current one."""
    if pid != res.project_id:
        return store.visible_view(db, pid)
    branch = res.location.branch if res.location else None
    return store.visible_view(db, pid, branch, res.checkout.id if res.checkout else None)


def _labelled(db: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    labels = store.project_labels(db)
    data["project_label"] = labels.get(data["project"], "global") if data["project"] else "global"
    return data


def list_projects() -> dict[str, Any]:
    with session() as (db, _, res):
        return {"projects": payload.projects_payload(db, res), "current": res.project_id}


def search_memories(query: str, project: str | None = None, limit: int = 8) -> dict[str, Any]:
    with session() as (db, cfg, res):
        view = _view(db, res, _project_id(db, res, project))
        vector = embed.query_vector(db, query, cfg)
        hits = store.search(db, query, view, limit=max(1, min(limit, 50)), vector=vector)
        return {"hits": [_labelled(db, hit_json(h)) for h in hits]}


def store_memory(
    content: str,
    kind: str,
    tags: list[str] | None = None,
    project: str | None = None,
    scope: str | None = None,
    refs: list[dict[str, Any]] | None = None,
    anchor: str | None = None,
    global_memory: bool = False,
) -> dict[str, Any]:
    if kind == "candidate":
        raise store.Invalid("candidates are created by hooks; store a reviewed kind instead")
    with session() as (db, _, res):
        m = store.add_memory(
            db,
            content,
            kind,
            project=None if global_memory else _project_id(db, res, project),
            scope=identity.scope_for(res, scope),
            source="mcp",
            tags=tags or (),
            refs=refs,
            anchor=anchor,
        )
        return _labelled(db, memory_json(m))


def forget_memory(id: str) -> dict[str, Any]:
    with session() as (db, _, _res):
        return _labelled(db, memory_json(store.forget_memory(db, id)))


def list_recent(limit: int = 20) -> dict[str, Any]:
    with session() as (db, _, res):
        view = _view(db, res, res.project_id) if res.project_id else store.ALL
        memories = store.list_memories(db, view, limit=max(1, min(limit, 200)))
        return {"memories": [_labelled(db, memory_json(m)) for m in memories]}


def memory_status() -> dict[str, Any]:
    with session() as (db, cfg, res):
        status = payload.status_payload(db, cfg)
        status["version"] = version()
        status["current_project"] = res.project.label if res.project else None
        return status


def _state_payload(saved: SessionState) -> dict[str, Any]:
    return {
        "session_id": saved.session_id,
        "project": saved.project,
        "state": saved.state,
        "updated_at": saved.updated_at,
        "updated_at_iso": iso(saved.updated_at),
        "rendered": state.render(saved.state),
    }


def checkpoint(
    goal: str,
    done: list[str],
    in_progress: str,
    next: list[str],  # shadows the builtin: the tool contract names it next
    decisions: list[str],
    files: list[str],
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    with session() as (db, _, res):
        sid = store.current_session(db, res.project_id)
        with transaction(db):  # hooks may update this session's state meanwhile
            return _state_payload(
                _checkpoint(db, res, sid, goal, done, in_progress, next, decisions, files, blockers)
            )


def _checkpoint(
    db: sqlite3.Connection,
    res: identity.Resolution,
    sid: str,
    goal: str,
    done: list[str],
    in_progress: str,
    next_: list[str],
    decisions: list[str],
    files: list[str],
    blockers: list[str] | None,
) -> SessionState:
    old = store.get_state(db, sid)
    new = state.checkpoint(
        old.state if old else {},
        goal=goal,
        done=done,
        in_progress=in_progress,
        next_=next_,
        decisions=decisions,
        files=files,
        blockers=blockers,
    )
    return store.save_state(db, sid, res.project_id, new)


def session_state() -> dict[str, Any]:
    with session() as (db, _, res):
        found = store.get_state(db, store.current_session(db, res.project_id))
        if found is None or state.is_empty(found.state):
            return {}
        return _state_payload(found)


def _inv_payload(
    db: sqlite3.Connection, res: identity.Resolution, inv: Investigation
) -> dict[str, Any]:
    found = store.get_state(db, store.current_session(db, res.project_id))
    next_steps = state.strings(found.state.get("next")) if found else []
    ctx = investigate.context(db, inv, res, next_steps)
    data: dict[str, Any] = to_json(inv)
    data["entries"] = len(ctx.entries)
    data["rendered"] = investigate.render(ctx)
    return data


def investigation_open(title: str, symptom: str, env: str | None = None) -> dict[str, Any]:
    with session() as (db, _, res):
        return _inv_payload(db, res, investigate.open_new(db, res, title, symptom, env))


def investigation_log(
    kind: str,
    text: str,
    command: str | None = None,
    result: str | None = None,
    anchor: str | None = None,
    refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with session() as (db, _, res):
        inv = investigate.require_open(db, res)
        entry = investigate.log_entry(
            db, res, inv, store.NewEntry(kind, text, command, result, anchor, refs=refs)
        )
        return {"entry": to_json(entry), "investigation": inv.id}


def investigation_status(id: str | None = None) -> dict[str, Any]:
    with session() as (db, _, res):
        inv = store.get_investigation(db, id) if id else investigate.require_open(db, res)
        return _inv_payload(db, res, inv)


def investigation_close(root_cause: str, fix: str, status: str = "solved") -> dict[str, Any]:
    with session() as (db, _, res):
        inv = investigate.require_open(db, res)
        closed, pitfall = investigate.close(db, res, inv, root_cause, fix, status)
        data = _inv_payload(db, res, closed)
        data["pitfall"] = memory_json(pitfall) if pitfall else None
        return data


Tool = Callable[..., dict[str, Any]]

TOOLS: tuple[tuple[Tool, str], ...] = (
    (
        list_projects,
        "READ. All projects with memory counts, identities and checkouts (path, branch, head, "
        "label); marks the current project and clone. No preconditions.",
    ),
    (
        search_memories,
        "READ. Relevance-ranked search over long-term memories of the current project (or "
        "`project`, a label or id), its linked projects and global memories. Returns at most "
        "`limit` hits, never a full dump. Candidates are never returned.",
    ),
    (
        store_memory,
        "WRITE. Store one memory. Precondition: the user explicitly asked to remember it or "
        "confirmed a fact you proposed; never store silently. kind: fact, preference, project, "
        "decision, environment, pitfall. scope: project (default), branch (true only on this "
        "branch) or checkout (true only for this clone). global_memory=true for facts that hold "
        "everywhere. refs: pointers (§15) to the source; store the conclusion, not raw output.",
    ),
    (
        forget_memory,
        "WRITE. Soft-delete a memory by id or unique id prefix (restorable with the CLI). "
        "Precondition: the user asked to forget it.",
    ),
    (
        list_recent,
        "READ. Newest memories first (current project, linked projects and global; all projects "
        "outside a project). Excludes candidates.",
    ),
    (
        memory_status,
        "READ. DB path, schema version, counts per kind, FTS health, vector search state "
        "(sqlite-vec, embeddings), Ollama reachability and the current project.",
    ),
    (
        checkpoint,
        "WRITE. Save the working state of this session so it survives context compaction: "
        "goal, done, in_progress, next, decisions (each with a one-line reason), files, "
        "optional blockers. Overwrites what it states, merges files. Call after every "
        "completed subtask and before any long operation; reference the open investigation "
        "id in in_progress.",
    ),
    (
        session_state,
        "READ. The current session's checkpoint (state and its rendering), or {} when none.",
    ),
    (
        investigation_open,
        "WRITE. Open a debug investigation for the current project (one open at a time). Use "
        "before the first experiment when a task is debugging: a symptom with an unknown cause. "
        "Snapshots the project's environment memories; env adds free-form setup text.",
    ),
    (
        investigation_log,
        "WRITE. Append to the open investigation. kind: hypothesis, experiment, observation, "
        "ruled_out (with a one-line reason), note. An experiment needs `command` (logged "
        "BEFORE running it) and gets `result` good/bad/inconclusive after; anchor defaults to "
        "the current HEAD(+.config). One entry per experiment, never a summary of several.",
    ),
    (
        investigation_status,
        "READ. Compact state of the open investigation (or `id`): symptom, setup, ruled out, "
        "open hypotheses, last experiments, blocker, commands not to re-run. Call at the start "
        "of any debugging task and after every compaction.",
    ),
    (
        investigation_close,
        "WRITE. Close the open investigation with root_cause and fix; status solved (default) "
        "writes one pitfall memory pointing at it, abandoned writes none.",
    ),
)


def guarded[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Domain errors become tool errors with their message; anything else is a bug."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except DOMAIN_ERRORS as e:
            raise ToolError(str(e)) from e

    return wrapper


def build_server() -> MCPServer:
    server: MCPServer = MCPServer("survivemem", instructions=INSTRUCTIONS, version=version())
    for func, description in TOOLS:
        server.tool(name=func.__name__, description=description)(guarded(func))
    return server


def serve() -> None:
    build_server().run("stdio")
