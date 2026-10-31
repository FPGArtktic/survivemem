# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code transcript JSONL, read defensively.

The entry format is internal to Claude Code and changes between versions; anything that does
not look like {"type": "user"|"assistant", "message": {"content": str | [blocks]}} is skipped.
Only the tail is parsed: the end of a long session is what the hooks need.
"""

import json
from collections import deque
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

TAIL_LINES = 2000

Entry = dict[str, Any]


def tail(path: Path, max_lines: int = TAIL_LINES) -> list[Entry]:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            lines = deque(f, maxlen=max_lines)
    except OSError:
        return []
    return list(_parse(lines))


def _parse(lines: Iterable[str]) -> Iterator[Entry]:
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and not entry.get("isSidechain") and not entry.get("isMeta"):
            yield entry


def _content(entry: Entry, role: str) -> list[Any] | str | None:
    message = entry.get("message")
    if entry.get("type") != role or not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str | list) else None


def _blocks(content: list[Any] | str, kind: str) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if kind == "text" else []
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind]


def _text(content: list[Any] | str) -> str:
    return "\n".join(str(b.get("text", "")) for b in _blocks(content, "text")).strip()


def user_prompts(entries: Iterable[Entry]) -> list[str]:
    """Typed prompts, oldest first; user entries that carry tool results are not prompts."""
    prompts = []
    for entry in entries:
        content = _content(entry, "user")
        if content is None or _blocks(content, "tool_result"):
            continue
        text = _text(content)
        if text:
            prompts.append(text)
    return prompts


def assistant_messages(entries: Iterable[Entry]) -> list[str]:
    """Assistant text per message, oldest first. Claude Code writes each content block as its own
    entry carrying the same message id, so blocks are joined by id."""
    messages: list[str] = []
    last_id: object = None
    for entry in entries:
        content = _content(entry, "assistant")
        text = _text(content) if content is not None else ""
        if not text:
            continue
        message_id = entry["message"].get("id")
        if messages and message_id is not None and message_id == last_id:
            messages[-1] += "\n" + text
        else:
            messages.append(text)
        last_id = message_id
    return messages


def last(items: list[str]) -> str:
    return items[-1] if items else ""
