# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-repo workspaces (§15): the repos a bblayers.conf, kas.yml or repo manifest names."""

import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

BBLAYERS_ASSIGN = re.compile(r'^\s*BBLAYERS\s*[?:+]?=\s*"(?P<value>[^"]*)"', re.M | re.S)
KAS_REPO = re.compile(r"^  (?P<name>[\w.+-]+):\s*$")
KAS_PATH = re.compile(r"^    path:\s*['\"]?(?P<path>[^'\"#]+?)['\"]?\s*(#.*)?$")


def bblayers(conf: Path) -> list[Path]:
    """Layer directories of conf/bblayers.conf; ${TOPDIR} is the build directory."""
    topdir = conf.parent.parent
    text = conf.read_text(errors="replace").replace("\\\n", " ")
    layers: list[Path] = []
    for match in BBLAYERS_ASSIGN.finditer(text):
        for token in match.group("value").split():
            token = token.replace("${TOPDIR}", str(topdir))
            if "${" not in token:
                layers.append(Path(os.path.normpath(topdir / token)))
    return layers


def kas_repos(kas: Path) -> list[Path]:
    """`repos:` entries of a kas file: `path:` when given, else the repo's name."""
    repos: list[Path] = []
    in_repos, name, path = False, None, None
    for line in [*kas.read_text(errors="replace").splitlines(), "end:"]:
        if not line.startswith(" ") and line.strip():
            if name:
                repos.append(kas.parent / (path or name))
            in_repos, name, path = line.startswith("repos:"), None, None
            continue
        if not in_repos:
            continue
        entry = KAS_REPO.match(line)
        if entry:
            if name:
                repos.append(kas.parent / (path or name))
            name, path = entry.group("name"), None
        elif found := KAS_PATH.match(line):
            path = found.group("path")
    return [p.resolve() for p in repos]


def manifest_projects(top: Path) -> list[Path]:
    """<project path=… name=…> of .repo/manifest.xml, relative to the repo top directory."""
    tree = ET.parse(top / ".repo" / "manifest.xml")
    return [
        (top / (p.get("path") or p.get("name") or "")).resolve()
        for p in tree.getroot().iter("project")
        if p.get("path") or p.get("name")
    ]


def sources(root: Path, cwd: Path) -> Iterator[tuple[str, Path]]:
    """Workspace description files near the current directory."""
    for base in dict.fromkeys([cwd, root]):
        for conf in (base / "conf/bblayers.conf", base / "build/conf/bblayers.conf"):
            if conf.is_file():
                yield "bblayers", conf
        for kas in sorted(base.glob("kas*.y*ml")):
            yield "kas", kas
        if (base / ".repo/manifest.xml").is_file():
            yield "manifest", base


def member_dirs(root: Path, cwd: Path) -> list[tuple[str, Path]]:
    readers = {"bblayers": bblayers, "kas": kas_repos, "manifest": manifest_projects}
    found: dict[Path, str] = {}
    for kind, source in sources(root, cwd):
        for path in readers[kind](source):
            if path.is_dir():
                found.setdefault(path, kind)
    return [(kind, path) for path, kind in found.items()]
