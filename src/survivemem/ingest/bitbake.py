# SPDX-License-Identifier: GPL-3.0-or-later
"""bitbake / devtool: ERROR lines, failing task and recipe, log.do_* path, compiler output."""

import re

from survivemem.ingest.base import Extract, build, matching, tags

ERROR = re.compile(r"^ERROR: ")
TASK = re.compile(r"ERROR: (?P<pkg>[\w.+-]+?)-(?P<ver>[\w.+~]+)-r\d+ (?P<task>do_\w+)")
FAILED_TASK = re.compile(r"Task \((?P<recipe>[^:]+\.bb(?:append)?):(?P<task>do_\w+)\) failed")
LOGFILE = re.compile(r"Logfile of failure stored in: (?P<path>\S+)")
PIPED_ERROR = re.compile(r"^\| .*\b(error|Error|ERROR)\b")
PIPED_LINES = 20


def _piped_block(lines: list[str]) -> list[str]:
    """Up to 20 '| ' lines from the one before the first reported error (its context)."""
    for i, line in enumerate(lines):
        if PIPED_ERROR.search(line):
            start = i - 1 if i and lines[i - 1].startswith("|") else i
            return [x for x in lines[start:] if x.startswith("|")][:PIPED_LINES]
    return []


def extract(text: str) -> Extract:
    lines = text.splitlines()
    task = next((m for m in map(TASK.search, lines) if m), None)
    failed = next((m for m in map(FAILED_TASK.search, lines) if m), None)
    logfile = next((m.group("path") for m in map(LOGFILE.search, lines) if m), None)
    name = task.group("task") if task else failed.group("task") if failed else None
    recipe = task.group("pkg") if task else None
    summary = f"bitbake: {name or 'task'} failed" + (f" in {recipe}" if recipe else "")
    excerpt = build(
        [
            summary,
            *matching(lines, ERROR, 6),
            *_piped_block(lines),
            f"log: {logfile}" if logfile else "",
        ]
    )
    return Extract(excerpt, "bitbake", tags("bitbake", name, recipe), "pitfall")
