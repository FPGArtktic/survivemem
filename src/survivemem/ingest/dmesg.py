# SPDX-License-Identifier: GPL-3.0-or-later
"""dmesg / journalctl: oopses and warnings with context, probe failures, FPGA manager/region."""

import re

from survivemem.ingest.base import Extract, build, matching, tags, with_following

CRASH = re.compile(r"Oops|BUG:|Kernel panic|\bpanic\b|WARNING: CPU:")
PROBE = re.compile(r"probe.*failed|failed to probe|probe of \S+ failed")
DEFER = re.compile(r"EPROBE_DEFER|error -517\b|deferred probe")
FPGA = re.compile(r"fpga_manager|fpga-region|fpga_region|fpga-bridge|fpga_bridge")
CRASH_CONTEXT = 15


def extract(text: str) -> Extract:
    lines = text.splitlines()
    crash = with_following(lines, CRASH, CRASH_CONTEXT)
    probe = matching(lines, PROBE, 5)
    defer = matching(lines, DEFER, 5)
    fpga = matching(lines, FPGA, 8)
    found = tags(
        "dmesg",
        "oops" if crash else None,
        "probe" if probe else None,
        "eprobe_defer" if defer else None,
        "fpga" if fpga else None,
    )
    excerpt = build([*fpga, *probe, *defer, *crash])
    return Extract(excerpt, "dmesg", found, "pitfall")
