# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenOCD / system-console / gdb: connection and JTAG failures, the last lines before exit."""

import re

from survivemem.ingest.base import Extract, build, matching, tags, tail

ERROR = re.compile(r"^(Error|Warn)\b|^\s*Error:")
CONNECTION = re.compile(
    r"(?i)connection refused|could not connect|unable to open|no device found"
    r"|libusb_open\(\) failed|not found"
)
NOT_EXAMINED = re.compile(r"Target not examined|Examination failed")
JTAG = re.compile(r"(?i)jtag_debug|jtag scan chain|IR capture error|\bJTAG\b")


def extract(text: str) -> Extract:
    lines = text.splitlines()
    errors = matching(lines, ERROR, 10)
    connection = matching(lines, CONNECTION, 3)
    examined = matching(lines, NOT_EXAMINED, 2)
    jtag = matching(lines, JTAG, 5)
    excerpt = build([*connection, *jtag, *examined, *errors, "--- last lines ---", *tail(text)])
    found = tags(
        "openocd",
        "jtag" if jtag else None,
        "connection" if connection else None,
        "target-not-examined" if examined else None,
    )
    return Extract(excerpt, "openocd", found, "pitfall")
