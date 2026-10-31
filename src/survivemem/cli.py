# SPDX-License-Identifier: GPL-3.0-or-later
"""survivemem command line: one binary, human and machine output (§10)."""

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections.abc import Callable, Sequence
from functools import cached_property
from pathlib import Path
from typing import Any

from survivemem import db as dbmod
from survivemem import hooks, identity, state, store, version
from survivemem.config import Config, ConfigError, load_config
from survivemem.model import Checkout, Memory, SessionState, hit_json, memory_json, to_json
from survivemem.output import FORMATS, Column, Output, Record, emit_error, relative

EXIT_USAGE, EXIT_NOT_FOUND, EXIT_DB, EXIT_AMBIGUOUS = 1, 2, 3, 4
SINCE_RE = re.compile(r"^(\d+)([mhdw])$")
SINCE_UNITS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}

HELP = """\
usage: survivemem <command> [options]       (survivemem help <command> for details)

Local persistent memory for Claude Code and Claude Desktop. One SQLite file.

Everyday
  add "<text>" --kind K --tag T    store a memory (fact, preference, decision, …)
  search "<query>" [--explain]      relevance-ranked lookup
  list [--kind K] [--since 7d]      browse; show <id>, edit <id>, forget <id>, restore <id>
  review                            promote or discard captured candidates
Projects and clones
  projects [show|rename|merge|identities|alias|here|link|unlink|autodetect]
  clones [label|prune|diff]         checkouts of the current project
Debugging (investigations)
  inv open|list|show|log|attach|bisect|pause|resume|close|export|loops
  ingest-log <file> --tool T        extract errors from a build/boot log
For AI clients and scripts
  brief | status | schema           orientation, health, JSON Schema
  export [--format md|jsonl] | import < file.jsonl
  state show|set|purge              compaction-proof session checkpoint
Integration and maintenance
  serve                             MCP stdio server
  hook prompt|pretool|posttool|stop|precompact|session-start
  reindex | doctor [--stale]

Output: --json, --jsonl, --format table|md|json|jsonl, --quiet, --wide, --no-color.
Filters: --project <name|id>, --global, --all. IDs accept any unique prefix.
Exit codes: 0 ok, 1 usage, 2 not found, 3 database, 4 ambiguous id prefix.
"""


class UsageError(Exception):
    pass


class VersionAction(argparse.Action):
    """--version without paying for the version lookup on every other invocation."""

    def __call__(self, parser: argparse.ArgumentParser, *_: Any) -> None:
        sys.stdout.write(f"survivemem {version()}\n")
        parser.exit()


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise UsageError(message)


# ---------------------------------------------------------------------------------------------
# Context shared by commands


class Ctx:
    def __init__(self, args: argparse.Namespace, out: Output) -> None:
        self.args = args
        self.out = out

    @cached_property
    def db(self) -> sqlite3.Connection:
        return dbmod.connect()

    @cached_property
    def cfg(self) -> Config:
        return load_config()

    @cached_property
    def here(self) -> identity.Resolution:
        return identity.resolve(self.db, Path.cwd(), self.cfg)

    @cached_property
    def labels(self) -> dict[str, str]:
        return store.project_labels(self.db)

    @cached_property
    def checkouts(self) -> dict[str, Checkout]:
        return {c.id: c for c in store.list_checkouts(self.db)}

    def flag(self, name: str) -> Any:
        return getattr(self.args, name, None)

    def project_ref(self) -> str | None:
        """Project for writes: --project X, --global (None), else the current one."""
        if self.flag("global_"):
            return None
        if self.flag("project"):
            return store.get_project(self.db, self.args.project).id
        return self.here.project_id

    def list_view(self) -> store.View:
        if self.flag("all"):
            return store.ALL
        if self.flag("global_"):
            return store.View()
        if self.flag("project"):
            return store.View((store.get_project(self.db, self.args.project).id,), False)
        project = self.here.project_id
        return store.View((project, *store.linked_projects(self.db, project)) if project else ())

    def search_view(self) -> store.View:
        if self.flag("all"):
            return store.ALL
        if self.flag("global_"):
            return store.View(scopes=("project",))
        here = self.here
        project = here.project_id
        if self.flag("project"):
            project = store.get_project(self.db, self.args.project).id
        same = project == here.project_id
        branch = here.location.branch if same and here.location else None
        checkout = here.checkout.id if same and here.checkout else None
        return store.visible_view(self.db, project, branch, checkout)

    def label(self, project: str | None) -> str:
        return "global" if project is None else self.labels.get(project, project[:8])

    def confirm(self, question: str) -> bool:
        if self.flag("yes"):
            return True
        if not sys.stdin.isatty():
            raise UsageError(f"{question} — refusing without --yes (stdin is not a TTY)")
        sys.stderr.write(f"{question} [y/N] ")
        return sys.stdin.readline().strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------------------------
