# SPDX-License-Identifier: GPL-3.0-or-later
import json
import sqlite3
from pathlib import Path

from survivemem import state, store, transcript

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_basic.jsonl"


def test_transcript_extraction() -> None:
    calls = transcript.tool_uses(transcript.tail(FIXTURE))
    assert [n for n, _ in calls] == ["Bash", "Read", "Edit", "TodoWrite"]
    assert transcript.files_touched(calls) == ["/w/poky/build/conf/local.conf"]
    assert transcript.bash_commands(calls) == ["bitbake core-image-minimal 2>&1 | tail -50"]
    assert transcript.last_todos(calls) == [
        {"content": "fix IMAGE_INSTALL", "status": "in_progress"},
        {"content": "rebuild image", "status": "pending"},
    ]


def test_from_transcript() -> None:
    found = state.from_transcript(transcript.tail(FIXTURE))
    assert found["goal"] == "Why does do_rootfs fail on the de10-nano image?"
    assert found["in_progress"] == "fix IMAGE_INSTALL" and found["next"] == ["rebuild image"]
    assert found["done"] == []
    assert found["last_answer"].startswith("The package nonexistent-pkg")
    assert found["files"] == ["/w/poky/build/conf/local.conf"]


def test_model_checkpoint_wins_for_goal_and_decisions() -> None:
    model = state.checkpoint(
        {},
        goal="ship 6.6 bring-up",
        done=["dts"],
        in_progress="uart",
        next_=["fpga bridge"],
        decisions=["6.6 LTS: vendor BSP"],
        files=["a.dts"],
    )
    found = state.from_transcript(transcript.tail(FIXTURE))
    merged = state.merge_heuristic(model, found)
    assert merged["goal"] == "ship 6.6 bring-up"
    assert merged["decisions"] == ["6.6 LTS: vendor BSP"]
    assert merged["files"] == ["/w/poky/build/conf/local.conf", "a.dts"]
    assert merged["in_progress"] == "fix IMAGE_INSTALL"
    assert merged["source"] == "model"
    assert state.merge_heuristic({}, found)["source"] == "hook"


def test_checkpoint_overwrites_lists_and_keeps_unstated_fields() -> None:
    first = state.checkpoint({}, goal="g", done=["a"], next_=["b", "c"], files=["x"])
    second = state.checkpoint(first, done=["a", "b"], next_=["c"], files=["y"])
    assert second["goal"] == "g" and second["done"] == ["a", "b"] and second["next"] == ["c"]
    assert second["files"] == ["y", "x"]


def test_render_is_terse_and_bounded() -> None:
    st = state.checkpoint(
        {},
        goal="g" * 500,
        done=[f"step {i} " + "x" * 150 for i in range(40)],
        in_progress="now",
        next_=["later"],
        decisions=["d"],
    )
    out = state.render(st)
    assert out.startswith("<survivemem-state>\nGOAL: ggg") and out.endswith("</survivemem-state>")
    assert len(out) <= state.BUDGET
    assert "IN PROGRESS: now" in out and "NEXT:\n- later" in out
    assert not out.splitlines()[-2].endswith(":")
    assert state.render({}) == "" and state.is_empty({"files": []})


def test_render_tolerates_garbage_types() -> None:
    assert state.render({"goal": 3, "next": "single", "done": [None, "ok", {"x": 1}]}) == (
        "<survivemem-state>\nNEXT: single\nDONE:\n- ok\n</survivemem-state>"
    )


def test_store_roundtrip_touch_latest_purge(db: sqlite3.Connection) -> None:
    store.touch_session(db, "s1", "p1")
    store.save_state(db, "s2", "p1", {"goal": "x"})
    store.touch_session(db, "s1", None)
    latest = store.latest_state(db, "p1")
    assert latest and latest.session_id == "s1" and latest.state == {}
    got = store.get_state(db, "s2")
    assert got and got.state == {"goal": "x"} and got.project == "p1"
    assert store.latest_state(db, None) is None
    assert store.purge_states(db, older_than_ms=10**15) == 2
    assert store.list_states(db) == []


def test_project_merge_moves_session_state(db: sqlite3.Connection) -> None:
    src, dst = store.create_project(db, "src"), store.create_project(db, "dst")
    store.save_state(db, "s1", src.id, {"goal": "g"})
    store.merge_projects(db, src.id, dst.id)
    moved = store.get_state(db, "s1")
    assert moved is not None and moved.project == dst.id


def test_transcript_state_redacts_secrets() -> None:
    token = "ghp_" + "a" * 36
    entries = [
        {"type": "user", "message": {"content": f"use token {token} please"}},
        {
            "type": "assistant",
            "message": {
                "id": "m",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": f"curl -H 'Authorization: {token}' x"},
                    },
                    {"type": "text", "text": f"done with {token}"},
                ],
            },
        },
    ]
    found = state.from_transcript(entries)
    assert token not in json.dumps(found) and "[REDACTED]" in found["commands"][0]


def test_parallel_counter_updates_are_not_lost(tmp_path: Path) -> None:
    import threading

    from survivemem import db as dbmod

    path = tmp_path / "par.db"
    dbmod.connect(path).close()

    def worker() -> None:
        conn = dbmod.connect(path)
        for _ in range(25):
            store.bump_state(conn, "s", None, "_posttool_candidates")
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = dbmod.connect(path)
    saved = store.get_state(conn, "s")
    assert saved and saved.state["_posttool_candidates"] == 100
    store.set_state_key(conn, "s", None, "blockers", ["LOOP: x"])
    again = store.get_state(conn, "s")
    assert (
        again
        and again.state["blockers"] == ["LOOP: x"]
        and again.state["_posttool_candidates"] == 100
    )
