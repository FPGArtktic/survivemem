# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code hook handlers (§9).

stdout belongs to the hook contract: plain text for UserPromptSubmit and SessionStart, JSON for
PreToolUse/PostToolUse. Everything else goes to hooks.log. A hook never fails: the boundary in
run() logs the traceback and exits 0.
"""

import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, TypedDict, cast

from survivemem import db as dbmod
from survivemem import identity, logfile, state, store, transcript
from survivemem.config import Config, ConfigError, load_config
from survivemem.model import Checkout, Hit
from survivemem.redact import contains_secret

log = logging.getLogger(__name__)

PROMPT_BUDGET = 6000
SESSION_START_MEMORY_BUDGET = 3000
RECORD_LIMIT = 400
SEARCH_LIMIT = 8
CANDIDATE_PROMPT_CHARS = 300
CANDIDATE_ANSWER_CHARS = 600
ERROR_LIKE = re.compile(
    r"\bERROR\b|failed|\bOops\b|\bpanic|timing|slack|Critical Warning|Error \(|Fitter", re.I
)


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


def _resolve(db: sqlite3.Connection, cwd: Path, cfg: Config) -> identity.Resolution:
    try:
        return identity.resolve(db, cwd, cfg)
    except sqlite3.OperationalError as e:
        log.warning("resolution without writes (%s)", e)
        return identity.resolve(db, cwd, cfg, writable=False)


@contextmanager
def session(payload: HookInput) -> Iterator[Session]:
    cfg = config()
    cwd = Path(text_field(payload, "cwd") or os.getcwd())
    with closing(dbmod.connect()) as db:
        yield Session(db, cfg, _resolve(db, cwd, cfg))


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
        touch(s, payload)
        head = [x for x in (identity.checkout_line(s.db, s.res), s.res.hint) if x]
        budget = PROMPT_BUDGET - sum(len(x) + 1 for x in head)
        block = render_memories(s, relevant(s, prompt), budget) if prompt else ""
    return "\n".join([*head, block] if block else head)


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
    path = text_field(payload, "transcript_path")
    entries = transcript.tail(Path(path)) if path else []
    answer = text_field(payload, "last_assistant_message")
    answer = answer or transcript.last(transcript.assistant_messages(entries))
    if not answer.strip():
        return ""
    with session(payload) as s:
        store_candidate(s, transcript.last(transcript.user_prompts(entries)), answer, "stop")
    return ""


def hook_precompact(payload: HookInput) -> str:
    """Deterministic fallback checkpoint from the transcript, merged under the model's one."""
    sid = text_field(payload, "session_id")
    path = text_field(payload, "transcript_path")
    entries = transcript.tail(Path(path)) if path else []
    found = state.from_transcript(entries)
    with session(payload) as s:
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
        parts = [
            identity.checkout_line(s.db, s.res, always=True),
            state.render(st),
            render_memories(s, startup_hits(s, st), SESSION_START_MEMORY_BUDGET),
        ]
        purge_old_states(s)
    return "\n".join(p for p in parts if p)


HANDLERS: dict[str, Callable[[HookInput], str]] = {
    "prompt": hook_prompt,
    "stop": hook_stop,
    "precompact": hook_precompact,
    "session-start": hook_session_start,
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
