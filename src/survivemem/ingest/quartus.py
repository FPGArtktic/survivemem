# SPDX-License-Identifier: GPL-3.0-or-later
"""Quartus: Error / Critical Warning (NNNNN), Fitter, timing slack, programmer, JTAG chain."""

import re

from survivemem.ingest.base import Extract, build, matching, tags

MESSAGE = re.compile(r"^\s*(?P<level>Error|Critical Warning) \((?P<id>\d+)\): ")
UNSUCCESSFUL = re.compile(r"^\s*Error: Quartus Prime .* was unsuccessful")
FIT = re.compile(r"Can't fit|requires \d+ \w+ to implement|contains only \d+")
SLACK_HEADER = re.compile(
    r"Worst-case (?P<kind>setup|hold|recovery|removal) slack is (?P<v>-?[\d.]+)"
)
SLACK_ROW = re.compile(
    r"Info \(332119\):\s+(?P<slack>-?\d+\.\d+)\s+(?P<tns>-?\d+\.\d+)\s+(?P<clock>\S+)$"
)
CABLE = re.compile(r'Using programming cable "(?P<cable>[^"]+)"')
CHAIN_HEAD = re.compile(r"^\d+\) .+\[.+\]$")
CHAIN_DEVICE = re.compile(r"^\s+[0-9A-F]{8}\s+\S+")


def _slack(lines: list[str]) -> list[str]:
    """'setup slack -1.274 clk_50' per clock under each Worst-case header."""
    out, kind = [], None
    for line in lines:
        header = SLACK_HEADER.search(line)
        if header:
            kind = header.group("kind")
            continue
        row = SLACK_ROW.search(line)
        if row and kind:
            out.append(f"{kind} slack {row.group('slack')} {row.group('clock')}")
    return out


def extract(text: str) -> Extract:
    lines = text.splitlines()
    messages = matching(lines, MESSAGE, 10)
    ids = [m.group("id") for m in map(MESSAGE.search, messages) if m]
    chain = [line for line in lines if CHAIN_HEAD.search(line) or CHAIN_DEVICE.search(line)]
    slack = _slack(lines)
    cable = next((m.group("cable") for m in map(CABLE.search, lines) if m), None)
    excerpt = build(
        [
            *messages,
            *matching(lines, FIT, 5),
            *slack,
            f"cable: {cable}" if cable and messages else "",
            *matching(lines, UNSUCCESSFUL, 2),
            *chain,
        ]
    )
    timing = "timing" if any(s.split()[2].startswith("-") for s in slack) else None
    kind = "environment" if chain and not messages else "pitfall"
    found = tags(
        "quartus",
        "fitter" if matching(lines, FIT, 1) else None,
        timing,
        "jtag" if chain else None,
        *ids[:5],
    )
    return Extract(excerpt, "quartus", found, kind)
