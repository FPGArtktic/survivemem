# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic log extraction, one module per tool (§15 `ingest-log` rules)."""

import re
from collections.abc import Callable

from survivemem.ingest import bitbake, dmesg, make, openocd, quartus
from survivemem.ingest.base import Extract, build, tail

TOOLS = ("bitbake", "make", "dmesg", "quartus", "openocd", "gdb", "system-console", "other")


def _other(text: str) -> Extract:
    return Extract(build(tail(text)), "other", ("other",), "pitfall")


EXTRACTORS: dict[str, Callable[[str], Extract]] = {
    "bitbake": bitbake.extract,
    "make": make.extract,
    "dmesg": dmesg.extract,
    "quartus": quartus.extract,
    "openocd": openocd.extract,
    "gdb": openocd.extract,
    "system-console": openocd.extract,
    "other": _other,
}

# Most specific first: "make" also appears inside other tools' command lines.
COMMAND_TOOLS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(bitbake|devtool)\b"), "bitbake"),
    (re.compile(r"\b(quartus_\w+|jtagconfig)\b"), "quartus"),
    (re.compile(r"\b(openocd|system-console|nios2-\S+|niosv\S*)\b"), "openocd"),
    (re.compile(r"\b(gdb|gdb-multiarch|[\w-]+-gdb)\b"), "gdb"),
    (re.compile(r"\b(dmesg|journalctl)\b"), "dmesg"),
    (re.compile(r"\bmake\b"), "make"),
)


def detect_tool(command: str) -> str:
    return next((tool for pattern, tool in COMMAND_TOOLS if pattern.search(command)), "other")


def extract(text: str, tool: str) -> Extract:
    if tool not in EXTRACTORS:
        raise ValueError(f"unknown tool {tool!r}; known: {', '.join(EXTRACTORS)}")
    return EXTRACTORS[tool](text)


__all__ = ["EXTRACTORS", "TOOLS", "Extract", "detect_tool", "extract"]