# Rendering helpers


def parse_since(value: str | None) -> int | None:
    if not value:
        return None
    m = SINCE_RE.match(value)
    if not m:
        raise UsageError(f"--since expects e.g. 30m, 12h, 7d, 2w; got {value!r}")
    return dbmod.now_ms() - int(m.group(1)) * SINCE_UNITS[m.group(2)]


def memory_record(ctx: Ctx, m: Memory) -> Record:
    data = memory_json(m)
    data["project_label"] = ctx.label(m.project)
    return data


def display_content(ctx: Ctx, r: Record) -> str:
    suffix = identity.scope_suffix(r["scope"], ctx.checkouts)
    return f"{r['content']} {suffix}" if suffix else r["content"]


def memory_columns(ctx: Ctx) -> list[Column]:
    return [
        Column("id", lambda r: r["id"][:8]),
        Column("kind", lambda r: r["kind"]),
        Column("project", lambda r: r["project_label"]),
        Column("updated", lambda r: relative(r["updated_at"])),
        Column("tags", lambda r: ",".join(r["tags"])),
        Column("content", lambda r: display_content(ctx, r), flex=True),
    ]


def memory_detail(ctx: Ctx, r: Record) -> str:
    lines = [
        f"id       {r['id']}",
        f"kind     {r['kind']}",
        f"project  {r['project_label']}" + (f" ({r['project']})" if r["project"] else ""),
        f"scope    {r['scope']} {identity.scope_suffix(r['scope'], ctx.checkouts)}".rstrip(),
        f"source   {r['source']}",
        f"tags     {', '.join(r['tags']) or '-'}",
        f"created  {r['created_at_iso']}",
        f"updated  {r['updated_at_iso']}",
    ]
    if r["deleted_at"]:
        lines.append(f"deleted  {r['deleted_at_iso']}")
    if r["anchor"]:
        lines.append(f"anchor   {r['anchor']}")
    lines += [f"ref      {line}" for line in ref_commands(r.get("refs") or [])]
    return "\n".join([*lines, "", r["content"]])


def ref_commands(refs: Sequence[dict[str, Any]]) -> list[str]:
    """§15: refs as copy-pasteable commands where possible."""
    lines = []
    for ref in refs:
        kind = ref.get("type")
        if kind in ("symbol", "file") and ref.get("commit"):
            lines.append(f"git show {ref['commit']}:{ref['path']}")
        elif kind == "symbol":
            lines.append(f"{ref.get('tool', 'jcodemunch')}: {ref['path']} {ref.get('symbol', '')}")
        elif kind == "investigation":
            lines.append(f"survivemem inv show {ref['id']}")
        elif kind == "doc":
            lines.append(f"{ref.get('tool', 'rag')}: doc {ref['doc_id']} chunk {ref.get('chunk')}")
        else:
            lines.append(json.dumps(ref, ensure_ascii=False))
    return lines


# ---------------------------------------------------------------------------------------------
# Memory commands


def cmd_add(ctx: Ctx) -> int:
    a = ctx.args
    refs = [json.loads(r) for r in a.ref] if a.ref else None
    m = store.add_memory(
        ctx.db,
        a.text,
        a.kind,
        project=ctx.project_ref(),
        scope=identity.scope_for(ctx.here, a.scope),
        source="cli",
        tags=a.tag or (),
        refs=refs,
        anchor=a.anchor,
    )
    ctx.out.record(memory_record(ctx, m), lambda r: f"stored {r['id']} ({r['kind']})")
    return 0


