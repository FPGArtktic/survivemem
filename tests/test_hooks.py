# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from survivemem import db as dbmod
from survivemem import hooks, paths, store
from tests.helpers import make_repo

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_basic.jsonl"


def run_hook(name: str, payload: Any) -> str:
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    stdout = io.StringIO()
    assert hooks.run(name, stdin, stdout) == 0
    return stdout.getvalue()


@contextmanager
def open_db() -> Iterator[sqlite3.Connection]:
    # closed on exit: an open connection keeps -wal/-shm around and hides read-only bugs
    with closing(dbmod.connect(paths.db_path())) as db:
        yield db


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "w/linux-de10", {"origin": "https://example.com/linux.git"})


def project_of(repo: Path) -> str:
    run_hook("prompt", {"prompt": "", "cwd": str(repo)})
    with open_db() as db:
        return store.get_project(db, repo.name).id


def test_prompt_injects_relevant_memories(repo: Path) -> None:
    pid = project_of(repo)
    with open_db() as db:
        m = store.add_memory(db, "The de10-nano UART is /dev/ttyUSB1.", "environment", project=pid)
        store.add_memory(db, "Unrelated: prefers vim.", "preference")
    out = run_hook("prompt", {"prompt": "which tty is the de10-nano uart?", "cwd": str(repo)})
    assert out.startswith("<survivemem>\n- [environment] The de10-nano UART is /dev/ttyUSB1.")
    assert f"({m.id[:8]})" in out and "vim" not in out and out.endswith("</survivemem>\n")
    assert run_hook("prompt", {"prompt": "nothing matches zebra", "cwd": str(repo)}) == ""


def test_prompt_respects_budget(repo: Path) -> None:
    pid = project_of(repo)
    with open_db() as db:
        for i in range(30):
            store.add_memory(db, f"quartus fitter note {i} " + "x" * 900, "fact", project=pid)
    out = run_hook("prompt", {"prompt": "quartus fitter", "cwd": str(repo)})
    lines = out.splitlines()[1:-1]
    assert len(out) <= hooks.PROMPT_BUDGET
    assert len(lines) == hooks.SEARCH_LIMIT and all(len(line) <= 400 for line in lines)


def test_pitfall_ranked_first_on_error_like_prompt(repo: Path) -> None:
    pid = project_of(repo)
    with open_db() as db:
        store.add_memory(db, "do_rootfs do_rootfs do_rootfs is a bitbake task", "fact", project=pid)
        store.add_memory(
            db,
            "do_rootfs failed → root cause: bad IMAGE_INSTALL → fix: drop it",
            "pitfall",
            project=pid,
        )
    out = run_hook("prompt", {"prompt": "ERROR: do_rootfs failed", "cwd": str(repo)})
    assert out.splitlines()[1].startswith("- [pitfall]")


def test_checkout_line_and_root_hint(repo: Path, tmp_path: Path) -> None:
    other = make_repo(tmp_path / "w/linux-up", {"origin": "https://example.com/linux.git"})
    project_of(repo)
    out = run_hook("prompt", {"prompt": "hello", "cwd": str(other)})
    assert out.startswith("linux-de10 · this clone ")
    assert "others: " in out


def test_malformed_stdin_exits_zero_and_logs(repo: Path) -> None:
    assert run_hook("prompt", "{not json") == ""
    assert run_hook("prompt", "[1, 2]") == ""
    log = paths.log_path().read_text()
    assert "hook prompt failed" in log and "Traceback" in log


