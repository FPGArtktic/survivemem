# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code hook handlers (§9).

stdout belongs to the hook contract: plain text for UserPromptSubmit and SessionStart, JSON for
PreToolUse/PostToolUse. Everything else goes to hooks.log. A hook never fails: the boundary in
run() logs the traceback and exits 0.
"""

import fnmatch
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, TypedDict, cast

from survivemem import db as dbmod
from survivemem import identity, ingest, investigate, logfile, state, store, transcript
from survivemem.config import Config, ConfigError, load_config
from survivemem.model import Checkout, Hit, Investigation
from survivemem.redact import contains_secret

log = logging.getLogger(__name__)

PROMPT_BUDGET = 6000
SESSION_START_MEMORY_BUDGET = 3000
INVESTIGATION_EVERY = 20
POSTTOOL_CANDIDATE_CAP = 50
MCP_DIGEST_CHARS = 300
MCP_REFS = 5
EXIT_CODE = re.compile(r"Exit code (\d+)")
STOP_EMBED_BATCH = 16
STOP_EMBED_TIMEOUT = 2.0
RECORD_LIMIT = 400
SEARCH_LIMIT = 8
CANDIDATE_PROMPT_CHARS = 300
CANDIDATE_ANSWER_CHARS = 600
ERROR_LIKE = re.compile(
    r"\bERROR\b|failed|\bOops\b|\bpanic|timing|slack|Critical Warning|Error \(|Fitter", re.I
)


# Added to the payload by `survivemem hook … --remote` (Profile B): what the daemon cannot see.
PROBE_KEY = "_survivemem_probe"
TRANSCRIPT_KEY = "_survivemem_transcript"


class HookInput(TypedDict, total=False):
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    prompt: str
    stop_hook_active: bool
    last_assistant_message: str
    trigger: str
    source: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_response: Any
    tool_use_id: str
    error: str


@dataclass(frozen=True, slots=True)
class Session:
    db: sqlite3.Connection
    cfg: Config
    res: identity.Resolution


def text_field(payload: HookInput, key: str) -> str:
    value = cast(dict[str, Any], payload).get(key)
    return value if isinstance(value, str) else ""


def read_payload(stream: TextIO) -> HookInput:
    raw: Any = json.loads(stream.read() or "{}")
    if not isinstance(raw, dict):
        raise ValueError(f"hook payload is {type(raw).__name__}, expected an object")
    return cast(HookInput, raw)


def config() -> Config:
    try:
        return load_config()
    except ConfigError as e:
        log.error("invalid config, using defaults: %s", e)
        return Config()


@contextmanager
def session(payload: HookInput) -> Iterator[Session]:
    cfg = config()
    cwd = Path(text_field(payload, "cwd") or os.getcwd())
    sent = cast(dict[str, Any], payload).get(PROBE_KEY)
    observed = identity.probe_from_json(sent) if isinstance(sent, dict) else None
    with closing(dbmod.connect()) as db:
        yield Session(db, cfg, identity.resolve_tolerant(db, cwd, cfg, observed))


def transcript_entries(payload: HookInput) -> list[transcript.Entry]:
    """The transcript tail, or the compacted copy a remote hook client sent along."""
    sent = cast(dict[str, Any], payload).get(TRANSCRIPT_KEY)
    if isinstance(sent, list):
        return [e for e in sent if isinstance(e, dict)]
    path = text_field(payload, "transcript_path")
    return transcript.tail(Path(path)) if path else []


# ---------------------------------------------------------------------------------------------
# Rendering


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def hit_line(hit: Hit, checkouts: dict[str, Checkout]) -> str:
    m = hit.memory
    suffix = identity.scope_suffix(m.scope, checkouts)
    tail = f" {suffix} ({m.id[:8]})" if suffix else f" ({m.id[:8]})"
    head = f"- [{m.kind}] "
    return head + shorten(m.content, RECORD_LIMIT - len(head) - len(tail)) + tail


def render_memories(s: Session, hits: list[Hit], budget: int) -> str:
    """'<survivemem>…</survivemem>' within `budget` characters; empty when nothing fits."""
    if not hits:
        return ""
    checkouts = {c.id: c for c in store.list_checkouts(s.db, s.res.project_id)}
    open_tag, close_tag = "<survivemem>", "</survivemem>"
    used = len(open_tag) + len(close_tag) + 2
    lines = []
    for hit in hits:
        line = hit_line(hit, checkouts)
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join([open_tag, *lines, close_tag]) if lines else ""


def relevant(s: Session, query: str) -> list[Hit]:
    loc = s.res.location
    branch = loc.branch if loc else None
    checkout = s.res.checkout.id if s.res.checkout else None
    view = store.visible_view(s.db, s.res.project_id, branch, checkout)
    hits = store.search(s.db, query, view, limit=SEARCH_LIMIT)
    if ERROR_LIKE.search(query):
        hits.sort(key=lambda h: h.memory.kind != "pitfall")
    return hits


def touch(s: Session, payload: HookInput) -> None:
    """Record which session is active in this project, for the MCP checkpoint tool."""
    sid = text_field(payload, "session_id")
    if not sid:
        return
    try:
        store.touch_session(s.db, sid, s.res.project_id)
    except sqlite3.OperationalError as e:
        log.warning("cannot record session %s: %s", sid, e)


def store_candidate(s: Session, prompt: str, answer: str, tag: str) -> None:
    content = candidate_content(prompt, answer)
    if not content:
        return
    if contains_secret(content):
        log.info("%s: candidate skipped, content looks like it contains a secret", tag)
        return
    store.add_memory(
        s.db, content, "candidate", project=s.res.project_id, source="hook", tags=(tag,)
    )


def purge_old_states(s: Session) -> None:
    """Housekeeping only: on a read-only DB it is skipped, never at the cost of the output."""
    try:
        removed = store.purge_states(s.db, dbmod.now_ms() - state.TTL_MS)
    except sqlite3.OperationalError as e:
        log.warning("cannot purge old session states: %s", e)
        return
    if removed:
        log.info("purged %d session states older than 14 days", removed)


# ---------------------------------------------------------------------------------------------
# Handlers


def hook_prompt(payload: HookInput) -> str:
    prompt = text_field(payload, "prompt")
    with session(payload) as s:
        count = count_prompt(s, payload)
        head = [x for x in (identity.checkout_line(s.db, s.res), s.res.hint) if x]
        head += investigation_block(
            s, payload, full=count <= 1 or (count - 1) % INVESTIGATION_EVERY == 0
        )
        budget = PROMPT_BUDGET - sum(len(x) + 1 for x in head)
        block = render_memories(s, relevant(s, prompt), budget) if prompt else ""
    return "\n".join([*head, block] if block else head)


def count_prompt(s: Session, payload: HookInput) -> int:
    """Prompts seen by this session, this one included; 1 when it cannot be recorded."""
    sid = text_field(payload, "session_id")
    if not sid:
        return 1
    try:
        return store.count_prompt(s.db, sid, s.res.project_id)
    except sqlite3.OperationalError as e:
        log.warning("cannot record session %s: %s", sid, e)
        return 1


def investigation_block(s: Session, payload: HookInput, full: bool) -> list[str]:
    """§14 re-injection: the full rendering on the first prompt of a session and every 20th
    after it, one line in between (plus the blocker, which must stay visible)."""
    inv = store.open_investigation_of(s.db, s.res.project_id)
    if inv is None:
        return []
    found = store.get_state(s.db, text_field(payload, "session_id"))
    next_steps = state.strings(found.state.get("next")) if found else []
    ctx = investigate.context(s.db, inv, s.res, next_steps)
    sync_blockers(s, payload, investigate.blocker(ctx.entries))
    if full:
        return [
            "<survivemem-investigation>",
            investigate.render(ctx),
            "</survivemem-investigation>",
        ]
    block = investigate.blocker(ctx.entries)
    return [investigate.one_liner(inv), *([f"BLOCKER   {block}"] if block else [])]


def sync_blockers(s: Session, payload: HookInput, current: str | None) -> None:
    """A loop blocker leaves session_state once a hypothesis has answered it."""
    sid = text_field(payload, "session_id")
    found = store.get_state(s.db, sid) if sid else None
    if found is None:
        return
    blockers = state.strings(found.state.get("blockers"))
    kept = [b for b in blockers if not b.startswith(investigate.LOOP_PREFIX) or b == current]
    if kept != blockers:
        try:
            store.set_state_key(s.db, sid, s.res.project_id, "blockers", kept)
        except sqlite3.OperationalError as e:
            log.warning("cannot update blockers: %s", e)


def candidate_content(prompt: str, answer: str) -> str:
    if not answer.strip():
        return ""
    answer = shorten(answer, CANDIDATE_ANSWER_CHARS)
    if not prompt.strip():
        return answer
    return f"{shorten(prompt, CANDIDATE_PROMPT_CHARS)}\n→ {answer}"


def hook_stop(payload: HookInput) -> str:
    if cast(dict[str, Any], payload).get("stop_hook_active"):
        return ""
    entries = transcript_entries(payload)
    answer = text_field(payload, "last_assistant_message")
    answer = answer or transcript.last(transcript.assistant_messages(entries))
    if not answer.strip():
        return ""
    with session(payload) as s:
        store_candidate(s, transcript.last(transcript.user_prompts(entries)), answer, "stop")
        embed_backlog(s)
    return ""


def embed_backlog(s: Session) -> None:
    """Embeddings happen here, never in the prompt hook (§9 Stop)."""
    if not store.has_vec_table(s.db):
        return
    from survivemem import embed  # httpx only when vectors are enabled

    try:
        done = embed.backfill(s.db, s.cfg, STOP_EMBED_BATCH, timeout=STOP_EMBED_TIMEOUT)
    except sqlite3.OperationalError as e:
        log.warning("cannot store embeddings: %s", e)
        return
    if done:
        log.info("embedded %d memories", done)


def hook_precompact(payload: HookInput) -> str:
    """Deterministic fallback checkpoint from the transcript, merged under the model's one."""
    sid = text_field(payload, "session_id")
    entries = transcript_entries(payload)
    found = state.from_transcript(entries)
    with session(payload) as s:
        if s.cfg.compaction_summarize:
            found["summary"] = summary_of(s.cfg, found)
        if sid:
            old = store.get_state(s.db, sid)
            merged = state.merge_heuristic(old.state if old else {}, found)
            store.save_state(s.db, sid, s.res.project_id, merged)
        prompts = transcript.user_prompts(entries)
        store_candidate(
            s, transcript.last(prompts), str(found.get("last_answer", "")), "precompact"
        )
        purge_old_states(s)
    return ""


