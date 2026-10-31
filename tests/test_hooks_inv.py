# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from survivemem import db as dbmod
from survivemem import hooks, identity, investigate, paths, store
from survivemem.config import Config
from tests.helpers import make_repo


def run_hook(name: str, payload: dict[str, Any]) -> str:
    out = io.StringIO()
    assert hooks.run(name, io.StringIO(json.dumps(payload)), out) == 0
    return out.getvalue()


@contextmanager
def open_db() -> Iterator[sqlite3.Connection]:
    with closing(dbmod.connect(paths.db_path())) as db:
        yield db


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "linux", {"origin": "https://example.com/linux.git"})


def open_inv(repo: Path) -> str:
    with open_db() as db:
        res = identity.resolve(db, repo, Config(), env={})
        return investigate.open_new(db, res, "UART hang", "hangs after fpga load").id


def bash(
    repo: Path, command: str, *, failed: bool, output: str = "Error: boom", session: str = "s1"
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": session,
        "cwd": str(repo),
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "toolu_1",
    }
    if failed:
        payload |= {"hook_event_name": "PostToolUseFailure", "error": f"Exit code 2\n{output}"}
    else:
        payload |= {
            "hook_event_name": "PostToolUse",
            "tool_response": {"stdout": output, "stderr": "", "interrupted": False},
        }
    return payload


def pretool(repo: Path, command: str) -> str:
    return run_hook(
        "pretool", {"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": command}}
    )


def test_pretool_silent_without_investigation(repo: Path) -> None:
    assert pretool(repo, "dmesg") == ""


