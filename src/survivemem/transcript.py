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
# subagent entries, meta messages, and the continuation summary written after a compaction
# (a user entry, but not something the user typed)
SKIP_FLAGS = ("isSidechain", "isMeta", "isCompactSummary")

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
        if isinstance(entry, dict) and not any(entry.get(flag) for flag in SKIP_FLAGS):
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


FILE_TOOLS = {
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "Read": "file_path",
    "NotebookEdit": "notebook_path",
}


def tool_uses(entries: Iterable[Entry]) -> list[tuple[str, dict[str, Any]]]:
    """(tool name, input) of every assistant tool call, oldest first."""
    calls = []
    for entry in entries:
        content = _content(entry, "assistant")
        for block in _blocks(content, "tool_use") if content is not None else []:
            name, args = block.get("name"), block.get("input")
            if isinstance(name, str) and isinstance(args, dict):
                calls.append((name, args))
    return calls


def files_touched(calls: Iterable[tuple[str, dict[str, Any]]], limit: int = 30) -> list[str]:
    """Paths from Edit/Write/Read calls, deduplicated, newest first."""
    seen: dict[str, None] = {}
    for name, args in reversed(list(calls)):
        path = args.get(FILE_TOOLS.get(name, ""))
        if isinstance(path, str) and path and path not in seen:
            seen[path] = None
    return list(seen)[:limit]


def bash_commands(calls: Iterable[tuple[str, dict[str, Any]]], limit: int = 10) -> list[str]:
    commands = [str(a["command"]) for n, a in calls if n == "Bash" and a.get("command")]
    return commands[-limit:]


def last_todos(calls: Iterable[tuple[str, dict[str, Any]]]) -> list[dict[str, str]]:
    """The last TodoWrite payload as [{content, status}], or [] when there is none."""
    todos: list[dict[str, str]] = []
    for name, args in calls:
        items = args.get("todos")
        if name == "TodoWrite" and isinstance(items, list):
            todos = [
                {"content": str(t.get("content", "")), "status": str(t.get("status", ""))}
                for t in items
                if isinstance(t, dict) and t.get("content")
            ]
    return todos


def last(items: list[str]) -> str:
    return items[-1] if items else ""