def summary_of(cfg: Config, found: state.State) -> str:
    """Optional ([compaction] summarize): a local model's 10-line summary, else nothing."""
    from survivemem import embed

    text = "\n".join(
        [
            *state.strings(found.get("recent_prompts")),
            state.text(found.get("last_answer")),
            *state.strings(found.get("commands")),
        ]
    )
    if not text.strip():
        return ""
    return embed.summarize(text, cfg) or ""


def startup_hits(s: Session, st: state.State) -> list[Hit]:
    """Memories for the state's goal and next steps, topped up with the newest ones."""
    query = " ".join(
        [
            state.text(st.get("goal")),
            state.text(st.get("in_progress")),
            *state.strings(st.get("next")),
        ]
    )
    hits = relevant(s, query) if query.strip() else []
    seen = {h.memory.id for h in hits}
    view = store.visible_view(s.db, s.res.project_id)
    for m in store.list_memories(s.db, view, limit=SEARCH_LIMIT):
        if len(hits) < SEARCH_LIMIT and m.id not in seen:
            hits.append(Hit(m, 0.0, ("recent",)))
    return hits


def hook_session_start(payload: HookInput) -> str:
    """After compaction or resume: clone line, session state, long-term memories."""
    sid = text_field(payload, "session_id")
    with session(payload) as s:
        touch(s, payload)
        found = store.get_state(s.db, sid) if sid else None
        st = found.state if found else {}
        inv_block = investigation_block(s, payload, full=True)
        parts = [
            *inv_block,
            identity.checkout_line(s.db, s.res, always=True),
            state.render(st),
            render_memories(s, startup_hits(s, st), SESSION_START_MEMORY_BUDGET),
            investigate.CLOSING_LINE if inv_block else "",
        ]
        purge_old_states(s)
    return "\n".join(p for p in parts if p)


