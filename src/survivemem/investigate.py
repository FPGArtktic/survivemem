# SPDX-License-Identifier: GPL-3.0-or-later
"""Investigations (§14): normalisation, loop guard and detection, rendering, postmortem."""

import hashlib
import json
import re
import shutil
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from survivemem import identity, ingest, paths, store
from survivemem.model import Checkout, Entry, Evidence, Investigation, Memory, iso

BUDGET = 2500
SYMPTOM_CHARS = 300
HEADER_CHARS = 200  # SETUP and CLONE: what compaction loses first, never shrunk further
RULED_OUT_KEEP = 40
LAST_EXPERIMENTS = 5
OPEN_HYPOTHESES = 10
DO_NOT_RERUN = 10
LOOP_THRESHOLD = 3
LOOP_PREFIX = "LOOP: "
BLOCKER_PREFIX = "BLOCKER: "
ITEM_LIMITS = (160, 120, 90, 70, 50, 40, 30)
TIGHT_ITEM_CHARS = 20
CLOSING_LINE = "Call investigation_status before the next experiment."

# ---------------------------------------------------------------------------------------------
# Normalisation (loop guard key)

LEADING = (
    re.compile(r"^cd\s+[^;&|]+?\s*&&\s*"),
    re.compile(r"^sudo(\s+-\S+)*\s+"),
    re.compile(r"^env(\s+-\S+)*(\s+\w+=\S*)*\s+"),
    re.compile(r"^(\w+=(?:'[^']*'|\"[^\"]*\"|\S*)\s+)+"),
)
REDIRECT = re.compile(r"\s+2>&1(?=\s|$)")
PIPE_TAIL = re.compile(r"\s*\|\s*(tee|head|tail)\b[^|]*")
TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T_ ]\d{2}[:\-]?\d{2}(?:[:\-]?\d{2})?)?|\b\d{8}[-_]\d{4,6}\b"
)


def normalise_command(command: str, workspace: Path | None = None) -> str:
    """Strip cd/sudo/env/VAR= prefixes, timestamps, 2>&1 and tee/head/tail; workspace → $WS,
    $HOME → ~; collapse whitespace. Arguments and flags are kept."""
    cmd = command.strip()
    changed = True
    while changed:
        before = cmd
        for pattern in LEADING:
            cmd = pattern.sub("", cmd)
        changed = cmd != before
    cmd = PIPE_TAIL.sub("", REDIRECT.sub("", cmd))
    cmd = TIMESTAMP.sub("", cmd)
    if workspace is not None:
        cmd = cmd.replace(str(workspace), "$WS")
    cmd = cmd.replace(str(Path.home()) + "/", "~/")
    return " ".join(cmd.split())


def checkout_anchor(c: Checkout | None) -> str | None:
    """'<head sha>[+<config sha8>]' (§9 anchor convention for config-dependent repos)."""
    if c is None or not c.last_head:
        return None
    return c.last_head + (f"+{c.last_config[:8]}" if c.last_config else "")


def current_anchor(res: identity.Resolution) -> str | None:
    return checkout_anchor(res.checkout)


def short_anchor(anchor: str | None) -> str:
    if not anchor:
        return "-"
    head, _, config = anchor.partition("+")
    return head[:7] + (f"+{config[:4]}" if config else "")


def workspace_of(res: identity.Resolution) -> Path | None:
    return res.location.root if res.location else None


# ---------------------------------------------------------------------------------------------
# Loop guard (PreToolUse) and loop detection (PostToolUse)


def _hypothesis_after(entries: Sequence[Entry], seq: int) -> bool:
    return any(e.kind == "hypothesis" and e.seq > seq for e in entries)


@dataclass(frozen=True, slots=True)
class Verdict:
    blocking: bool
    entry: Entry
    message: str