def cmd_list(ctx: Ctx) -> int:
    a = ctx.args
    memories = store.list_memories(
        ctx.db, ctx.list_view(), kind=a.kind, tag=a.tag, since=parse_since(a.since), limit=a.limit
    )
    ctx.out.records(
        [memory_record(ctx, m) for m in memories],
        memory_columns(ctx),
        md=lambda rs: markdown_memories(ctx, rs),
    )
    return 0


def markdown_memories(ctx: Ctx, records: Sequence[Record]) -> str:
    return "\n".join(md_line(r) for r in records)


def md_line(r: Record) -> str:
    """§10.5: '- [id] date — content #tags', continuation lines indented by two spaces."""
    tags = " ".join(f"#{t}" for t in r["tags"])
    content = r["content"].replace("\n", "\n  ")
    line = f"- [{r['id']}] {r['updated_at_iso'][:10]} — {content}"
    return f"{line} {tags}" if tags else line


def cmd_search(ctx: Ctx) -> int:
    a = ctx.args
    from survivemem import embed  # httpx is only needed when searching

    vector = embed.query_vector(ctx.db, a.query, ctx.cfg)
    hits = store.search(ctx.db, a.query, ctx.search_view(), limit=a.limit, vector=vector)
    records = []
    for h in hits:
        data = hit_json(h)
        data["project_label"] = ctx.label(h.memory.project)
        records.append(data)
    columns = memory_columns(ctx)
    columns.insert(1, Column("score", lambda r: f"{r['score']:.4f}"))
    if a.explain:
        columns.insert(2, Column("bm25", lambda r: _num(r.get("bm25"))))
        columns.insert(3, Column("vec", lambda r: _num(r.get("vec_distance"))))
        columns.insert(4, Column("by", lambda r: "+".join(r["matched_by"])))
    ctx.out.records(records, columns, md=lambda rs: markdown_memories(ctx, rs), empty="(no hits)")
    return 0


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def cmd_forget(ctx: Ctx) -> int:
    memories = [store.get_memory(ctx.db, ref) for ref in ctx.args.ids]
    if not ctx.confirm(f"Forget {len(memories)} memor{'y' if len(memories) == 1 else 'ies'}?"):
        return EXIT_USAGE
    forgotten = [store.forget_memory(ctx.db, m.id) for m in memories]
    ctx.out.records([memory_record(ctx, m) for m in forgotten], memory_columns(ctx))
    return 0


REVIEW_KINDS = {
    "f": "fact",
    "p": "preference",
    "d": "decision",
    "t": "pitfall",
    "v": "environment",
    "j": "project",
}
REVIEW_MENU = (
    "f fact · p preference · d decision · t pitfall · v environment · j project"
    " · e edit · x discard · s skip · q quit"
)


def cmd_review(ctx: Ctx) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise UsageError("review is interactive; script with list --kind candidate and edit --kind")
    candidates = store.list_memories(ctx.db, ctx.list_view(), kind="candidate")
    tally = review(ctx, candidates, input, print)
    print(", ".join(f"{k}: {v}" for k, v in tally.items()) or "no candidates")
    return 0


def review(
    ctx: Ctx, candidates: Sequence[Memory], ask: Callable[[str], str], say: Callable[[str], None]
) -> dict[str, int]:
    tally = {"promoted": 0, "discarded": 0, "skipped": 0}
    for i, m in enumerate(candidates, 1):
        say(
            f"\n[{i}/{len(candidates)}] {m.id[:8]}  {ctx.label(m.project)}  "
            f"{relative(m.created_at)}\n{m.content}\n{REVIEW_MENU}"
        )
        outcome = review_one(ctx, m, ask, say)
        if outcome == "quit":
            break
        tally[outcome] += 1
    return tally


def review_one(ctx: Ctx, m: Memory, ask: Callable[[str], str], say: Callable[[str], None]) -> str:
    content = m.content
    while True:
        key = ask("> ").strip().lower()[:1]
        if key in REVIEW_KINDS:
            store.edit_memory(ctx.db, m.id, store.Edit(content=content, kind=REVIEW_KINDS[key]))
            return "promoted"
        if key == "e":
            content = ask("new text: ").strip() or content
            say(content)
        elif key == "x":
            store.forget_memory(ctx.db, m.id)
            return "discarded"
        elif key in ("s", ""):
            return "skipped"
        elif key == "q":
            return "quit"