def tool_input(payload: HookInput) -> dict[str, Any]:
    value = cast(dict[str, Any], payload).get("tool_input")
    return value if isinstance(value, dict) else {}


def hook_pretool(payload: HookInput) -> str:
    """Loop guard (§9 PreToolUse): only while the project has an open investigation."""
    command = tool_input(payload).get("command")
    if not isinstance(command, str) or not command.strip():
        return ""
    with closing(dbmod.connect()) as db:
        if not store.any_open_investigation(db):  # the common case: no git calls at all
            return ""
    with session(payload) as s:
        inv = store.open_investigation_of(s.db, s.res.project_id)
        if inv is None:
            return ""
        normalised = investigate.normalise_command(command, investigate.workspace_of(s.res))
        evidence = {e.id: e for e in store.evidence_of(s.db, inv.id)}
        verdict = investigate.guard(
            store.entries_of(s.db, inv.id), normalised, investigate.current_anchor(s.res), evidence
        )
        mode = s.cfg.loop_guard_mode
    return json.dumps(pretool_output(verdict, mode)) if verdict else ""


def pretool_output(verdict: investigate.Verdict, mode: str) -> dict[str, Any]:
    """ask/deny carry a permission decision; warn and "seen as" only add context. They never
    answer "allow": that would skip the user's normal permission prompt for the command."""
    if not verdict.blocking or mode == "warn":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": verdict.message,
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny" if mode == "deny" else "ask",
            "permissionDecisionReason": verdict.message,
        }
    }


