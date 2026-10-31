# SPDX-License-Identifier: GPL-3.0-or-later
"""make / kernel builds: first compiler error, undefined references, Kconfig and modpost."""

import re

from survivemem.ingest.base import Extract, build, matching, tags

COMPILER_ERROR = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+)(?::\d+)?: (?:fatal )?error: ")
DTC_ERROR = re.compile(r"^(?:Error|ERROR)(?: \([\w-]+\))?: (?P<file>\S+\.dtsi?):\d+")
UNDEFINED = re.compile(r"undefined reference to [`']")
IN_FUNCTION = re.compile(r": in function [`']")
KCONFIG = re.compile(r"Kconfig:\d+:warning:|\(defined at [^)]*Kconfig")
MODPOST = re.compile(r"^ERROR: modpost: ")
MAKE_FAILED = re.compile(r"^make(\[\d+\])?: \*\*\* ")


def extract(text: str) -> Extract:
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if COMPILER_ERROR.search(line)), None)
    compiler = lines[first : first + 3] if first is not None else []
    dtc = matching(lines, DTC_ERROR, 5)
    undefined = matching(lines, UNDEFINED, 5)
    linker = matching(lines, IN_FUNCTION, 5) if undefined else []
    kconfig = matching(lines, KCONFIG, 5)
    modpost = matching(lines, MODPOST, 5)
    failed = matching(lines, MAKE_FAILED, 1)
    kind = (
        "compile"
        if compiler
        else "dtc"
        if dtc
        else "link"
        if undefined
        else "modpost"
        if modpost
        else "kconfig"
        if kconfig
        else None
    )
    location = COMPILER_ERROR.search(lines[first]) if first is not None else None
    dtc_file = DTC_ERROR.search(dtc[0]) if dtc else None
    file_tag = location.group("file") if location else dtc_file.group("file") if dtc_file else None
    excerpt = build([*compiler, *dtc, *modpost, *linker, *undefined, *kconfig, *failed])
    return Extract(excerpt, "make", tags("make", kind, file_tag), "pitfall")
