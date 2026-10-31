# SPDX-License-Identifier: GPL-3.0-or-later
"""Schema, versioned migrations and the connection factory."""

import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from survivemem import paths

KINDS = ("fact", "preference", "project", "decision", "environment", "pitfall", "candidate")
SOURCES = ("cli", "mcp", "hook", "tool", "import", "investigation")

# memories.rid is an explicit INTEGER PRIMARY KEY: the FTS tables use it as content_rowid,
# and VACUUM may renumber an implicit rowid, which would silently detach the index.
SCHEMA_V1 = """
CREATE TABLE memories (
  rid         INTEGER PRIMARY KEY,
  id          TEXT NOT NULL UNIQUE,
  content     TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('fact','preference','project','decision',
                                            'environment','pitfall','candidate')),
  project     TEXT,
  scope       TEXT NOT NULL DEFAULT 'project',
  source      TEXT NOT NULL CHECK (source IN ('cli','mcp','hook','tool','import',
                                              'investigation')),
  refs        TEXT,
  anchor      TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  deleted_at  INTEGER
);
CREATE INDEX memories_project ON memories(project, kind, deleted_at);
CREATE INDEX memories_updated ON memories(updated_at);
CREATE TABLE memory_tags (
  memory_id TEXT NOT NULL REFERENCES memories(id),
  tag       TEXT NOT NULL,
  PRIMARY KEY (memory_id, tag)
);
CREATE INDEX memory_tags_tag ON memory_tags(tag);
CREATE TABLE projects (id TEXT PRIMARY KEY, label TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE project_identities (
  identity    TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  kind        TEXT NOT NULL CHECK (kind IN ('remote','root','path','name')),
  added_at    INTEGER NOT NULL
);
CREATE INDEX project_identities_project ON project_identities(project_id);
CREATE TABLE checkouts (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  path        TEXT NOT NULL UNIQUE,
  label       TEXT,
  last_branch TEXT,
  last_head   TEXT,
  last_config TEXT,
  last_seen   INTEGER NOT NULL,
  present     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX checkouts_project ON checkouts(project_id);
CREATE TABLE project_links (parent TEXT NOT NULL, child TEXT NOT NULL, PRIMARY KEY (parent, child));
CREATE VIRTUAL TABLE memories_fts USING fts5(
  content, content='memories', content_rowid='rid', tokenize = "unicode61 tokenchars '_-./:'");
CREATE VIRTUAL TABLE memories_tri USING fts5(
  content, content='memories', content_rowid='rid', tokenize = 'trigram');
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content) VALUES (new.rid, new.content);
  INSERT INTO memories_tri(rowid, content) VALUES (new.rid, new.content);
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rid, old.content);
  INSERT INTO memories_tri(memories_tri, rowid, content) VALUES ('delete', old.rid, old.content);
END;
CREATE TRIGGER memories_au AFTER UPDATE OF content ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rid, old.content);
  INSERT INTO memories_tri(memories_tri, rowid, content) VALUES ('delete', old.rid, old.content);
  INSERT INTO memories_fts(rowid, content) VALUES (new.rid, new.content);
  INSERT INTO memories_tri(rowid, content) VALUES (new.rid, new.content);
END;
"""

SQL_GET_VERSION = "SELECT version FROM schema_version"
SQL_CREATE_VERSION = "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
SQL_INIT_VERSION = (
    "INSERT INTO schema_version (version) SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM schema_version)"
)
SQL_SET_VERSION = "UPDATE schema_version SET version = ?"


class DBError(Exception):
    pass


# Working state that must survive context compaction: ephemeral, per session, never searched.
SCHEMA_V2 = """
CREATE TABLE session_state (
  session_id  TEXT PRIMARY KEY,
  project     TEXT,
  state       TEXT NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX session_state_project ON session_state(project, updated_at);
"""


def _migrate_1(db: sqlite3.Connection) -> None:
    for statement in _statements(SCHEMA_V1):
        db.execute(statement)


def _migrate_2(db: sqlite3.Connection) -> None:
    for statement in _statements(SCHEMA_V2):
        db.execute(statement)


MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (_migrate_1, _migrate_2)
SCHEMA_VERSION = len(MIGRATIONS)


def _statements(script: str) -> Iterator[str]:
    """Split a schema script into statements; trigger bodies end with END;."""
    buf: list[str] = []
    for line in script.strip().splitlines():
        buf.append(line)
        text = "\n".join(buf)
        if sqlite3.complete_statement(text):
            yield text
            buf = []
    if "".join(buf).strip():
        raise ValueError("incomplete SQL statement in schema script")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    """ULID: 48-bit millisecond timestamp, 80 random bits, Crockford base32."""
    value = (now_ms() << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE … COMMIT; joins an already open transaction instead of nesting."""
    if db.in_transaction:
        yield db
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def schema_version(db: sqlite3.Connection) -> int:
    try:
        row = db.execute(SQL_GET_VERSION).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def migrate(db: sqlite3.Connection) -> int:
    """Apply pending migrations, each in its own transaction; return the resulting version."""
    db.execute(SQL_CREATE_VERSION)
    db.execute(SQL_INIT_VERSION)
    current = schema_version(db)
    if current > SCHEMA_VERSION:
        raise DBError(f"database schema v{current} is newer than this survivemem v{SCHEMA_VERSION}")
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with transaction(db):
            MIGRATIONS[version - 1](db)
            db.execute(SQL_SET_VERSION, (version,))
    return SCHEMA_VERSION


def backup_before_migration(db: sqlite3.Connection, path: Path) -> Path | None:
    """memory.db.bak-<version> before upgrading a populated DB; downgrade = restore that file.
    An existing backup of the same version is kept: it is the one taken before the first try."""
    current = schema_version(db)
    if not 0 < current < SCHEMA_VERSION:
        return None
    target = path.with_name(f"{path.name}.bak-{current}")
    if not target.exists():
        with closing(sqlite3.connect(target)) as copy:
            db.backup(copy)
    return target


def _is_writable(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)
    return True


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (and migrate) the DB. A read-only file opens read-only; writes then fail loudly."""
    path = path or paths.db_path()
    try:
        if not _is_writable(path):
            return _connect_readonly(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        backup_before_migration(db, path)
        migrate(db)
    except sqlite3.Error as e:
        raise DBError(f"{path}: {e}") from e
    return db


# A WAL database in a directory we cannot write has no -shm file to coordinate readers, and
# mode=ro then cannot read at all; immutable=1 reads the main file without locking instead.
READONLY_URIS = ("file:{}?mode=ro", "file:{}?mode=ro&immutable=1")


def _connect_readonly(path: Path) -> sqlite3.Connection:
    for template in READONLY_URIS:
        db = sqlite3.connect(
            template.format(path),
            uri=True,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        db.row_factory = sqlite3.Row
        try:
            row = db.execute(SQL_GET_VERSION).fetchone()
        except sqlite3.OperationalError:
            db.close()
            continue
        if row is None or row[0] != SCHEMA_VERSION:
            db.close()
            raise DBError(f"{path}: read-only database is not at schema v{SCHEMA_VERSION}")
        return db
    raise DBError(f"{path}: cannot open read-only database")