def hook_posttool(payload: HookInput) -> str:
    name = text_field(payload, "tool_name")
    if name == "Bash":
        capture_bash(payload)
    elif name.startswith("mcp__"):
        capture_mcp(payload, name)
    return ""


def bash_outcome(payload: HookInput) -> tuple[int, str]:
    """(exit code, output). PostToolUse fires for successful calls only; a failed one arrives as
    PostToolUseFailure whose `error` reads 'Exit code N\n<output>'."""
    error = text_field(payload, "error")
    if text_field(payload, "hook_event_name") == "PostToolUseFailure" or error:
        found = EXIT_CODE.search(error)
        return (int(found.group(1)) if found else 1), error
    response = cast(dict[str, Any], payload).get("tool_response")
    if isinstance(response, dict):
        streams = (response.get("stdout"), response.get("stderr"))
        return 0, "\n".join(str(x) for x in streams if x)
    return 0, str(response or "")


def capture_bash(payload: HookInput) -> None:
    command = tool_input(payload).get("command")
    if not isinstance(command, str):
        return
    cfg = config()
    if not investigate.is_debug_command(command, cfg.debug_commands):
        return
    code, output = bash_outcome(payload)
    with session(payload) as s:
        inv = store.open_investigation_of(s.db, s.res.project_id)
        if inv is None and code == 0:
            return
        normalised = investigate.normalise_command(command, investigate.workspace_of(s.res))
        extract = ingest.extract(output, ingest.detect_tool(normalised))
        if inv is None:
            content = f"$ {normalised} → exit {code}\n{extract.excerpt}"
            capped_candidate(s, payload, content, "hook", (*extract.tags, "posttool"))
            return
        record_experiment(s, payload, inv, normalised, code, output, extract)


def record_experiment(
    s: Session,
    payload: HookInput,
    inv: Investigation,
    command: str,
    code: int,
    output: str,
    extract: ingest.Extract,
) -> None:
    """Evidence (excerpt only) + experiment/observation entry; a third identical failure adds
    a LOOP note and makes it the session's blocker."""
    ev = store.add_evidence(
        s.db,
        inv.id,
        f"bash:{text_field(payload, 'tool_use_id') or 'run'}",
        investigate.sha256_text(output),
        len(output.encode()),
        extract.tool,
        extract.excerpt,
    )
    failed = code != 0
    entry = store.NewEntry(
        "experiment" if failed else "observation",
        f"{command} → exit {code}",
        command,
        "bad" if failed else None,
        investigate.current_anchor(s.res),
        investigate.excerpt_sha(extract.excerpt),
        evidence_id=ev.id,
    )
    store.add_entry(s.db, inv.id, entry)
    note = investigate.loop_note(store.entries_of(s.db, inv.id), command, entry.excerpt_sha)
    if note is None:
        return
    store.add_entry(s.db, inv.id, store.NewEntry("note", note))
    sid = text_field(payload, "session_id")
    if sid:
        store.set_state_key(s.db, sid, s.res.project_id, "blockers", [note])


