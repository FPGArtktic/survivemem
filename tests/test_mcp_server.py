# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from survivemem import mcp_server
from tests.helpers import make_repo

EXPECTED_TOOLS = {
    "list_projects",
    "search_memories",
    "store_memory",
    "forget_memory",
    "list_recent",
    "memory_status",
    "checkpoint",
    "session_state",
}


def call(name: str, args: dict[str, Any] | None = None) -> Any:
    async def go() -> Any:
        async with Client(mcp_server.build_server()) as client:
            result = await client.call_tool(name, args or {})
            if result.is_error:
                return {"error": result.content[0].text}  # type: ignore[union-attr]
            return result.structured_content

    return anyio.run(go)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = make_repo(tmp_path / "bsp", {"origin": "https://example.com/bsp.git"})
    monkeypatch.chdir(path)
    return path


def test_tool_list_and_descriptions() -> None:
    async def go() -> Any:
        async with Client(mcp_server.build_server()) as client:
            return (await client.list_tools()).tools

    tools = anyio.run(go)
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description and tool.description.split(".")[0] in ("READ", "WRITE")


def test_store_search_forget_roundtrip(repo: Path) -> None:
    stored = call(
        "store_memory",
        {"content": "Kernel pinned to 6.6 LTS.", "kind": "decision", "tags": ["kernel"]},
    )
    assert stored["source"] == "mcp" and stored["project_label"] == "bsp"
    hits = call("search_memories", {"query": "kernel LTS"})["hits"]
    assert [h["id"] for h in hits] == [stored["id"]]
    assert call("list_recent")["memories"][0]["id"] == stored["id"]
    assert call("forget_memory", {"id": stored["id"][:12]})["deleted_at"] is not None
    assert call("search_memories", {"query": "kernel"})["hits"] == []


def test_store_scopes_and_global(repo: Path) -> None:
    b = call("store_memory", {"content": "backport only here", "kind": "fact", "scope": "branch"})
    g = call(
        "store_memory",
        {"content": "prefers terse answers", "kind": "preference", "global_memory": True},
    )
    assert b["scope"] == "branch:main"
    assert g["project"] is None and g["project_label"] == "global"


def test_errors_are_reported_with_message(repo: Path) -> None:
    assert "not found" in call("forget_memory", {"id": "ZZZZZZ"})["error"]
    assert "kind" in call("store_memory", {"content": "x", "kind": "bogus"})["error"]
    assert "candidate" in call("store_memory", {"content": "x", "kind": "candidate"})["error"]
    assert (
        "secret"
        in call("store_memory", {"content": "api_key=abcdefgh12345", "kind": "fact"})["error"]
    )


def test_list_projects_marks_current(repo: Path) -> None:
    call("store_memory", {"content": "a fact", "kind": "fact"})
    data = call("list_projects")
    project = data["projects"][0]
    assert project["current"] and project["id"] == data["current"]
    assert project["memories"] == 1
    assert project["checkouts"][0]["current"] and project["checkouts"][0]["branch"] == "main"


def test_memory_status(repo: Path) -> None:
    call("store_memory", {"content": "a fact", "kind": "fact"})
    status = call("memory_status")
    assert status["counts"] == {"fact": 1} and status["fts_ok"]
    assert status["current_project"] == "bsp"


def test_stdio_transport_end_to_end(repo: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "survivemem", "serve"],
        env=dict(os.environ),
        cwd=str(repo),
    )

    async def go() -> Any:
        async with Client(params) as client:
            await client.call_tool("store_memory", {"content": "over stdio", "kind": "fact"})
            result = await client.call_tool("search_memories", {"query": "stdio"})
            return json.loads(result.content[0].text)  # type: ignore[union-attr]

    assert anyio.run(go)["hits"][0]["content"] == "over stdio"


def test_checkpoint_goes_to_the_latest_session_of_the_project(repo: Path) -> None:
    from contextlib import closing

    from survivemem import db as dbmod
    from survivemem import store

    call("store_memory", {"content": "creates the project", "kind": "fact"})
    with closing(dbmod.connect()) as db:
        project = store.get_project(db, "bsp").id
        store.touch_session(db, "sess-0", project)
        time.sleep(0.005)  # updated_at has millisecond resolution
        store.touch_session(db, "sess-1", project)
    saved = call(
        "checkpoint",
        {
            "goal": "bring up uart",
            "done": ["dts"],
            "in_progress": "probe",
            "next": ["bridge"],
            "decisions": ["6.6 LTS"],
            "files": ["a.dts"],
        },
    )
    assert saved["session_id"] == "sess-1" and saved["state"]["source"] == "model"
    assert "GOAL: bring up uart" in saved["rendered"]
    got = call("session_state")
    assert got["state"]["next"] == ["bridge"] and got["session_id"] == "sess-1"


def test_session_state_empty_without_checkpoint(repo: Path) -> None:
    assert call("session_state") == {}
    saved = call(
        "checkpoint",
        {"goal": "g", "done": [], "in_progress": "", "next": [], "decisions": [], "files": []},
    )
    assert saved["session_id"].startswith("mcp-")