# ---------------------------------------------------------------------------------------------
# Clones and projects here


def checkout_record(ctx: Ctx, c: Checkout) -> Record:
    data: Record = to_json(c)
    data["project_label"] = ctx.label(c.project_id)
    return data


CHECKOUT_COLUMNS = [
    Column("id", lambda r: r["id"]),
    Column("project", lambda r: r["project_label"]),
    Column("path", lambda r: identity.abbrev_home(r["path"])),
    Column("label", lambda r: r["label"] or "-"),
    Column("branch", lambda r: r["last_branch"] or "-"),
    Column("head", lambda r: (r["last_head"] or "-")[:7]),
    Column(".config", lambda r: (r["last_config"] or "-")[:8]),
    Column("last seen", lambda r: relative(r["last_seen"])),
    Column("present", lambda r: "yes" if r["present"] else "gone"),
]


def cmd_clones(ctx: Ctx) -> int:
    project = None if ctx.flag("all") else ctx.project_ref()
    if project is None and not ctx.flag("all"):
        raise UsageError("not inside a project; use --project X or --all")
    rows = [checkout_record(ctx, c) for c in store.list_checkouts(ctx.db, project)]
    ctx.out.records(rows, CHECKOUT_COLUMNS)
    return 0


def cmd_clones_label(ctx: Ctx) -> int:
    c = store.label_checkout(ctx.db, ctx.args.id, ctx.args.text)
    ctx.out.record(checkout_record(ctx, c), lambda r: f"{r['id']} labelled {r['label']!r}")
    return 0


def cmd_clones_prune(ctx: Ctx) -> int:
    gone = [c for c in store.list_checkouts(ctx.db) if c.present and not Path(c.path).exists()]
    for c in gone:
        store.mark_checkout_gone(ctx.db, c.id)
    purged = 0
    if ctx.args.purge and ctx.confirm("Delete vanished clones that have no scoped memories?"):
        purged = store.purge_checkouts(ctx.db)
    result = {"marked_gone": [c.id for c in gone], "purged": purged}
    ctx.out.record(result, lambda r: f"marked gone: {len(gone)}, purged: {purged}")
    return 0


def cmd_clones_diff(ctx: Ctx) -> int:
    a, b = (store.get_checkout(ctx.db, ref) for ref in (ctx.args.a, ctx.args.b))
    sides = [checkout_record(ctx, c) for c in (a, b)]
    for side, c in zip(sides, (a, b), strict=True):
        view = store.View((c.project_id,), False, (f"checkout:{c.id}",))
        side["memories"] = [memory_json(m) for m in store.list_memories(ctx.db, view)]
    ctx.out.record({"a": sides[0], "b": sides[1]}, render_clone_diff)
    return 0


def render_clone_diff(r: Record) -> str:
    rows = [
        ("path", "path"),
        ("label", "label"),
        ("branch", "last_branch"),
        ("head", "last_head"),
        (".config", "last_config"),
    ]
    width = max(len(str(r["a"].get(k) or "-")) for _, k in rows)
    lines = [
        f"{name:8} {r['a'].get(k) or '-'!s:{width}}  {r['b'].get(k) or '-'}" for name, k in rows
    ]
    for side in ("a", "b"):
        lines.append(f"\nmemories scoped to {r[side]['id']}:")
        lines += [f"  [{m['id'][:8]}] {m['content']}" for m in r[side]["memories"]] or ["  -"]
    return "\n".join(lines)


def cmd_projects_here(ctx: Ctx) -> int:
    res = ctx.here
    loc = res.location
    remotes = []
    for name, url in loc.remotes if loc else ():
        host = identity.ssh_host(url)
        resolved = identity.ssh_resolve(host) if host else None
        remotes.append(
            {
                "name": name,
                "url": url,
                "resolved": f"{resolved[0]}:{resolved[1]}" if resolved else None,
                "identity": "remote:"
                + identity.normalise_remote_url(
                    url, base=loc.root if loc else None, hosts=ctx.cfg.hosts
                ),
            }
        )
    data = {
        "cwd": str(Path.cwd()),
        "root": str(loc.root) if loc else None,
        "common_dir": str(loc.common_dir) if loc and loc.common_dir else None,
        "step": res.step,
        "project": to_json(res.project),
        "identities": list(res.identities),
        "remotes": remotes,
        "root_commit": res.root,
        "family": res.family,
        "hint": res.hint,
        "checkout": to_json(res.checkout) if res.checkout else None,
        "checkout_line": identity.checkout_line(ctx.db, res, always=True),
    }
    ctx.out.record(data, render_here)
    return 0