def capture_mcp(payload: HookInput, name: str) -> None:
    """A pointer to what a RAG / code-index tool answered, never the payload itself."""
    cfg = config()
    if not any(fnmatch.fnmatchcase(name, pattern) for pattern in cfg.capture_tools):
        return
    server = name.split("__")[1] if name.count("__") >= 2 else "mcp"
    args = tool_input(payload)
    query = next(
        (
            str(args[k])
            for k in ("query", "symbol", "symbol_id", "action", "path", "file_path", "doc_id")
            if args.get(k)
        ),
        "",
    )
    response = cast(dict[str, Any], payload).get("tool_response")
    digest = shorten(response_text(response), MCP_DIGEST_CHARS)
    content = shorten(f"{name} {query} → {digest}", MCP_DIGEST_CHARS)
    with session(payload) as s:
        capped_candidate(
            s, payload, content, "tool", ("mcp", server), refs=result_refs(response, server) or None
        )


def response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        return " ".join(response_text(r) for r in response)
    if isinstance(response, dict):
        if isinstance(response.get("content"), list):
            return response_text(response["content"])
        if isinstance(response.get("text"), str):
            return str(response["text"])
    return json.dumps(response, ensure_ascii=False, default=str)[:2000]


def result_refs(response: Any, server: str) -> list[dict[str, Any]]:
    """symbol / doc pointers found in the result (§15 refs), at most five."""
    refs: list[dict[str, Any]] = []
    stack: list[Any] = [response]
    while stack and len(refs) < MCP_REFS:
        item = stack.pop()
        if isinstance(item, str) and item[:1] in "{[":
            with suppress(ValueError):  # text that merely starts like JSON carries no refs
                stack.append(json.loads(item))
        elif isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            ref = _ref(item, server)
            if ref and ref not in refs:
                refs.append(ref)
            stack.extend(reversed(list(item.values())))
    return refs


def _ref(item: dict[str, Any], server: str) -> dict[str, Any] | None:
    path = item.get("file") or item.get("path") or item.get("file_path")
    symbol = item.get("symbol") or item.get("name")
    if isinstance(path, str) and isinstance(symbol, str):
        return {"type": "symbol", "tool": server, "path": path, "symbol": symbol}
    if item.get("doc_id"):
        ref = {"type": "doc", "tool": server, "doc_id": str(item["doc_id"])}
        if item.get("chunk") is not None:
            ref["chunk"] = item["chunk"]
        return ref
    return None


def capped_candidate(
    s: Session,
    payload: HookInput,
    content: str,
    source: str,
    tags: tuple[str, ...],
    refs: list[dict[str, Any]] | None = None,
) -> None:
    """At most 50 candidates per session from PostToolUse; then stop and log once."""
    if contains_secret(content):
        log.info("posttool: candidate skipped, content looks like it contains a secret")
        return
    sid = text_field(payload, "session_id") or "unknown"
    written = store.bump_state(s.db, sid, s.res.project_id, "_posttool_candidates")
    if written > POSTTOOL_CANDIDATE_CAP:
        if written == POSTTOOL_CANDIDATE_CAP + 1:
            log.warning(
                "posttool: %d candidates in session %s, not writing more",
                POSTTOOL_CANDIDATE_CAP,
                sid,
            )
        return
    store.add_memory(
        s.db, content, "candidate", project=s.res.project_id, source=source, tags=tags, refs=refs
    )


HANDLERS: dict[str, Callable[[HookInput], str]] = {
    "prompt": hook_prompt,
    "stop": hook_stop,
    "precompact": hook_precompact,
    "session-start": hook_session_start,
    "pretool": hook_pretool,
    "posttool": hook_posttool,
}


def run(name: str, stdin: TextIO, stdout: TextIO) -> int:
    logfile.configure()
    started = time.monotonic()
    try:
        out = HANDLERS[name](read_payload(stdin))
        if out:
            stdout.write(out if out.endswith("\n") else out + "\n")
    except Exception:  # hook boundary: Claude Code must never see a failing hook
        log.exception("hook %s failed", name)
    log.debug("hook %s took %.0f ms", name, (time.monotonic() - started) * 1000)
    return 0