def test_git_missing_from_path_injects_global_only(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with open_db() as db:
        store.add_memory(db, "prefers terse answers about quartus", "preference")
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    out = run_hook("prompt", {"prompt": "quartus", "cwd": str(repo)})
    assert "prefers terse answers" in out


def test_readonly_db_still_injects(repo: Path) -> None:
    pid = project_of(repo)
    with open_db() as db:
        store.add_memory(db, "socfpga kernel lives on branch socfpga-6.6", "fact", project=pid)
    db_file = paths.db_path()
    db_file.chmod(0o444)
    db_file.parent.chmod(0o555)
    try:
        out = run_hook("prompt", {"prompt": "socfpga kernel branch", "cwd": str(repo)})
    finally:
        db_file.parent.chmod(0o755)
    assert "socfpga-6.6" in out


def test_stop_stores_candidate(repo: Path) -> None:
    payload = {
        "transcript_path": str(FIXTURE),
        "cwd": str(repo),
        "session_id": "s1",
        "stop_hook_active": False,
        "last_assistant_message": "Remove nonexistent-pkg from IMAGE_INSTALL.",
    }
    assert run_hook("stop", payload) == ""
    with open_db() as db:
        (cand,) = store.list_memories(db, store.ALL, kind="candidate")
        assert cand.source == "hook" and cand.project is not None
        assert cand.content == (
            "Why does do_rootfs fail on the de10-nano image?\n"
            "→ Remove nonexistent-pkg from IMAGE_INSTALL."
        )
        assert store.search(db, "IMAGE_INSTALL", store.ALL) == []


def test_stop_falls_back_to_transcript_answer(repo: Path) -> None:
    run_hook("stop", {"transcript_path": str(FIXTURE), "cwd": str(repo)})
    with open_db() as db:
        (cand,) = store.list_memories(db, store.ALL, kind="candidate")
    assert cand.content.endswith("Remove it from local.conf and rebuild.")


def test_stop_guards(repo: Path) -> None:
    run_hook("stop", {"transcript_path": str(FIXTURE), "cwd": str(repo), "stop_hook_active": True})
    run_hook("stop", {"cwd": str(repo), "last_assistant_message": "token ghp_" + "a" * 36})
    run_hook("stop", {"cwd": str(repo), "last_assistant_message": "   "})
    with open_db() as db:
        assert store.list_memories(db, store.ALL, kind="candidate") == []


def seed(db: sqlite3.Connection, project: str, n: int) -> None:
    words = ["kernel", "bitbake", "quartus", "fitter", "u-boot", "dts", "uart", "jtag", "sof"]
    now = dbmod.now_ms()
    rows = [
        (
            dbmod.new_id(),
            f"memory {i} about {words[i % 9]} and {words[(i * 7) % 9]} {i * 31}",
            "fact",
            project if i % 2 else None,
            "cli",
            now,
            now,
        )
        for i in range(n)
    ]
    with dbmod.transaction(db):
        db.executemany(
            "INSERT INTO memories (id, content, kind, project, source, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def test_prompt_latency_with_10000_memories(repo: Path) -> None:
    pid = project_of(repo)
    with open_db() as db:
        seed(db, pid, 10_000)
    payload = {"prompt": "why does the quartus fitter fail on the jtag chain?", "cwd": str(repo)}
    started = time.perf_counter()
    out = run_hook("prompt", payload)
    in_process = time.perf_counter() - started
    assert "<survivemem>" in out
    assert in_process < 0.3, f"in-process prompt hook took {in_process * 1000:.0f} ms"
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "survivemem", "hook", "prompt"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=True,
    )
    whole = time.perf_counter() - started
    assert "<survivemem>" in proc.stdout
    assert whole < 1.0, f"prompt hook process took {whole * 1000:.0f} ms"


def test_precompact_saves_heuristic_state_and_candidate(repo: Path) -> None:
    payload = {
        "session_id": "s9",
        "transcript_path": str(FIXTURE),
        "cwd": str(repo),
        "trigger": "auto",
        "custom_instructions": None,
    }
    assert run_hook("precompact", payload) == ""
    with open_db() as db:
        saved = store.get_state(db, "s9")
        assert saved is not None and saved.state["source"] == "hook"
        assert saved.state["in_progress"] == "fix IMAGE_INSTALL"
        assert saved.state["files"] == ["/w/poky/build/conf/local.conf"]
        (cand,) = store.list_memories(db, store.ALL, kind="candidate")
        assert cand.tags == ("precompact",)


def test_precompact_keeps_model_goal(repo: Path) -> None:
    from survivemem import state

    pid = project_of(repo)
    with open_db() as db:
        store.save_state(
            db, "s9", pid, state.checkpoint({}, goal="bring up the UART", decisions=["use ttyS0"])
        )
    run_hook("precompact", {"session_id": "s9", "transcript_path": str(FIXTURE), "cwd": str(repo)})
    with open_db() as db:
        saved = store.get_state(db, "s9")
    assert saved and saved.state["goal"] == "bring up the UART"
    assert saved.state["decisions"] == ["use ttyS0"] and saved.state["source"] == "model"


def test_session_start_reinjects_state_and_memories(repo: Path) -> None:
    from survivemem import state

    pid = project_of(repo)
    with open_db() as db:
        store.add_memory(db, "UART console is ttyS0 at 115200", "environment", project=pid)
        store.add_memory(db, "unrelated older note", "fact", project=pid)
        store.save_state(
            db,
            "s9",
            pid,
            state.checkpoint({}, goal="UART console bring-up", next_=["check ttyS0 baud"]),
        )
    out = run_hook("session-start", {"session_id": "s9", "source": "compact", "cwd": str(repo)})
    clone, rest = out.split("\n", 1)
    assert clone.startswith("linux-de10 · this clone ")
    assert "<survivemem-state>\nGOAL: UART console bring-up" in rest
    memories = rest.split("<survivemem>\n", 1)[1]
    assert memories.startswith("- [environment] UART console is ttyS0")
    assert "unrelated older note" in memories


def test_session_start_outside_any_project_with_nothing_stored(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert (
        run_hook("session-start", {"session_id": "x", "source": "resume", "cwd": str(plain)}) == ""
    )


def test_prompt_touches_session_and_old_states_are_purged(repo: Path) -> None:
    pid = project_of(repo)
    with open_db() as db:
        store.save_state(db, "ancient", pid, {"goal": "old"})
        db.execute("UPDATE session_state SET updated_at = 0 WHERE session_id = 'ancient'")
    run_hook("prompt", {"prompt": "hi", "cwd": str(repo), "session_id": "fresh"})
    with open_db() as db:
        latest = store.latest_state(db, pid)
        assert latest and latest.session_id == "fresh"
    run_hook("session-start", {"session_id": "fresh", "source": "resume", "cwd": str(repo)})
    with open_db() as db:
        assert store.get_state(db, "ancient") is None


def test_session_start_on_readonly_db_still_prints(repo: Path) -> None:
    from survivemem import state

    pid = project_of(repo)
    with open_db() as db:
        store.save_state(db, "s9", pid, state.checkpoint({}, goal="keep working"))
    db_file = paths.db_path()
    db_file.chmod(0o444)
    db_file.parent.chmod(0o555)
    try:
        out = run_hook("session-start", {"session_id": "s9", "source": "compact", "cwd": str(repo)})
    finally:
        db_file.parent.chmod(0o755)
    assert "GOAL: keep working" in out


def test_unwritable_log_dir_does_not_break_the_hook(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/proc/nonexistent")
    assert run_hook("prompt", {"prompt": "x", "cwd": str(repo)}) == ""
