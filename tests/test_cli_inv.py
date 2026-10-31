# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from tests.helpers import make_repo
from tests.test_cli import Runner

LOGS = Path(__file__).parent / "fixtures" / "logs"


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Runner:
    return Runner(capsys)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = make_repo(tmp_path / "linux", {"origin": "https://example.com/linux.git"})
    monkeypatch.chdir(path)
    return path


def test_inv_workflow(run: Runner, repo: Path) -> None:
    run.json("add", "MACHINE=cyclone5", "--kind", "environment")
    opened = run.json("inv", "open", "UART hang", "--symptom", "hangs after fpga load")
    assert opened["status"] == "open" and opened["rendered"].startswith("INVESTIGATION ")
    code, _, err = run("inv", "open", "second", "--symptom", "x")
    assert code == 1 and "already open" in err
    run.json("inv", "log", "hypothesis", "bridges not enabled")
    exp = run.json(
        "inv",
        "log",
        "experiment",
        "reload overlay",
        "--command",
        "cd /tmp && sudo dmesg | tail",
        "--result",
        "bad",
        "--evidence",
        str(LOGS / "dmesg.log"),
        "--tool",
        "dmesg",
    )
    assert exp["command"] == "dmesg" and exp["evidence_id"] and exp["excerpt_sha"]
    run.json("inv", "log", "ruled_out", "not the clock: measured 50 MHz")
    shown = run.json("inv", "show", "--full")
    assert [e["kind"] for e in shown["timeline"]] == ["hypothesis", "experiment", "ruled_out"]
    assert "RULED OUT (1, last 40)" in shown["rendered"]
    assert len(run.json("inv", "list")) == 1
    assert run.json("inv", "list", "--grep", "clock")[0]["id"] == opened["id"]
    loops = run.json("inv", "loops")
    assert loops == []
    code, out, _ = run("inv", "bisect", "--format", "table")
    assert code == 0 and "#2" in out


def test_inv_pause_resume_close_export(run: Runner, repo: Path) -> None:
    first = run.json("inv", "open", "one", "--symptom", "s1")
    assert run.json("inv", "pause")["status"] == "paused"
    second = run.json("inv", "open", "two", "--symptom", "s2")
    code, _, err = run("inv", "resume", first["id"])
    assert code == 1 and "another investigation is open" in err
    run.json("inv", "pause", second["id"])
    assert run.json("inv", "resume", first["id"])["status"] == "open"
    attached = run.json("inv", "attach", str(LOGS / "quartus-fit.log"), "--tool", "quartus")
    assert attached["path"].endswith("001-quartus-fit.log")
    closed = run.json("inv", "close", "--root-cause", "too many LABs", "--fix", "move NCO to HPS")
    assert closed["status"] == "solved" and closed["pitfall"]["kind"] == "pitfall"
    assert run.json("search", "too many LABs")[0]["kind"] == "pitfall"
    code, md, _ = run("inv", "export", first["id"], "--format", "md")
    assert code == 0 and md.startswith("# one\n") and "## Evidence" in md
    code, _, err = run("inv", "show")
    assert code == 2 and "no open investigation" in err


def test_ingest_log_candidate_or_evidence(run: Runner, repo: Path) -> None:
    result = run.json("ingest-log", str(LOGS / "bitbake.log"), "--tool", "bitbake")
    assert result["stored"] == "candidate" and "do_rootfs" in result["tags"]
    assert run.json("list", "--kind", "candidate")[0]["source"] == "tool"
    run.json("inv", "open", "rootfs", "--symptom", "do_rootfs fails")
    result = run.json("ingest-log", str(LOGS / "bitbake.log"), "--tool", "bitbake")
    assert result["stored"] == "evidence" and result["seq"] == 1


def test_inv_outside_project(run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, err = run("inv", "open", "t", "--symptom", "s")
    assert code == 1 and "project" in err
