# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from survivemem import db as dbmod
from survivemem import paths, store
from tests.helpers import make_repo
from tests.jsonschema_lite import validate
from tests.test_cli import Runner

GOLDEN = Path(__file__).parent / "golden"
NOW = 1_793_000_000_000  # 2026-10-26T09:33:20Z
DAY = 86_400_000


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Runner:
    return Runner(capsys)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = make_repo(tmp_path / "w/yocto-bsp", {"origin": "https://example.com/bsp.git"})
    monkeypatch.chdir(path)
    return path


def defs(run: Runner) -> dict[str, Any]:
    code, out, _ = run("schema")
    assert code == 0
    found: dict[str, Any] = json.loads(out)["$defs"]
    return found


def test_json_output_matches_schema(run: Runner, repo: Path) -> None:
    schemas = defs(run)
    m = run.json(
        "add",
        "Kernel pinned to 6.6 LTS.",
        "--kind",
        "decision",
        "--tag",
        "kernel",
        "--ref",
        '{"type": "file", "path": "linux.bb"}',
    )
    run.json("state", "set", "--goal", "g", "--next", "n")
    checks = [
        (m, "memory"),
        (run.json("show", m["id"]), "memory"),
        *[(x, "memory") for x in run.json("list")],
        *[(x, "memory") for x in run.json("search", "kernel")],
        *[(x, "project") for x in run.json("projects")],
        (run.json("state", "show")["state"], "checkpoint"),
    ]
    for instance, name in checks:
        assert validate(instance, schemas[name]) == [], (name, instance)


def test_show_edit_restore(run: Runner, repo: Path) -> None:
    m = run.json(
        "add",
        "uart on ttyUSB1",
        "--kind",
        "environment",
        "--ref",
        '{"type": "symbol", "path": "drivers/tty/x.c", "symbol": "probe", "commit": "abc123"}',
    )
    edited = run.json(
        "edit", m["id"][:10], "--content", "uart on ttyUSB2", "--add-tag", "env", "--global"
    )
    assert edited["content"] == "uart on ttyUSB2" and edited["tags"] == ["env"]
    assert edited["project"] is None
    run.json("forget", m["id"], "--yes")
    shown = run.json("show", m["id"])
    assert shown["deleted_at"] is not None
    assert run.json("restore", m["id"])["deleted_at"] is None
    code, out, _ = run("show", m["id"], "--format", "table")
    assert code == 0 and "ref      git show abc123:drivers/tty/x.c" in out


def test_status_and_brief(run: Runner, repo: Path) -> None:
    settings = Path.home() / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "survivemem hook prompt"}]}
                    ]
                }
            }
        )
    )
    (Path.home() / ".claude.json").write_text(json.dumps({"mcpServers": {"survivemem": {}}}))
    run.json("add", "a fact")
    status = run.json("status")
    assert status["integration"]["hooks"]["UserPromptSubmit"] is True
    assert status["integration"]["hooks"]["Stop"] is False
    assert status["integration"]["mcp"] is True and status["memories"] == 1
    code, out, _ = run("status", "--format", "table")
    assert code == 0 and "hooks     1/7 registered; missing: PreToolUse" in out
    brief = run.json("brief")
    assert set(brief) >= {"projects", "recent", "candidates", "status"}
    assert brief["current_project"] == "yocto-bsp" and len(brief["recent"]) == 1


def test_project_commands(
    run: Runner, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.json("add", "bsp fact")
    other = make_repo(tmp_path / "w/meta-foo", {"origin": "https://example.com/meta-foo.git"})
    monkeypatch.chdir(other)
    run.json("add", "layer fact")
    projects = {p["label"]: p for p in run.json("projects")}
    assert projects["meta-foo"]["current"] and not projects["yocto-bsp"]["current"]
    shown = run.json("projects", "show", "yocto-bsp")
    assert shown["identities"][0]["identity"] == "remote:example.com/bsp"
    assert [m["content"] for m in shown["recent"]] == ["bsp fact"]
    assert run.json("projects", "rename", "meta-foo", "layer")["label"] == "layer"
    ids = run.json("projects", "identities", "layer")
    assert any(i["identity"] == "remote:example.com/meta-foo" for i in ids)
    code, _, err = run("projects", "alias", "add", "yocto-bsp", "remote:example.com/meta-foo")
    assert code == 1 and "--yes" in err
    moved = run.json(
        "projects", "alias", "add", "yocto-bsp", "remote:example.com/meta-foo", "--yes"
    )
    assert moved["project_id"] == projects["yocto-bsp"]["id"]
    run.json("projects", "alias", "rm", "remote:example.com/meta-foo")
    merged = run.json("projects", "merge", "layer", "yocto-bsp", "--yes")
    assert merged["label"] == "yocto-bsp"
    assert [p["label"] for p in run.json("projects")] == ["yocto-bsp"]
    assert len(run.json("list", "--project", "yocto-bsp")) == 2


def seed_fixed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO projects VALUES ('01JB0000000000000000000BSP', 'yocto-bsp', ?)",
        (NOW - 30 * DAY,),
    )
    db.execute(
        "INSERT INTO project_identities VALUES ('remote:example.com/bsp',"
        " '01JB0000000000000000000BSP', 'remote', ?)",
        (NOW - 30 * DAY,),
    )
    rows = [
        (
            "01JB4K0000000000000000000A",
            "Kernel pinned to 6.6 LTS; vendor BSP breaks on 6.12.",
            "decision",
            "01JB0000000000000000000BSP",
            "project",
            7,
            "kernel bsp",
        ),
        (
            "01JB4K0000000000000000000B",
            "MACHINE=cyclone5",
            "environment",
            "01JB0000000000000000000BSP",
            "branch:socfpga-6.6",
            2,
            "env",
        ),
        (
            "01JB4K0000000000000000000C",
            "Prefers terse answers.\nNo emojis.",
            "preference",
            None,
            "project",
            40,
            "",
        ),
    ]
    for mid, content, kind, project, scope, age_days, tags in rows:
        db.execute(
            "INSERT INTO memories (id, content, kind, project, scope, source, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, 'cli', ?, ?)",
            (mid, content, kind, project, scope, NOW - age_days * DAY, NOW - age_days * DAY),
        )
        db.executemany("INSERT INTO memory_tags VALUES (?, ?)", [(mid, t) for t in tags.split()])


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("list-table", ["list", "--all", "--format", "table"]),
        ("list-md", ["list", "--all", "--format", "md"]),
        ("projects-table", ["projects", "--format", "table"]),
        ("show-table", ["show", "01JB4K0000000000000000000B", "--format", "table"]),
        ("search-table", ["search", "kernel", "--all", "--format", "table"]),
    ],
)
def test_golden_output(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, args: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with closing(dbmod.connect(paths.db_path())) as db:
        seed_fixed(db)
    monkeypatch.setattr(time, "time_ns", lambda: NOW * 1_000_000)
    code, out, err = run(*args)
    assert code == 0, err
    golden = GOLDEN / f"{name}.txt"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(out)
    assert out == golden.read_text()


def test_store_projects_summary_matches(db: sqlite3.Connection) -> None:
    seed_fixed(db)
    (summary,) = store.project_summaries(db)
    assert summary.memories == 2 and summary.primary_identity == "remote:example.com/bsp"
