# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from survivemem import cli
from tests.helpers import make_repo


class Runner:
    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys = capsys

    def __call__(self, *args: str) -> tuple[int, str, str]:
        code = cli.run(list(args))
        out = self.capsys.readouterr()
        return code, out.out, out.err

    def json(self, *args: str) -> Any:
        code, out, err = self(*args, "--json")
        assert code == 0, err
        return json.loads(out)


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Runner:
    return Runner(capsys)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = make_repo(tmp_path / "w/linux-de10", {"origin": "https://example.com/linux.git"})
    monkeypatch.chdir(path)
    return path


def test_help_fits_in_40_lines(run: Runner) -> None:
    code, out, _ = run("--help")
    assert code == 0 and len(out.splitlines()) <= 40
    code, out, _ = run("help", "add")
    assert code == 0 and "--kind" in out


def test_add_and_list_json_contract(run: Runner, repo: Path) -> None:
    added = run.json("add", "Kernel pinned to 6.6 LTS.", "--kind", "decision", "--tag", "kernel")
    assert added["kind"] == "decision" and added["tags"] == ["kernel"]
    assert added["project_label"] == "linux-de10"
    listed = run.json("list")
    assert [m["id"] for m in listed] == [added["id"]]
    for field in (
        "id",
        "content",
        "kind",
        "project",
        "scope",
        "source",
        "refs",
        "anchor",
        "created_at",
        "created_at_iso",
        "updated_at",
        "updated_at_iso",
        "deleted_at",
    ):
        assert field in listed[0]
    assert listed[0]["created_at_iso"].endswith("Z")


def test_project_filters(
    run: Runner, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.json("add", "repo fact")
    run.json("add", "global fact", "--global", "--kind", "preference")
    other = make_repo(tmp_path / "other", {"origin": "https://example.com/other.git"})
    monkeypatch.chdir(other)
    assert [m["content"] for m in run.json("list")] == ["global fact"]
    assert [m["content"] for m in run.json("list", "--project", "linux-de10")] == ["repo fact"]
    assert len(run.json("list", "--all")) == 2
    assert [m["content"] for m in run.json("list", "--global")] == ["global fact"]


def test_search_json_and_explain(run: Runner, repo: Path) -> None:
    run.json("add", "The de10-nano UART is on /dev/ttyUSB1.")
    hits = run.json("search", "de10-nano uart")
    assert hits[0]["matched_by"] == ["fts"] and hits[0]["score"] > 0
    code, out, _ = run("search", "de10-nano", "--explain", "--format", "table")
    assert code == 0 and "bm25" in out.splitlines()[0]


def test_scope_branch_and_checkout(run: Runner, repo: Path) -> None:
    b = run.json("add", "only on main", "--scope", "branch")
    c = run.json("add", "only in this clone", "--scope", "checkout")
    assert b["scope"] == "branch:main" and c["scope"].startswith("checkout:")
    code, out, _ = run("list", "--format", "table", "--wide")
    assert code == 0
    assert "[branch main]" in out and "[clone linux-de10]" in out


def test_forget_requires_yes_without_tty(run: Runner, repo: Path) -> None:
    mid = run.json("add", "short-lived")["id"]
    code, _, err = run("forget", mid)
    assert code == 1 and "--yes" in err
    assert run.json("forget", mid[:10], "--yes")[0]["deleted_at"] is not None
    assert run.json("list") == []


def test_exit_codes_and_json_errors(run: Runner, repo: Path) -> None:
    run.json("add", "one")
    run.json("add", "two")
    code, _, err = run("forget", "ZZZZZZ", "--yes", "--json")
    assert code == 2 and json.loads(err)["error"]["code"] == 2
    code, _, err = run("forget", "0", "--yes")
    assert code == 4 and "ambiguous" in err
    code, _, err = run("add", "x", "--kind", "bogus")
    assert code == 1
    code, _, _ = run("add", "x", "--ref", "{not json")
    assert code == 1


def test_db_error_exit_code(
    run: Runner, repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "not-a-db"
    bad.write_text("garbage" * 100)
    monkeypatch.setenv("SURVIVEMEM_DB", str(bad))
    code, _, err = run("list")
    assert code == 3 and json.loads(err)["error"]["code"] == 3
    code, _, err = run("list", "--format", "table")
    assert code == 3 and err.startswith("survivemem:")


def test_quiet_prints_ids(run: Runner, repo: Path) -> None:
    mid = run.json("add", "x")["id"]
    code, out, _ = run("list", "--quiet")
    assert code == 0 and out.strip() == mid


def test_table_and_markdown_output(run: Runner, repo: Path) -> None:
    run.json("add", "a" * 300, "--tag", "t")
    code, out, _ = run("list", "--format", "table")
    header, row = out.splitlines()
    assert header.split() == ["id", "kind", "project", "updated", "tags", "content"]
    assert "now" in row and "t" in row
    code, out, _ = run("list", "--format", "md")
    assert code == 0 and out.startswith("- [") and out.rstrip().endswith("#t")


def test_clones_commands(
    run: Runner, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = make_repo(tmp_path / "w/linux-up", {"origin": "https://example.com/linux.git"})
    run.json("list")
    monkeypatch.chdir(second)
    clones = run.json("clones")
    assert {Path(c["path"]).name for c in clones} == {"linux-de10", "linux-up"}
    labelled = run.json("clones", "label", clones[0]["id"][:6], "bisect scratch")
    assert labelled["label"] == "bisect scratch"
    diff = run.json("clones", "diff", clones[0]["id"], clones[1]["id"])
    assert diff["a"]["id"] == clones[0]["id"]
    monkeypatch.chdir(tmp_path)
    shutil.rmtree(repo)
    pruned = run.json("clones", "prune")
    assert len(pruned["marked_gone"]) == 1
    assert {c["present"] for c in run.json("clones", "--all")} == {0, 1}


def test_projects_here(run: Runner, repo: Path) -> None:
    here = run.json("projects", "here")
    assert here["step"] == "created" and here["project"]["label"] == "linux-de10"
    assert here["remotes"][0]["identity"] == "remote:example.com/linux"
    assert run.json("projects", "here")["step"] == "remote"
    code, out, _ = run("projects", "here", "--format", "table")
    assert code == 0 and "matched by: remote" in out


def test_outside_project(run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert run.json("add", "global by default")["project"] is None
    code, _, err = run("clones")
    assert code == 1 and "--project" in err


def test_hook_command_reads_stdin(run: Runner, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "x", "cwd": str(repo)})))
    code, out, _ = run("hook", "prompt")
    assert code == 0 and out == ""
