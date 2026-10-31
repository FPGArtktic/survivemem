# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from survivemem import db as dbmod
from survivemem import doctor, paths
from survivemem.config import Config
from tests.helpers import make_repo
from tests.test_cli import Runner
from tests.test_cli_browse import GOLDEN, NOW, seed_fixed


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Runner:
    return Runner(capsys)


def export_jsonl(run: Runner, *args: str) -> list[dict[str, object]]:
    code, out, err = run("export", *args)
    assert code == 0, err
    return [json.loads(line) for line in out.splitlines()]


def test_export_import_roundtrip(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "bsp", {"origin": "https://example.com/bsp.git"})
    monkeypatch.chdir(repo)
    run.json("add", "Kernel pinned to 6.6 LTS.", "--kind", "decision", "--tag", "kernel")
    run.json("add", "prefers terse answers", "--global", "--kind", "preference")
    lines = export_jsonl(run)
    assert lines[0]["record"] == "project"
    identities = lines[0]["identities"]
    assert isinstance(identities, list)
    assert {"remote:example.com/bsp", f"path:{repo}/.git"} <= set(identities)
    assert {line.get("kind") for line in lines[1:]} == {"decision", "preference"}

    monkeypatch.setenv("SURVIVEMEM_DB", str(tmp_path / "other.db"))
    stream = "\n".join(json.dumps(x) for x in lines) + "\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(stream))
    result = run.json("import")
    assert result == {"memories_added": 2, "memories_skipped": 0, "projects_added": 1, "errors": []}
    monkeypatch.setattr("sys.stdin", io.StringIO(stream))
    assert run.json("import")["memories_skipped"] == 2
    # the imported project is recognised from the repo through its identities
    assert {m["content"] for m in run.json("list")} == {
        "Kernel pinned to 6.6 LTS.",
        "prefers terse answers",
    }
    assert run.json("show", str(lines[1]["id"]))["tags"] == lines[1]["tags"]


def test_import_reports_bad_lines(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    good = {"id": "01JB0000000000000000000001", "content": "fine", "kind": "fact"}
    secret = {"id": "01JB0000000000000000000002", "content": "api_key=abcdefgh123", "kind": "fact"}
    stream = "\n".join([json.dumps(good), "{not json", json.dumps(secret), "[1]", ""])
    monkeypatch.setattr("sys.stdin", io.StringIO(stream))
    result = run.json("import")
    assert result["memories_added"] == 1 and len(result["errors"]) == 3
    assert run.json("show", good["id"])["source"] == "import"


def test_export_markdown_golden(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with closing(dbmod.connect(paths.db_path())) as db:
        seed_fixed(db)
    monkeypatch.setattr(time, "time_ns", lambda: NOW * 1_000_000)
    code, out, _ = run("export", "--format", "md")
    golden = GOLDEN / "export-md.txt"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(out)
    assert code == 0 and out == golden.read_text()


def test_doctor_reports_and_fails_on_broken_fts(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    report = run.json("doctor")
    levels = {c["name"]: c["level"] for c in report["checks"]}
    assert report["ok"] and levels["integrity"] == "ok" and levels["hooks"] == "warn"
    with closing(dbmod.connect(paths.db_path())) as db:
        db.execute("DROP TRIGGER memories_ai")
        db.execute(
            "INSERT INTO memories (id, content, kind, source, created_at, updated_at)"
            " VALUES ('x', 'unindexed', 'fact', 'cli', 0, 0)"
        )
    code, out, _ = run("doctor", "--json")
    assert code == 3 and not json.loads(out)["ok"]


def test_doctor_network_fs_and_name_collision(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "filesystem_type", lambda path: "nfs4")
    assert doctor.check_filesystem().level == "fail"
    db.execute("INSERT INTO projects VALUES ('p', 'bsp', 0)")
    for ident, kind in (("name:bsp", "name"), ("root:aaa", "root"), ("root:bbb", "root")):
        db.execute("INSERT INTO project_identities VALUES (?, 'p', ?, 0)", (ident, kind))
    assert doctor.check_name_collisions(db).level == "warn"
    assert {c.name for c in doctor.run_checks(db, Config(ollama_url="http://127.0.0.1:9"))} >= {
        "integrity",
        "fts",
        "vec",
        "ollama",
        "hooks",
        "mcp",
        "profile",
    }