def guard(
    entries: Sequence[Entry], command: str, anchor: str | None, evidence: dict[str, Evidence]
) -> Verdict | None:
    """A failed experiment with this command at this anchor, not answered by a newer
    hypothesis, blocks; any other earlier run is reported as seen."""
    runs = [e for e in entries if e.kind == "experiment" and e.command == command]
    if not runs:
        return None
    failed = [e for e in runs if e.result == "bad" and e.anchor == anchor]
    if failed and not _hypothesis_after(entries, failed[-1].seq):
        e = failed[-1]
        ev = evidence.get(e.evidence_id or "")
        day = (iso(e.created_at) or "")[:10]
        message = (
            f"LOOP GUARD: identical to experiment #{e.seq} ({day}, "
            f"result bad, evidence {Path(ev.path).name if ev else 'none'}). Log a new "
            "hypothesis or change the anchor before re-running."
        )
        return Verdict(True, e, message)
    e = runs[-1]
    return Verdict(
        False, e, f"seen as #{e.seq} ({e.result or 'no result'}, anchor {short_anchor(e.anchor)})"
    )


def loop_note(entries: Sequence[Entry], command: str, excerpt_sha: str | None) -> str | None:
    """'LOOP: <command> failed identically ×N — #a, #b, #c' once the same failure has been
    seen three times, unless a hypothesis or a loop note for it came after the last one."""
    if not excerpt_sha:
        return None
    same = [
        e
        for e in entries
        if e.kind == "experiment"
        and e.command == command
        and e.excerpt_sha == excerpt_sha
        and e.result == "bad"
    ]
    if len(same) < LOOP_THRESHOLD:
        return None
    marker = f"{LOOP_PREFIX}{command} "
    since = max((e.seq for e in entries if e.kind == "hypothesis"), default=0)
    if any(e.kind == "note" and e.text.startswith(marker) and e.seq > since for e in entries):
        return None
    seqs = ", ".join(f"#{e.seq}" for e in same[-LOOP_THRESHOLD:])
    return f"{LOOP_PREFIX}{command} failed identically ×{len(same)} — {seqs}"


def blocker(entries: Sequence[Entry]) -> str | None:
    """The last LOOP/BLOCKER note, until a hypothesis is logged after it."""
    notes = [
        e for e in entries if e.kind == "note" and e.text.startswith((LOOP_PREFIX, BLOCKER_PREFIX))
    ]
    if not notes or _hypothesis_after(entries, notes[-1].seq):
        return None
    return notes[-1].text.removeprefix(BLOCKER_PREFIX)


def do_not_rerun(entries: Sequence[Entry], anchor: str | None) -> list[str]:
    bad = [
        e
        for e in entries
        if e.kind == "experiment"
        and e.result == "bad"
        and e.command
        and (anchor is None or e.anchor == anchor)
        and not _hypothesis_after(entries, e.seq)
    ]
    return list(dict.fromkeys(str(e.command) for e in reversed(bad)))[:DO_NOT_RERUN]


def loops(entries: Sequence[Entry]) -> list[dict[str, Any]]:
    """Commands with repeated identical failures: what the guard and detection act on."""
    groups: dict[tuple[str, str | None], list[Entry]] = {}
    for e in entries:
        if e.kind == "experiment" and e.result == "bad" and e.command:
            groups.setdefault((e.command, e.excerpt_sha), []).append(e)
    return [
        {
            "command": cmd,
            "excerpt_sha": sha,
            "failures": len(es),
            "seqs": [e.seq for e in es],
            "anchors": sorted({short_anchor(e.anchor) for e in es}),
            "guarded": not _hypothesis_after(entries, es[-1].seq),
        }
        for (cmd, sha), es in groups.items()
        if len(es) > 1
    ]


# ---------------------------------------------------------------------------------------------
# Rendering (prompt hook, post-compaction, investigation_status, inv show)


@dataclass(frozen=True, slots=True)
class Context:
    inv: Investigation
    entries: Sequence[Entry]
    evidence: dict[str, Evidence]
    clone_line: str | None
    next_steps: Sequence[str]
    anchor: str | None


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def setup_line(env: str | None) -> str:
    try:
        data = json.loads(env) if env else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        return "-"
    parts = [str(x) for x in data.get("environment", []) if isinstance(x, str)]
    heads = data.get("heads", {})
    if isinstance(heads, dict):
        parts += [f"{label}@{short_anchor(str(a))}" for label, a in heads.items()]
    extra = data.get("extra")
    if isinstance(extra, str) and extra.strip():
        parts.append(extra.strip())
    return " ".join(parts) or "-"


