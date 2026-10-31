# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional Ollama client (bge-m3 embeddings, compaction summaries) and sqlite-vec loading.

Everything here degrades: no sqlite-vec, no Ollama, a slow Ollama or a missing model all mean
None, and search falls back to FTS5 only. Only a loopback Ollama is ever contacted.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from survivemem import store
from survivemem.config import Config

log = logging.getLogger(__name__)

QUERY_TIMEOUT = 0.5
BATCH_TIMEOUT = 30.0
SUMMARY_TIMEOUT = 25.0
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class VecStatus:
    extension: bool
    table: bool
    embedded: int


def is_loopback(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK


def load_extension(db: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec

        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except (ImportError, AttributeError, sqlite3.OperationalError) as e:
        log.debug("sqlite-vec unavailable: %s", e)
        return False
    return True


def _post(
    cfg: Config,
    path: str,
    body: dict[str, object],
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> dict[str, object] | None:
    if not is_loopback(cfg.ollama_url):
        log.warning("refusing non-loopback Ollama URL %s", cfg.ollama_url)
        return None
    try:
        with httpx.Client(
            base_url=cfg.ollama_url, timeout=timeout, transport=transport, trust_env=False
        ) as c:  # HTTP_PROXY must not see memory text
            response = c.post(path, json=body)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as e:
        log.debug("ollama %s failed: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def embed(
    texts: Sequence[str],
    cfg: Config,
    timeout: float = BATCH_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> list[list[float]] | None:
    data = _post(
        cfg, "/api/embed", {"model": cfg.embed_model, "input": list(texts)}, timeout, transport
    )
    vectors = data.get("embeddings") if data else None
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        return None
    if any(not isinstance(v, list) or len(v) != store.VEC_DIM for v in vectors):
        log.warning("model %s does not return %d-dim embeddings", cfg.embed_model, store.VEC_DIM)
        return None
    return [[float(x) for x in v] for v in vectors]


def summarize(text: str, cfg: Config, transport: httpx.BaseTransport | None = None) -> str | None:
    prompt = (
        "Summarise this coding session in at most 10 short lines: goal, what was done, "
        "decisions, open items. No preamble.\n\n" + text
    )
    body = {"model": cfg.compaction_model, "prompt": prompt, "stream": False}
    data = _post(cfg, "/api/generate", body, SUMMARY_TIMEOUT, transport)
    answer = data.get("response") if data else None
    return answer.strip() if isinstance(answer, str) and answer.strip() else None


def query_vector(
    db: sqlite3.Connection, query: str, cfg: Config, transport: httpx.BaseTransport | None = None
) -> list[float] | None:
    """§7 step 3: a vector only when memory_vec exists, sqlite-vec loads and Ollama answers
    within 500 ms."""
    if not query.strip() or not store.has_vec_table(db) or not load_extension(db):
        return None
    vectors = embed([query], cfg, QUERY_TIMEOUT, transport)
    return vectors[0] if vectors else None


def backfill(
    db: sqlite3.Connection,
    cfg: Config,
    limit: int,
    timeout: float = BATCH_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Embed up to `limit` memories whose vector is missing or older than the memory."""
    if not store.has_vec_table(db) or not load_extension(db):
        return 0
    stale = store.stale_vectors(db, limit)
    if not stale:
        return 0
    vectors = embed([content for _, content, _ in stale], cfg, timeout, transport)
    if vectors is None:
        return 0
    store.save_vectors(db, [(mid, updated) for mid, _, updated in stale], vectors)
    return len(stale)


def status(db: sqlite3.Connection) -> VecStatus:
    loaded = load_extension(db)
    table = store.has_vec_table(db)
    return VecStatus(loaded, table, store.vector_count(db) if loaded and table else 0)


def ollama_reachable(cfg: Config, transport: httpx.BaseTransport | None = None) -> bool:
    if not is_loopback(cfg.ollama_url):
        return False
    try:
        with httpx.Client(
            base_url=cfg.ollama_url, timeout=QUERY_TIMEOUT, transport=transport, trust_env=False
        ) as c:
            return c.get("/api/version").status_code == 200
    except httpx.HTTPError:
        return False
