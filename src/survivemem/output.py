# SPDX-License-Identifier: GPL-3.0-or-later
"""Human (table, md) and machine (json, jsonl) output for the CLI (§10.1, §10.3, §10.4)."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from survivemem.model import iso

FORMATS = ("table", "md", "json", "jsonl")
Record = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Column:
    title: str
    get: Callable[[Record], str]
    flex: bool = False


def relative(ms: int | None, now_ms: int | None = None) -> str:
    if ms is None:
        return "-"
    now_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    seconds = max(0, (now_ms - ms) // 1000)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "now"


def iso_date(ms: int | None) -> str:
    return (iso(ms) or "-")[:10]


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return text[:width]
    return text[: width - len(ellipsis)] + ellipsis


def render_table(
    rows: Sequence[Record], columns: Sequence[Column], width: int | None, ellipsis: str = "…"
) -> str:
    cells = [[col.get(r) for col in columns] for r in rows]
    widths = [max([len(c.title)] + [len(row[i]) for row in cells]) for i, c in enumerate(columns)]
    flex = [i for i, c in enumerate(columns) if c.flex]
    if width is not None and flex:
        fixed = sum(w + 2 for i, w in enumerate(widths) if i != flex[0])
        widths[flex[0]] = max(len(columns[flex[0]].title), min(widths[flex[0]], width - fixed))
    lines = [_row([c.title for c in columns], widths)]
    lines += [
        _row([truncate(v, widths[i], ellipsis) for i, v in enumerate(row)], widths) for row in cells
    ]
    return "\n".join(line.rstrip() for line in lines)


def _row(values: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(v.ljust(w) for v, w in zip(values, widths, strict=True))


class Output:
    def __init__(
        self,
        fmt: str,
        *,
        quiet: bool = False,
        wide: bool = False,
        no_color: bool = False,
        stream: TextIO | None = None,
    ) -> None:
        self.fmt = fmt
        self.quiet = quiet
        self.wide = wide
        self.stream = stream or sys.stdout
        # §10.1: --no-color, NO_COLOR or a non-TTY mean plain ASCII. No ANSI is ever emitted.
        self.ascii = no_color or "NO_COLOR" in os.environ or not self.is_tty

    @property
    def ellipsis(self) -> str:
        return "..." if self.ascii else "…"

    @property
    def is_machine(self) -> bool:
        return self.fmt in ("json", "jsonl")

    @property
    def is_tty(self) -> bool:
        return self.stream.isatty()

    def width(self) -> int | None:
        if self.wide or not self.is_tty:
            return None
        return shutil.get_terminal_size((100, 24)).columns

    def records(
        self,
        items: Sequence[Record],
        columns: Sequence[Column],
        md: Callable[[Sequence[Record]], str] | None = None,
        empty: str = "(none)",
    ) -> None:
        if self.quiet:
            self.write("\n".join(str(i["id"]) for i in items))
        elif self.fmt == "json":
            self.write(json.dumps(list(items), ensure_ascii=False, indent=2))
        elif self.fmt == "jsonl":
            self.write("\n".join(json.dumps(i, ensure_ascii=False) for i in items))
        elif not items:
            self.write(empty)
        elif self.fmt == "md" and md is not None:
            self.write(md(items))
        else:
            self.write(render_table(items, columns, self.width(), self.ellipsis))

    def record(self, item: Record, human: Callable[[Record], str]) -> None:
        if self.quiet and "id" in item:
            self.write(str(item["id"]))
        elif self.is_machine:
            indent = 2 if self.fmt == "json" else None
            self.write(json.dumps(item, ensure_ascii=False, indent=indent))
        else:
            self.write(human(item))

    def write(self, text: str) -> None:
        if not text:
            return
        if self._should_page(text):
            self._page(text)
            return
        self.stream.write(text if text.endswith("\n") else text + "\n")

    def _should_page(self, text: str) -> bool:
        if self.is_machine or self.quiet or not self.is_tty:
            return False
        return text.count("\n") + 1 > shutil.get_terminal_size((100, 24)).lines

    def _page(self, text: str) -> None:
        pager = shlex.split(os.environ.get("PAGER") or "less -R")
        try:
            subprocess.run(pager, input=text, text=True, check=False)
        except OSError:
            self.stream.write(text + "\n")


def emit_error(code: int, message: str, *, machine: bool) -> None:
    if machine:
        sys.stderr.write(json.dumps({"error": {"code": code, "message": message}}) + "\n")
    else:
        sys.stderr.write(f"survivemem: {message}\n")
