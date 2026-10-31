# SPDX-License-Identifier: GPL-3.0-or-later
"""Compaction-proof session state (§16): merge rules for checkpoints and terse rendering."""

from collections.abc import Sequence
from typing import Any

from survivemem import transcript

TTL_MS = 14 * 86_400_000
BUDGET = 3000
ITEM_CHARS = 200
MAX_FILES = 30
MAX_PROMPTS = 3
PROMPT_CHARS = 300
ANSWER_CHARS = 600
SUMMARY_CHARS = 1200
LIST_FIELDS = ("done", "next", "decisions", "files", "blockers", "commands", "recent_prompts")

State = dict[str, Any]


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def strings(value: Any) -> list[str]:
    """A JSON value narrowed to a list of non-empty strings."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str | int | float) and str(v).strip()]


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def union(newer: Sequence[str], older: Sequence[str], limit: int) -> list[str]:
    return list(dict.fromkeys([*newer, *older]))[:limit]


def is_empty(state: State) -> bool:
    scalars = ("goal", "in_progress", "last_answer", "summary")
    return not any(text(state.get(k)) for k in scalars) and not any(
        strings(state.get(k)) for k in LIST_FIELDS
    )


def checkpoint(
    old: State,
    *,
    goal: str | None = None,
    done: Sequence[str] | None = None,
    in_progress: str | None = None,
    next_: Sequence[str] | None = None,
    decisions: Sequence[str] | None = None,
    files: Sequence[str] = (),
    blockers: Sequence[str] | None = None,
) -> State:
    """A model checkpoint is the primary source: it overwrites what it states, merges files."""
    state = dict(old)
    updates: dict[str, Any] = {
        "goal": goal,
        "in_progress": in_progress,
        "done": done,
        "next": next_,
        "decisions": decisions,
        "blockers": blockers,
    }
    for key, value in updates.items():
        if value is not None:
            state[key] = value.strip() if isinstance(value, str) else strings(list(value))
    state["files"] = union(strings(list(files)), strings(old.get("files")), MAX_FILES)
    state["source"] = "model"
    return state


def from_transcript(entries: Sequence[transcript.Entry]) -> State:
    """Deterministic PreCompact extraction; no LLM involved."""
    calls = transcript.tool_uses(entries)
    prompts = [shorten(p, PROMPT_CHARS) for p in transcript.user_prompts(entries)[-MAX_PROMPTS:]]
    found: State = {
        "recent_prompts": prompts,
        "last_answer": shorten(
            transcript.last(transcript.assistant_messages(entries)), ANSWER_CHARS
        ),
        "files": transcript.files_touched(calls, MAX_FILES),
        "commands": transcript.bash_commands(calls),
    }
    todos = transcript.last_todos(calls)
    if todos:
        found["done"] = [t["content"] for t in todos if t["status"] == "completed"]
        found["in_progress"] = next(
            (t["content"] for t in todos if t["status"] == "in_progress"), ""
        )
        found["next"] = [t["content"] for t in todos if t["status"] == "pending"]
    if prompts:
        found["goal"] = shorten(prompts[0], ITEM_CHARS)
    return found


def merge_heuristic(old: State, found: State) -> State:
    """Fallback for a model that forgot to checkpoint: never overrides its goal or decisions."""
    state = dict(old)
    for key in ("recent_prompts", "last_answer", "commands", "summary"):
        state[key] = found.get(key, state.get(key))
    for key in ("done", "in_progress", "next"):
        if key in found:
            state[key] = found[key]
    if not text(state.get("goal")) and found.get("goal"):
        state["goal"] = found["goal"]
    state["files"] = union(strings(found.get("files")), strings(old.get("files")), MAX_FILES)
    state["source"] = "model" if old.get("source") == "model" else "hook"
    return state


SECTIONS: tuple[tuple[str, str, int], ...] = (
    ("GOAL", "goal", ITEM_CHARS),
    ("IN PROGRESS", "in_progress", ITEM_CHARS),
    ("BLOCKERS", "blockers", ITEM_CHARS),
    ("NEXT", "next", ITEM_CHARS),
    ("DECISIONS", "decisions", ITEM_CHARS),
    ("SUMMARY", "summary", SUMMARY_CHARS),
    ("DONE", "done", ITEM_CHARS),
    ("FILES", "files", ITEM_CHARS),
    ("RECENT COMMANDS", "commands", ITEM_CHARS),
    ("RECENT PROMPTS", "recent_prompts", PROMPT_CHARS),
    ("LAST ANSWER", "last_answer", ANSWER_CHARS),
)


def _section(title: str, value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        return [f"{title}: {shorten(value, limit)}"] if value.strip() else []
    items = strings(value)
    return [f"{title}:", *(f"- {shorten(i, limit)}" for i in items)] if items else []


def render(state: State, budget: int = BUDGET) -> str:
    """Headings and bullets in priority order, cut at `budget` characters including tags."""
    open_tag, close_tag = "<survivemem-state>", "</survivemem-state>"
    used = len(open_tag) + len(close_tag) + 2
    lines: list[str] = []
    for title, key, limit in SECTIONS:
        start = len(lines)
        for line in _section(title, state.get(key), limit):
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
        if len(lines) == start + 1 and lines[-1].endswith(":"):  # heading without any item
            used -= len(lines.pop()) + 1
    return "\n".join([open_tag, *lines, close_tag]) if lines else ""