def _sections(ctx: Context, limit: int, keep_optional: int, tight: bool = False) -> list[str]:
    inv, entries = ctx.inv, ctx.entries
    header = HEADER_CHARS * 3 // 5 if tight else HEADER_CHARS
    ruled = [e for e in entries if e.kind == "ruled_out"]
    last_ruled = ruled[-1].seq if ruled else 0
    hypotheses = [e for e in entries if e.kind == "hypothesis" and e.seq > last_ruled]
    experiments = [e for e in entries if e.kind == "experiment"][-LAST_EXPERIMENTS:]
    lines = [
        f'INVESTIGATION {inv.id} "{shorten(inv.title, 80)}"  status={inv.status}  '
        f"since {(iso(inv.created_at) or '')[:10]}  entries={len(entries)}",
        f"SYMPTOM   {shorten(inv.symptom, SYMPTOM_CHARS // 2 if tight else SYMPTOM_CHARS)}",
        f"SETUP     {shorten(setup_line(inv.env), header)}",
    ]
    if ctx.clone_line:
        lines.append(f"CLONE     {shorten(ctx.clone_line, header)}")
    older = len(ruled) - RULED_OUT_KEEP
    lines.append(f"RULED OUT ({len(ruled)}, last {RULED_OUT_KEEP})")
    if older > 0:
        lines.append(f"  (+{older} older)")
    lines += [
        f"  - {shorten(e.text, limit)}  [#{e.seq}, {short_anchor(e.anchor)}]"
        for e in ruled[-RULED_OUT_KEEP:]
    ]
    if keep_optional >= 2:
        lines.append(f"OPEN HYPOTHESES ({len(hypotheses)})")
        lines += [
            f"  - {shorten(e.text, limit)}  [#{e.seq}]" for e in hypotheses[-OPEN_HYPOTHESES:]
        ]
    if keep_optional >= 1:
        lines.append(f"LAST EXPERIMENTS ({len(experiments)})")
        lines += [_experiment_line(e, ctx.evidence, limit) for e in experiments]
    lines += _footer(ctx, limit)
    return lines


def _ruled_anchor(anchor: str | None, tight: bool) -> str:
    return (anchor or "-")[:7] if tight else short_anchor(anchor)


def _experiment_line(e: Entry, evidence: dict[str, Evidence], limit: int) -> str:
    ev = evidence.get(e.evidence_id or "")
    tail = f"  evidence={Path(ev.path).name}" if ev else ""
    return (
        f"  - #{e.seq} {e.result or '?':4} {shorten(e.command or e.text, limit)}  "
        f"anchor={short_anchor(e.anchor)}{tail}"
    )


def _footer(ctx: Context, limit: int) -> list[str]:
    lines = []
    block = blocker(ctx.entries)
    if block:
        lines.append(f"BLOCKER   {shorten(block, 300)}")
    if ctx.next_steps:
        lines.append(f"NEXT      {shorten('; '.join(ctx.next_steps), 200)}")
    rerun = do_not_rerun(ctx.entries, ctx.anchor)
    if rerun:
        commands = "; ".join(shorten(c, max(limit, 40)) for c in rerun)
        lines.append(f"DO NOT RE-RUN without a new hypothesis: {commands}")
    return lines


def render(ctx: Context, budget: int = BUDGET) -> str:
    """≤ budget characters. Item texts shrink first, then LAST EXPERIMENTS and OPEN
    HYPOTHESES go, then the header lines shrink. The last 40 RULED OUT entries are only cut
    when even that does not fit, oldest first; the header and footer always stay."""
    for keep in (2, 1, 0):
        for limit in ITEM_LIMITS:
            text = "\n".join(_sections(ctx, limit, keep))
            if len(text) <= budget:
                return text
    lines = _sections(ctx, TIGHT_ITEM_CHARS, 0, tight=True)
    while len("\n".join(lines)) > budget:
        ruled = next((i for i, x in enumerate(lines) if x.startswith("  - ")), None)
        if ruled is None:
            break
        del lines[ruled]
    return "\n".join(lines)[:budget]


