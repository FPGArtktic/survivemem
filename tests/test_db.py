# SPDX-License-Identifier: GPL-3.0-or-later
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from survivemem import db as dbmod


def tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_is_fully_migrated(db: sqlite3.Connection) -> None:
    assert dbmod.schema_version(db) == dbmod.SCHEMA_VERSION
    assert {
        "memories",
        "memory_tags",
        "projects",
        "project_identities",
        "checkouts",
        "project_links",
        "memories_fts",
        "memories_tri",
        "schema_version",
    } <= tables(db)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    dbmod.connect(path).close()
    conn = dbmod.connect(path)
    assert dbmod.migrate(conn) == dbmod.SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 1


def test_migration_1_from_empty_file(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "empty.db", isolation_level=None)
    assert dbmod.schema_version(conn) == 0
    dbmod.migrate(conn)
    assert dbmod.schema_version(conn) == dbmod.SCHEMA_VERSION
    assert "memories" in tables(conn)


def migrate_to(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(dbmod.SQL_CREATE_VERSION)
    conn.execute(dbmod.SQL_INIT_VERSION)
    for v in range(1, version + 1):
        dbmod.MIGRATIONS[v - 1](conn)
    conn.execute(dbmod.SQL_SET_VERSION, (version,))


def test_migration_2_adds_session_state_to_v1(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "v1.db", isolation_level=None)
    migrate_to(conn, 1)
    conn.execute("INSERT INTO projects VALUES ('p1', 'kept', 0)")
    assert "session_state" not in tables(conn)
    dbmod.migrate(conn)
    assert dbmod.schema_version(conn) >= 2 and "session_state" in tables(conn)
    assert conn.execute("SELECT label FROM projects").fetchone()[0] == "kept"


def test_backup_before_migration(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    migrate_to(conn, 1)
    conn.execute("INSERT INTO projects VALUES ('p1', 'kept', 0)")
    conn.close()
    dbmod.connect(path).close()
    with closing(sqlite3.connect(tmp_path / "old.db.bak-1")) as backup:
        assert dbmod.schema_version(backup) == 1
        assert backup.execute("SELECT label FROM projects").fetchone()[0] == "kept"
    assert not (tmp_path / "fresh.db.bak-0").exists()
    dbmod.connect(tmp_path / "fresh.db").close()
    assert sorted(p.name for p in tmp_path.glob("*.bak-*")) == ["old.db.bak-1"]


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "new.db"
    conn = dbmod.connect(path)
    conn.execute("UPDATE schema_version SET version = 999")
    conn.close()
    with pytest.raises(dbmod.DBError, match="newer"):
        dbmod.connect(path)


def test_readonly_file_opens_readonly(tmp_path: Path) -> None:
    path = tmp_path / "ro.db"
    dbmod.connect(path).close()
    os.chmod(path, 0o444)
    conn = dbmod.connect(path)
    assert dbmod.schema_version(conn) == dbmod.SCHEMA_VERSION
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO projects VALUES ('x', 'x', 0)")


def test_readonly_wal_database_in_readonly_directory(tmp_path: Path) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    path = folder / "m.db"
    conn = dbmod.connect(path)
    conn.execute("INSERT INTO projects VALUES ('p1', 'kept', 0)")
    conn.close()
    path.chmod(0o444)
    folder.chmod(0o555)
    try:
        ro = dbmod.connect(path)
        assert ro.execute("SELECT label FROM projects").fetchone()[0] == "kept"
        ro.close()
    finally:
        folder.chmod(0o755)


def test_ulid_shape_and_order() -> None:
    first = dbmod.new_id()
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", first)
    later = [dbmod.new_id() for _ in range(5)]
    assert all(i[:10] >= first[:10] for i in later)


def test_transaction_rolls_back(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), dbmod.transaction(db):
        db.execute("INSERT INTO projects VALUES ('p1', 'x', 0)")
        raise RuntimeError
    assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 0


def test_fts_triggers_follow_content(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO memories (id, content, kind, source, created_at, updated_at)"
        " VALUES ('a', 'alpha bravo', 'fact', 'cli', 0, 0)"
    )
    db.execute("UPDATE memories SET content = 'charlie delta' WHERE id = 'a'")
    fts = "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH ?"
    assert db.execute(fts, ("alpha",)).fetchone()[0] == 0
    assert db.execute(fts, ("charlie",)).fetchone()[0] == 1
    db.execute("DELETE FROM memories WHERE id = 'a'")
    assert db.execute(fts, ("charlie",)).fetchone()[0] == 0
