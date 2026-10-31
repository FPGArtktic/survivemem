# SPDX-License-Identifier: GPL-3.0-or-later
"""What every ingest rule returns, and the excerpt builder they share."""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from survivemem.redact import redact

EXCERPT_CHARS = 1000


@dataclass(frozen=True, slots=True)
class Extract:
    excerpt: str
    tool: str
    tags: tuple[str, ...]
    suggested_kind: str  # memory kind a reviewer would most likely promote it to


def build(parts: Iterable[str], limit: int = EXCERPT_CHARS) -> str:
    """Join lines in order, drop duplicates, redact secrets, cut at `limit` characters."""
    lines = list(dict.fromkeys(line.rstrip() for line in parts if line and line.strip()))
    text = redact("\n".join(lines))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def matching(lines: list[str], pattern: re.Pattern[str], limit: int = 10) -> list[str]:
    return [line for line in lines if pattern.search(line)][:limit]


def with_following(lines: list[str], pattern: re.Pattern[str], after: int) -> list[str]:
    """Each matching line plus the `after` lines that follow it."""
    out: list[str] = []
    for i, line in enumerate(lines):
        if pattern.search(line):
            out += lines[i : i + after + 1]
    return out


def tail(text: str, n: int = 10) -> list[str]:
    return [line for line in text.splitlines() if line.strip()][-n:]


def tags(*values: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in values if v))