def one_liner(inv: Investigation) -> str:
    return (
        f'investigation {inv.id} "{shorten(inv.title, 80)}" {inv.status} — '
        "call investigation_status for details"
    )


# ---------------------------------------------------------------------------------------------
# Operations shared by the CLI, the MCP tools and the hooks


def context(
    db: sqlite3.Connection,
    inv: Investigation,
    res: identity.Resolution,
    next_steps: Sequence[str] = (),
) -> Context:
    clone = None
    if inv.checkout_id:
        try:
            opened = store.get_checkout(db, inv.checkout_id)
        except store.NotFound:
            opened = None
        if opened is not None:
            here = res.checkout is not None and res.checkout.id == opened.id
            others = [
                c for c in store.list_checkouts(db, inv.project) if c.present and c.id != opened.id
            ]
            where = "opened in this clone" if here else "opened in another clone"
            clone = (
                f"{identity.describe_checkout(opened)} · {where}; {len(others)} other clones exist"
            )
    evidence = {e.id: e for e in store.evidence_of(db, inv.id)}
    return Context(
        inv, store.entries_of(db, inv.id), evidence, clone, list(next_steps), current_anchor(res)
    )


def environment_snapshot(
    db: sqlite3.Connection, res: identity.Resolution, extra: str | None = None
) -> dict[str, Any]:
    """`environment` memories of the project (and linked ones) plus each repo's head."""
    project = res.project_id
    view = store.visible_view(db, project)
    env = [m.content for m in store.list_memories(db, view, kind="environment")]
    labels = store.project_labels(db)
    heads = {}
    for pid in (project, *store.linked_projects(db, project)) if project else ():
        anchors = [checkout_anchor(c) for c in store.list_checkouts(db, pid) if c.present]
        found = next((a for a in anchors if a), None)
        if found:
            heads[labels.get(pid, pid)] = found
    snapshot: dict[str, Any] = {"environment": env, "heads": heads}
    if extra:
        snapshot["extra"] = extra
    return snapshot


def require_open(db: sqlite3.Connection, res: identity.Resolution) -> Investigation:
    inv = store.open_investigation_of(db, res.project_id)
    if inv is None:
        raise store.NotFound("no open investigation in this project; open one first")
    return inv


def open_new(
    db: sqlite3.Connection,
    res: identity.Resolution,
    title: str,
    symptom: str,
    extra_env: str | None = None,
) -> Investigation:
    if res.project_id is None:
        raise store.Invalid("investigations belong to a project; run inside a repository")
    return store.open_investigation(
        db,
        res.project_id,
        title,
        symptom,
        checkout_id=res.checkout.id if res.checkout else None,
        env=environment_snapshot(db, res, extra_env),
    )


def log_entry(
    db: sqlite3.Connection, res: identity.Resolution, inv: Investigation, new: store.NewEntry
) -> Entry:
    """Normalise the command and default an experiment's anchor to the current clone."""
    command = normalise_command(new.command, workspace_of(res)) if new.command else None
    anchor = new.anchor or (current_anchor(res) if new.kind == "experiment" else None)
    return store.add_entry(
        db,
        inv.id,
        store.NewEntry(
            new.kind,
            new.text,
            command,
            new.result,
            anchor,
            new.excerpt_sha,
            new.refs,
            new.evidence_id,
        ),
    )


