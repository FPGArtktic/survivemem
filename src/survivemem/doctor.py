# SPDX-License-Identifier: GPL-3.0-or-later
"""`survivemem doctor`: integrity and integration checks, each ok / warn / fail."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from survivemem import db as dbmod
from survivemem import embed, paths, payload, store
from survivemem.config import Config, ConfigError, load_config

NETWORK_FS = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "9p", "afs"})


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str  # ok | warn | fail
    detail: str


def check_integrity(db: sqlite3.Connection) -> Check:
    embed.load_extension(db)  # memory_vec cannot be checked without its module
    result = dbmod.integrity(db)
    return Check("integrity", "ok" if result == "ok" else "fail", str(result))


def check_schema(db: sqlite3.Connection) -> Check:
    version = dbmod.schema_version(db)
    ok = version == dbmod.SCHEMA_VERSION
    return Check(
        "schema", "ok" if ok else "fail", f"v{version}, code expects v{dbmod.SCHEMA_VERSION}"
    )


def check_fts(db: sqlite3.Connection) -> Check:
    if store.fts_ok(db):
        return Check("fts", "ok", "memories_fts and memories_tri consistent")
    return Check("fts", "fail", "index out of sync with memories; run: survivemem reindex")


def check_vec(db: sqlite3.Connection) -> Check:
    vec = embed.status(db)
    if vec.extension and vec.table:
        return Check("vec", "ok", f"enabled, {vec.embedded} memories embedded")
    why = "sqlite-vec not installed" if not vec.extension else "no memory_vec (run reindex)"
    return Check("vec", "ok", f"disabled ({why}); search uses FTS5 only")


def check_ollama(cfg: Config) -> Check:
    if embed.ollama_reachable(cfg):
        return Check("ollama", "ok", f"{cfg.ollama_url} reachable")
    return Check("ollama", "ok", f"{cfg.ollama_url} unreachable; vectors and summaries off")


def check_config() -> Check:
    try:
        load_config()
    except ConfigError as e:
        return Check("config", "fail", str(e))
    path = paths.config_path()
    return Check("config", "ok", str(path) if path.exists() else "defaults (no config.toml)")


def check_hooks() -> Check:
    hooks = payload.integration_payload(Path.cwd())["hooks"]
    missing = [event for event, ok in hooks.items() if not ok]
    if not missing:
        return Check("hooks", "ok", "all hook events registered")
    return Check("hooks", "warn", f"not registered: {', '.join(missing)} (see docs/hooks.md)")


def check_mcp() -> Check:
    if payload.integration_payload(Path.cwd())["mcp"]:
        return Check("mcp", "ok", "survivemem MCP server registered")
    return Check("mcp", "warn", "not registered; see docs/mcp.md")


def check_filesystem() -> Check:
    """WAL locking is unreliable on network filesystems (§13)."""
    fs = filesystem_type(paths.db_path())
    if fs in NETWORK_FS:
        return Check("filesystem", "fail", f"DB is on {fs}; WAL needs a local filesystem")
    return Check("filesystem", "ok", fs or "unknown")


def filesystem_type(path: Path) -> str | None:
    """Filesystem type of the longest mount point that contains the DB's directory."""
    target = path.resolve().parent
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return None
    best_len, fstype = -1, None
    for line in mounts:
        fields = line.split()
        if len(fields) < 3:
            continue
        mount = Path(fields[1])
        contains = target == mount or mount in target.parents
        if contains and len(fields[1]) > best_len:
            best_len, fstype = len(fields[1]), fields[2]
    return fstype


def check_name_collisions(db: sqlite3.Connection) -> Check:
    """A .survivemem.toml name shared by repos with different root commits (§9)."""
    clashes = []
    for summary in store.project_summaries(db):
        ids = store.identities_of(db, summary.project.id)
        roots = [i for i in ids if i.kind == "root"]
        if any(i.kind == "name" for i in ids) and len(roots) > 1:
            clashes.append(f"{summary.project.label} ({len(roots)} root commits)")
    if clashes:
        return Check("names", "warn", "one name, unrelated histories: " + ", ".join(clashes))
    return Check("names", "ok", "no .survivemem.toml name collisions")


def check_clones(db: sqlite3.Connection) -> Check:
    clones = store.list_checkouts(db)
    gone = [c for c in clones if c.present and not Path(c.path).exists()]
    if gone:
        return Check(
            "clones",
            "warn",
            f"{len(gone)} of {len(clones)} recorded clones vanished; run: survivemem clones prune",
        )
    return Check("clones", "ok", f"{len(clones)} clones recorded")


def check_profile() -> Check:
    return Check("profile", "ok", "A (native): hooks, CLI and MCP server open the DB directly")


def run_checks(db: sqlite3.Connection, cfg: Config) -> list[Check]:
    checks: list[Callable[[], Check]] = [
        lambda: check_integrity(db),
        lambda: check_schema(db),
        lambda: check_fts(db),
        check_filesystem,
        check_config,
        lambda: check_vec(db),
        lambda: check_ollama(cfg),
        check_hooks,
        check_mcp,
        lambda: check_name_collisions(db),
        lambda: check_clones(db),
        check_profile,
    ]
    return [check() for check in checks]


def describe(c: Check) -> str:
    return f"{c.level.upper():4} {c.name:11} {c.detail}"


def stale_memories(db: sqlite3.Connection, project: str | None, anchor: str | None) -> list[Check]:
    """--stale: memories of this project whose anchor no longer matches the current clone
    (another head, or the same head with another .config)."""
    if project is None or anchor is None:
        return []
    head, _, config = anchor.partition("+")
    view = store.View((project,), include_global=False)
    checks = []
    for m in store.list_memories(db, view):
        if not m.anchor:
            continue
        m_head, _, m_config = m.anchor.partition("+")
        same_head = head.startswith(m_head) or m_head.startswith(head)
        if not same_head or (m_config and config and m_config != config[: len(m_config)]):
            checks.append(
                Check(
                    "stale",
                    "warn",
                    f"{m.id[:8]} anchor {m.anchor[:16]} ≠ {anchor[:16]}: {m.content[:80]}",
                )
            )
    return checks