def test_loop_guard_modes(repo: Path) -> None:
    inv_id = open_inv(repo)
    run_hook("posttool", bash(repo, "sudo dmesg | tail -20", failed=True))
    with open_db() as db:
        (exp,) = [e for e in store.entries_of(db, inv_id) if e.kind == "experiment"]
    assert exp.command == "dmesg" and exp.result == "bad" and exp.evidence_id
    out = json.loads(pretool(repo, "cd /tmp && dmesg 2>&1"))["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert out["permissionDecisionReason"].startswith("LOOP GUARD: identical to experiment #1")
    assert "evidence bash:toolu_1" in out["permissionDecisionReason"]
    paths.config_path().write_text('[loop_guard]\nmode = "deny"\n')
    assert json.loads(pretool(repo, "dmesg"))["hookSpecificOutput"]["permissionDecision"] == (
        "deny"
    )
    paths.config_path().write_text('[loop_guard]\nmode = "warn"\n')
    warn = json.loads(pretool(repo, "dmesg"))["hookSpecificOutput"]
    assert "permissionDecision" not in warn and warn["additionalContext"].startswith("LOOP")
    assert pretool(repo, "dmesg -w") == ""


def test_hypothesis_answers_the_guard(repo: Path) -> None:
    inv_id = open_inv(repo)
    run_hook("posttool", bash(repo, "dmesg", failed=True))
    with open_db() as db:
        store.add_entry(db, inv_id, store.NewEntry("hypothesis", "bridges now enabled"))
    seen = json.loads(pretool(repo, "dmesg"))["hookSpecificOutput"]
    assert "permissionDecision" not in seen and seen["additionalContext"].startswith(
        "seen as #1 (bad"
    )


def test_three_identical_failures_raise_a_blocker(repo: Path) -> None:
    inv_id = open_inv(repo)
    run_hook("prompt", {"prompt": "start", "cwd": str(repo), "session_id": "s1"})
    for _ in range(3):
        run_hook("posttool", bash(repo, "make zImage", failed=True, output="error: x.c:1"))
    with open_db() as db:
        notes = [e.text for e in store.entries_of(db, inv_id) if e.kind == "note"]
        saved = store.get_state(db, "s1")
    assert notes == ["LOOP: make zImage failed identically ×3 — #1, #2, #3"]
    assert saved and saved.state["blockers"] == notes
    second = run_hook("prompt", {"prompt": "go on", "cwd": str(repo), "session_id": "s1"})
    assert second.startswith(f'investigation {inv_id} "UART hang" open — call ')
    assert "BLOCKER   LOOP: make zImage failed identically ×3" in second
    with open_db() as db:
        store.add_entry(db, inv_id, store.NewEntry("hypothesis", "stale .config"))
    third = run_hook("prompt", {"prompt": "again", "cwd": str(repo), "session_id": "s1"})
    assert "BLOCKER" not in third
    with open_db() as db:
        cleared = store.get_state(db, "s1")
    assert cleared and cleared.state["blockers"] == []


def test_successful_debug_command_is_an_observation(repo: Path) -> None:
    inv_id = open_inv(repo)
    log = (Path(__file__).parent / "fixtures" / "logs" / "dmesg.log").read_text()
    run_hook("posttool", bash(repo, "dmesg", failed=False, output=log))
    with open_db() as db:
        (entry,) = store.entries_of(db, inv_id)
        (ev,) = store.evidence_of(db, inv_id)
    assert entry.kind == "observation" and entry.result is None
    assert ev.tool == "dmesg" and "Oops" in ev.excerpt and ev.bytes == len(log.encode())


def test_failure_without_investigation_is_a_candidate(repo: Path) -> None:
    run_hook(
        "posttool",
        bash(
            repo,
            "bitbake core-image-minimal",
            failed=True,
            output="ERROR: x-1.0-r0 do_rootfs: failed",
        ),
    )
    run_hook("posttool", bash(repo, "ls -la", failed=True))
    run_hook("posttool", bash(repo, "bitbake core-image-minimal", failed=False))
    with open_db() as db:
        (cand,) = store.list_memories(db, store.ALL, kind="candidate")
    assert cand.source == "hook" and "posttool" in cand.tags and "do_rootfs" in cand.tags
    assert cand.content.startswith("$ bitbake core-image-minimal → exit 2")


def test_posttool_candidate_cap(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "POSTTOOL_CANDIDATE_CAP", 2)
    for i in range(4):
        run_hook("posttool", bash(repo, f"make target{i}", failed=True))
    with open_db() as db:
        assert len(store.list_memories(db, store.ALL, kind="candidate")) == 2
    assert paths.log_path().read_text().count("not writing more") == 1


def test_mcp_capture_stores_pointer_not_payload(repo: Path) -> None:
    result = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "results": [
                            {
                                "file": "drivers/fpga/socfpga.c",
                                "name": "socfpga_fpga_probe",
                                "source": "x" * 5000,
                            }
                        ]
                    }
                ),
            }
        ]
    }
    payload = {
        "cwd": str(repo),
        "session_id": "s1",
        "tool_name": "mcp__jcodemunch__order",
        "tool_input": {"action": "search_symbols", "args": {"query": "probe"}},
        "tool_response": result,
    }
    run_hook("posttool", payload)
    run_hook("posttool", payload | {"tool_name": "mcp__other__lookup"})
    with open_db() as db:
        (cand,) = store.list_memories(db, store.ALL, kind="candidate")
    assert cand.source == "tool" and cand.tags == ("jcodemunch", "mcp")
    assert cand.content.startswith("mcp__jcodemunch__order search_symbols → ")
    assert len(cand.content) <= 300
    assert json.loads(cand.refs or "[]") == [
        {
            "type": "symbol",
            "tool": "jcodemunch",
            "path": "drivers/fpga/socfpga.c",
            "symbol": "socfpga_fpga_probe",
        }
    ]


def test_prompt_full_block_on_first_and_every_20th(repo: Path) -> None:
    inv_id = open_inv(repo)
    with open_db() as db:
        pid = store.get_investigation(db, inv_id).project
        for i in range(40):
            store.add_memory(db, f"uart note {i} " + "y" * 380, "fact", project=pid)
    outputs = [
        run_hook("prompt", {"prompt": "uart note", "cwd": str(repo), "session_id": "s7"})
        for _ in range(21)
    ]
    full = [i + 1 for i, out in enumerate(outputs) if "<survivemem-investigation>" in out]
    assert full == [1, 21]
    assert all(len(out) <= hooks.PROMPT_BUDGET for out in outputs)
    assert "<survivemem>" in outputs[0] and outputs[1].startswith("investigation ")


def test_session_start_reinjects_full_investigation(repo: Path) -> None:
    inv_id = open_inv(repo)
    with open_db() as db:
        store.add_entry(db, inv_id, store.NewEntry("ruled_out", "not the clock"))
    out = run_hook("session-start", {"session_id": "s1", "source": "compact", "cwd": str(repo)})
    lines = out.splitlines()
    assert lines[0] == "<survivemem-investigation>" and lines[1].startswith("INVESTIGATION ")
    assert "  - not the clock  [#1, -]" in lines
    assert lines[-1] == investigate.CLOSING_LINE