def close(
    db: sqlite3.Connection,
    res: identity.Resolution,
    inv: Investigation,
    root_cause: str,
    fix: str,
    status: str = "solved",
) -> tuple[Investigation, Memory | None]:
    """Close; a solved investigation is distilled into one pitfall memory."""
    if status not in ("solved", "abandoned"):
        raise store.Invalid("close with status solved or abandoned")
    if not root_cause.strip() or not fix.strip():
        raise store.Invalid("closing needs a root cause and a fix")
    closed = store.set_investigation_status(db, inv.id, status, root_cause=root_cause, fix=fix)
    if status != "solved":
        return closed, None
    content = f"{shorten(inv.symptom, 200)} → root cause: {root_cause} → fix: {fix}"
    pitfall = store.add_memory(
        db,
        content,
        "pitfall",
        project=inv.project,
        source="investigation",
        tags=("investigation",),
        refs=[{"type": "investigation", "id": inv.id}],
        anchor=current_anchor(res),
    )
    return closed, pitfall


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def attach(
    db: sqlite3.Connection, inv: Investigation, path: Path, tool: str | None, copy: bool = True
) -> Evidence:
    """Copy (unless --no-copy) under the evidence dir, hash, extract an excerpt."""
    data = path.read_bytes()
    text = data.decode("utf-8", errors="replace")
    tool = tool or ingest.detect_tool(path.name)
    extract = ingest.extract(text, tool if tool in ingest.EXTRACTORS else "other")
    target = path
    if copy:
        folder = paths.evidence_dir(inv.id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{len(store.evidence_of(db, inv.id)) + 1:03d}-{path.name}"
        shutil.copyfile(path, target)
    return store.add_evidence(
        db, inv.id, str(target), hashlib.sha256(data).hexdigest(), len(data), tool, extract.excerpt
    )


# ---------------------------------------------------------------------------------------------
# Bisect table and postmortem


def bisect(entries: Sequence[Entry]) -> dict[str, Any]:
    """Experiments with anchor and result in order, and the last-good / first-bad boundary."""
    runs = [e for e in entries if e.kind == "experiment" and e.anchor and e.result]
    good = [e for e in runs if e.result == "good"]
    bad = [e for e in runs if e.result == "bad"]
    boundary = None
    if good and bad:
        first_bad = next((e for e in bad if e.seq > good[-1].seq), bad[0])
        boundary = {"last_good": _bisect_row(good[-1]), "first_bad_after": _bisect_row(first_bad)}
    return {"experiments": [_bisect_row(e) for e in runs], "boundary": boundary}


def _bisect_row(e: Entry) -> dict[str, Any]:
    return {"seq": e.seq, "anchor": e.anchor, "result": e.result, "command": e.command}


def postmortem(db: sqlite3.Connection, inv: Investigation) -> str:
    """Stand-alone Markdown: the artefact that goes into a tracker or wiki."""
    entries = store.entries_of(db, inv.id)
    lines = [
        f"# {inv.title}",
        "",
        f"Investigation `{inv.id}` · status **{inv.status}** · "
        f"opened {iso(inv.created_at)} · closed {iso(inv.closed_at) or '-'}",
        "",
        "## Symptom",
        "",
        inv.symptom,
        "",
        "## Setup under test",
        "",
        setup_line(inv.env),
        "",
        "## Timeline",
        "",
        "| # | kind | result | text | anchor |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {e.seq} | {e.kind} | {e.result or ''} | {_cell(e.text, e.command)} | "
        f"{short_anchor(e.anchor)} |"
        for e in entries
    ]
    lines += ["", "## Ruled out", ""]
    lines += [f"- {e.text} (#{e.seq})" for e in entries if e.kind == "ruled_out"] or ["- none"]
    lines += [
        "",
        "## Root cause",
        "",
        inv.root_cause or "-",
        "",
        "## Fix",
        "",
        inv.fix or "-",
        "",
        "## Evidence",
        "",
    ]
    lines += [
        f"- `{Path(ev.path).name}` sha256 `{ev.sha256}` ({ev.tool or 'other'}, {ev.bytes} bytes)"
        for ev in store.evidence_of(db, inv.id)
    ] or ["- none"]
    return "\n".join(lines)


def _cell(text: str, command: str | None) -> str:
    value = f"{text} — `{command}`" if command else text
    return " ".join(value.split()).replace("|", "\\|")