def render_here(r: Record) -> str:
    if r["project"] is None:
        return f"{r['cwd']}: not in a project — global scope only"
    lines = [
        f"project   {r['project']['label']} ({r['project']['id']})  matched by: {r['step']}",
        f"root      {r['root']}",
        f"git dir   {r['common_dir'] or '-'}",
    ]
    lines += [
        f"remote    {x['name']}: {x['url']}  →  {x['resolved'] or '-'}  →  {x['identity']}"
        for x in r["remotes"]
    ]
    lines += [f"identity  {i}" for i in r["identities"]]
    if r["family"]:
        lines.append(f"family    {r['family']}")
    if r["checkout_line"]:
        lines.append(f"clone     {r['checkout_line']}")
    if r["hint"]:
        lines.append(f"hint      {r['hint']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Session state (§16)


def _session_id(ctx: Ctx) -> str:
    return str(ctx.args.session or store.current_session(ctx.db, ctx.here.project_id))


def state_record(sid: str, found: SessionState | None) -> Record:
    if found is None:
        return {
            "session_id": sid,
            "project": None,
            "state": {},
            "updated_at": None,
            "updated_at_iso": None,
        }
    data: Record = to_json(found)
    return data


def render_state_record(r: Record) -> str:
    return state.render(r["state"]) or f"(no state for session {r['session_id']})"


def cmd_state_show(ctx: Ctx) -> int:
    sid = _session_id(ctx)
    ctx.out.record(state_record(sid, store.get_state(ctx.db, sid)), render_state_record)
    return 0


def cmd_state_set(ctx: Ctx) -> int:
    a = ctx.args
    sid = _session_id(ctx)
    old = store.get_state(ctx.db, sid)
    new = state.checkpoint(
        old.state if old else {},
        goal=a.goal,
        done=a.done,
        in_progress=a.in_progress,
        next_=a.next,
        decisions=a.decision,
        files=a.file or (),
        blockers=a.blocker,
    )
    saved = store.save_state(ctx.db, sid, ctx.here.project_id, new)
    ctx.out.record(state_record(sid, saved), render_state_record)
    return 0


def cmd_state_purge(ctx: Ctx) -> int:
    everything = ctx.args.all_sessions
    what = "all session states" if everything else "session states older than 14 days"
    if not ctx.confirm(f"Delete {what}?"):
        return EXIT_USAGE
    cutoff = dbmod.now_ms() + 1 if everything else dbmod.now_ms() - state.TTL_MS
    purged = store.purge_states(ctx.db, cutoff)
    ctx.out.record({"purged": purged}, lambda r: f"purged {r['purged']} session states")
    return 0


# ---------------------------------------------------------------------------------------------
# Maintenance

REINDEX_BATCH = 32


def cmd_reindex(ctx: Ctx) -> int:
    from survivemem import embed

    store.rebuild_fts(ctx.db)
    result: Record = {"fts": "rebuilt", "vec": "disabled", "embedded": 0}
    if not embed.load_extension(ctx.db):
        result["reason"] = "sqlite-vec is not installed (uv tool install 'survivemem[vec]')"
    elif not embed.embed(["probe"], ctx.cfg, timeout=embed.BATCH_TIMEOUT):
        result["reason"] = (
            f"Ollama at {ctx.cfg.ollama_url} did not embed with {ctx.cfg.embed_model}"
        )
    else:
        store.create_vec_table(ctx.db)
        store.clear_vectors(ctx.db)
        while done := embed.backfill(ctx.db, ctx.cfg, REINDEX_BATCH):
            result["embedded"] += done
        result["vec"] = "rebuilt"
    ctx.out.record(result, render_reindex)
    return 0


def render_reindex(r: Record) -> str:
    vec = (
        f"vec {r['vec']}, {r['embedded']} embedded"
        if r["vec"] == "rebuilt"
        else (f"vec disabled: {r.get('reason')}")
    )
    return f"fts rebuilt; {vec}"


# ---------------------------------------------------------------------------------------------
# Integration


def cmd_hook(ctx: Ctx) -> int:
    return hooks.run(ctx.args.event, sys.stdin, sys.stdout)


def cmd_serve(ctx: Ctx) -> int:
    from survivemem import logfile, mcp_server  # the MCP SDK is heavy; hooks must not import it

    logfile.configure()
    try:
        mcp_server.serve()
    except Exception:  # top-level boundary of serve: log with traceback, never crash the client
        logging.getLogger(__name__).exception("survivemem serve failed")
    return 0


# ---------------------------------------------------------------------------------------------
# Parser


def _common() -> argparse.ArgumentParser:
    p = Parser(add_help=False)
    s = argparse.SUPPRESS
    p.add_argument("--json", action="store_const", const="json", dest="fmt", default=s)
    p.add_argument("--jsonl", action="store_const", const="jsonl", dest="fmt", default=s)
    p.add_argument("--format", choices=FORMATS, dest="fmt", default=s)
    p.add_argument("--quiet", "-q", action="store_true", default=s)
    p.add_argument("--wide", action="store_true", default=s)
    p.add_argument("--no-color", action="store_true", default=s)
    return p


def _filters() -> argparse.ArgumentParser:
    p = Parser(add_help=False)
    s = argparse.SUPPRESS
    p.add_argument("--project", metavar="NAME|ID", default=s)
    p.add_argument("--global", dest="global_", action="store_true", default=s)
    p.add_argument("--all", action="store_true", default=s)
    return p


Command = Callable[[Ctx], int]


def _add(
    sub: Any, name: str, func: Command, help_: str, parents: Sequence[argparse.ArgumentParser]
) -> argparse.ArgumentParser:
    p: argparse.ArgumentParser = sub.add_parser(
        name, help=help_, description=help_, parents=list(parents)
    )
    p.set_defaults(func=func)
    return p


def _memory_commands(
    sub: Any, common: argparse.ArgumentParser, filters: argparse.ArgumentParser
) -> None:
    both = (common, filters)
    p = _add(sub, "add", cmd_add, "store a memory", both)
    p.add_argument("text")
    p.add_argument("--kind", default="fact", choices=[k for k in dbmod.KINDS if k != "candidate"])
    p.add_argument("--tag", action="append")
    p.add_argument("--scope", help="project (default), branch, checkout")
    p.add_argument("--ref", action="append", help="JSON ref object (§15), repeatable")
    p.add_argument("--anchor", help="commit / file hash the fact depends on")
    p = _add(sub, "list", cmd_list, "list memories, newest first", both)
    p.add_argument("--kind", choices=dbmod.KINDS)
    p.add_argument("--tag")
    p.add_argument("--since", help="30m, 12h, 7d, 2w")
    p.add_argument("--limit", type=int)
    p = _add(sub, "search", cmd_search, "relevance-ranked search", both)
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--explain", action="store_true", help="print bm25 / vec score per hit")
    _add(sub, "review", cmd_review, "promote or discard candidates (interactive)", (filters,))
    p = _add(sub, "forget", cmd_forget, "soft-delete memories", (common,))
    p.add_argument("ids", nargs="+")
    p.add_argument("--yes", action="store_true")


def _clone_commands(
    sub: Any, common: argparse.ArgumentParser, filters: argparse.ArgumentParser
) -> None:
    p = _add(sub, "clones", cmd_clones, "checkouts of a project", (common, filters))
    clones = p.add_subparsers(dest="clones_cmd", parser_class=Parser)
    q = _add(clones, "label", cmd_clones_label, "name a clone", (common,))
    q.add_argument("id")
    q.add_argument("text")
    q = _add(clones, "prune", cmd_clones_prune, "mark vanished clones", (common,))
    q.add_argument("--purge", action="store_true", help="delete gone clones without memories")
    q.add_argument("--yes", action="store_true")
    q = _add(clones, "diff", cmd_clones_diff, "compare two clones", (common,))
    q.add_argument("a")
    q.add_argument("b")


def _project_commands(
    sub: Any, common: argparse.ArgumentParser, filters: argparse.ArgumentParser
) -> None:
    p = _add(sub, "projects", cmd_projects_here, "projects", (common,))
    projects = p.add_subparsers(dest="projects_cmd", parser_class=Parser)
    _add(projects, "here", cmd_projects_here, "resolution chain for $PWD", (common,))


def _state_commands(sub: Any, common: argparse.ArgumentParser) -> None:
    p = _add(sub, "state", cmd_state_show, "compaction-proof session checkpoint", (common,))
    p.add_argument("--session")
    states = p.add_subparsers(dest="state_cmd", parser_class=Parser)
    q = _add(states, "show", cmd_state_show, "show the current session's state", (common,))
    q.add_argument("--session")
    q = _add(states, "set", cmd_state_set, "update the current session's state", (common,))
    q.add_argument("--session")
    q.add_argument("--goal")
    q.add_argument("--in-progress")
    for flag in ("--done", "--next", "--decision", "--blocker"):
        q.add_argument(flag, action="append", help="repeatable; replaces the stored list")
    q.add_argument("--file", action="append", help="repeatable; merged into the stored files")
    q = _add(states, "purge", cmd_state_purge, "delete states older than 14 days", (common,))
    q.add_argument("--all", dest="all_sessions", action="store_true", help="delete every state")
    q.add_argument("--yes", action="store_true")


def build_parser() -> Parser:
    parser = Parser(prog="survivemem", add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("--help", "-h", action="store_true")
    parser.add_argument("--version", action=VersionAction, nargs=0)
    common, filters = _common(), _filters()
    sub = parser.add_subparsers(dest="command", parser_class=Parser)
    _memory_commands(sub, common, filters)
    _clone_commands(sub, common, filters)
    _project_commands(sub, common, filters)
    _state_commands(sub, common)
    _add(sub, "reindex", cmd_reindex, "rebuild FTS; recompute embeddings if vec enabled", (common,))
    _add(sub, "serve", cmd_serve, "MCP stdio server", ())
    p = _add(sub, "hook", cmd_hook, "Claude Code hook handler (reads JSON on stdin)", ())
    p.add_argument("event", choices=sorted(hooks.HANDLERS))
    help_p = sub.add_parser("help", add_help=False)
    help_p.add_argument("topic", nargs="*")
    return parser


def output_for(args: argparse.Namespace) -> Output:
    fmt = getattr(args, "fmt", None) or ("table" if sys.stdout.isatty() else "json")
    return Output(
        fmt,
        quiet=getattr(args, "quiet", False),
        wide=getattr(args, "wide", False),
        no_color=getattr(args, "no_color", False),
    )


def _help(parser: Parser, topic: Sequence[str]) -> int:
    if not topic:
        sys.stdout.write(HELP)
        return 0
    try:
        parser.parse_args([*topic, "--help"])
    except SystemExit as e:
        return int(e.code or 0)
    return 0


ERRORS: tuple[tuple[type[BaseException], int], ...] = (
    (UsageError, EXIT_USAGE),
    (ConfigError, EXIT_USAGE),
    (store.Invalid, EXIT_USAGE),
    (json.JSONDecodeError, EXIT_USAGE),
    (store.NotFound, EXIT_NOT_FOUND),
    (store.Ambiguous, EXIT_AMBIGUOUS),
    (dbmod.DBError, EXIT_DB),
    (sqlite3.Error, EXIT_DB),
)


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    machine = "--json" in (argv or sys.argv) or "--jsonl" in (argv or sys.argv)
    try:
        args = parser.parse_args(argv)
        if args.command == "help" or (args.help and not args.command):
            return _help(parser, getattr(args, "topic", []))
        if args.command is None:
            sys.stdout.write(HELP)
            return EXIT_USAGE
        out = output_for(args)
        machine = out.is_machine
        return int(args.func(Ctx(args, out)))
    except tuple(e for e, _ in ERRORS) as e:
        code = next(c for t, c in ERRORS if isinstance(e, t))
        emit_error(code, str(e), machine=machine)
        return code


def main() -> None:
    if sys.platform != "linux":
        sys.stderr.write("survivemem: Linux only\n")
        raise SystemExit(1)
    raise SystemExit(run())
