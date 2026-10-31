# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import io
import json
import math
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from survivemem import cli, embed, hooks, paths, store
from survivemem import db as dbmod
from survivemem.config import Config
from tests.helpers import make_repo

pytest.importorskip("sqlite_vec")

LOCAL = Config(ollama_url="http://127.0.0.1:11434")


def fake_vector(text: str) -> list[float]:
    """Bag of words hashed into 1024 dimensions, normalised: shared words → close vectors."""
    v = [0.0] * store.VEC_DIM
    for word in text.lower().split():
        v[int(hashlib.sha1(word.encode()).hexdigest(), 16) % store.VEC_DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def fake_embed(
    texts: Sequence[str], cfg: Config, timeout: float = 0.0, transport: Any = None
) -> list[list[float]]:
    return [fake_vector(t) for t in texts]


def ollama(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_loopback_only() -> None:
    assert embed.is_loopback("http://127.0.0.1:11434")
    assert embed.is_loopback("http://localhost:11434")
    assert not embed.is_loopback("http://host.docker.internal:11434")
    assert not embed.is_loopback("https://127.0.0.1:11434")
    called: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, json={})

    transport = ollama(handler)
    assert (
        embed.embed(["x"], Config(ollama_url="http://10.0.0.2:11434"), transport=transport) is None
    )
    assert called == []


def test_embed_parses_and_validates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/api/embed" and body["model"] == "bge-m3"
        assert body["keep_alive"] == "30m"
        return httpx.Response(200, json={"embeddings": [[0.5] * 1024 for _ in body["input"]]})

    assert len(embed.embed(["a", "b"], LOCAL, transport=ollama(handler)) or []) == 2
    short = ollama(lambda r: httpx.Response(200, json={"embeddings": [[0.1, 0.2]]}))
    assert embed.embed(["a"], LOCAL, transport=short) is None
    missing = ollama(lambda r: httpx.Response(404, json={"error": "model not found"}))
    assert embed.embed(["a"], LOCAL, transport=missing) is None


def test_unreachable_ollama_degrades() -> None:
    assert embed.embed(["a"], Config(ollama_url="http://127.0.0.1:9"), timeout=0.5) is None
    assert not embed.ollama_reachable(Config(ollama_url="http://127.0.0.1:9"))


def test_summarize() -> None:
    reply = ollama(lambda r: httpx.Response(200, json={"response": " 1. goal\n2. done "}))
    assert embed.summarize("text", LOCAL, transport=reply) == "1. goal\n2. done"


def vec_db(db: sqlite3.Connection) -> sqlite3.Connection:
    assert embed.load_extension(db)
    store.create_vec_table(db)
    return db


def test_query_vector_needs_table(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed, "embed", fake_embed)
    assert embed.query_vector(db, "uart", LOCAL) is None
    vec_db(db)
    assert embed.query_vector(db, "uart", LOCAL) == fake_vector("uart")


def test_search_fuses_semantic_hits(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embed, "embed", fake_embed)
    vec_db(db)
    a = store.add_memory(db, "serial console baud rate 115200", "environment")
    store.add_memory(db, "quartus fitter settings", "fact")
    assert embed.backfill(db, LOCAL, 10) == 2
    # no lexical overlap with "console baud": found by the vector ranking only
    hits = store.search(db, "console baud", store.ALL, vector=fake_vector("serial console baud"))
    assert hits[0].memory.id == a.id and "vec" in hits[0].matched_by


def test_backfill_reembeds_after_edit(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embed, "embed", fake_embed)
    vec_db(db)
    m = store.add_memory(db, "old text", "fact")
    store.add_memory(db, "a candidate", "candidate")
    assert embed.backfill(db, LOCAL, 10) == 1
    assert embed.backfill(db, LOCAL, 10) == 0
    db.execute("UPDATE memories SET updated_at = updated_at + 1000 WHERE id = ?", (m.id,))
    assert embed.backfill(db, LOCAL, 10) == 1
    assert store.vector_count(db) == 1


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> Any:
    assert cli.run([*args, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_reindex_without_ollama_reports_reason(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_cli(capsys, "reindex")
    assert result["vec"] == "disabled" and "Ollama" in result["reason"]
    with closing(dbmod.connect()) as db:
        assert not store.has_vec_table(db)


def test_reindex_and_search_with_vectors(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(embed, "embed", fake_embed)
    run_cli(capsys, "add", "serial console baud rate 115200")
    run_cli(capsys, "add", "quartus fitter settings")
    assert run_cli(capsys, "reindex") == {"fts": "rebuilt", "vec": "rebuilt", "embedded": 2}
    hits = run_cli(capsys, "search", "serial console")
    assert hits[0]["matched_by"] == ["fts", "vec"] and "vec_distance" in hits[0]


def test_stop_hook_embeds_backlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed, "embed", fake_embed)
    repo = make_repo(tmp_path / "r")
    with closing(dbmod.connect(paths.db_path())) as db:
        vec_db(db)
        store.add_memory(db, "a fact to embed", "fact")
    payload = json.dumps({"cwd": str(repo), "last_assistant_message": "done"})
    hooks.run("stop", io.StringIO(payload), io.StringIO())
    with closing(dbmod.connect(paths.db_path())) as db:
        assert embed.load_extension(db) and store.vector_count(db) == 1


def test_precompact_summary_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.config_path().write_text("[compaction]\nsummarize = true\n")
    monkeypatch.setattr(embed, "summarize", lambda text, cfg, transport=None: "1. goal")
    repo = make_repo(tmp_path / "r")
    fixture = Path(__file__).parent / "fixtures" / "transcript_basic.jsonl"
    payload = {"session_id": "s1", "transcript_path": str(fixture), "cwd": str(repo)}
    hooks.run("precompact", io.StringIO(json.dumps(payload)), io.StringIO())
    with closing(dbmod.connect(paths.db_path())) as db:
        saved = store.get_state(db, "s1")
    assert saved and saved.state["summary"] == "1. goal"


def test_status_reports_vec_and_ollama(db: sqlite3.Connection) -> None:
    from survivemem import payload

    status = payload.status_payload(db, Config(ollama_url="http://127.0.0.1:9"))
    assert status["vec"]["extension"] and not status["vec"]["table"]
    assert status["ollama"]["reachable"] is False


def test_ollama_requests_ignore_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    seen: list[httpx.Client] = []
    real = httpx.Client

    def spy(*args: Any, **kwargs: Any) -> httpx.Client:
        client = real(*args, **kwargs)
        seen.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", spy)
    embed.embed(["x"], LOCAL, timeout=0.2)
    embed.ollama_reachable(LOCAL)
    assert seen and all(not c.trust_env for c in seen)
