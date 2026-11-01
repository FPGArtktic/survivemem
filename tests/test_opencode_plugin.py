# SPDX-License-Identifier: GPL-3.0-or-later
"""integrations/opencode/survivemem.js driven under Node with the payloads opencode sends."""

import json
import os
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from survivemem import db as dbmod
from survivemem import identity, investigate, paths, store
from survivemem.config import Config
from tests.helpers import make_repo

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def survivemem_shim(tmp_path: Path) -> Path:
    exe = tmp_path / "survivemem"
    exe.write_text(f'#!/bin/sh\nexec {sys.executable} -m survivemem "$@"\n')
    exe.chmod(0o755)
    return exe


def test_plugin_maps_opencode_hooks(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "board", {"origin": "https://example.com/board.git"})
    with closing(dbmod.connect(paths.db_path())) as conn:
        res = identity.resolve(conn, repo, Config(), env={})
        investigate.open_new(conn, res, "make fails", "target missing")
        store.add_memory(conn, "the build runs on host hydra-7", "fact", project=res.project_id)
    env = dict(os.environ, SURVIVEMEM_BIN=str(survivemem_shim(tmp_path)))
    plugin = ROOT / "integrations" / "opencode" / "survivemem.js"
    proc = subprocess.run(
        [str(NODE), str(ROOT / "tests" / "opencode_harness.mjs"), plugin.as_uri(), str(repo)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=120,
    )
    result = json.loads(proc.stdout)
    assert result["blocked"].startswith("LOOP GUARD: identical to experiment #")
    injected = result["injected"]
    assert len(injected) == 1 and injected[0]["synthetic"] is True
    assert "hydra-7" in injected[0]["text"] and injected[0]["id"] == "prt_1sm"
    assert result["compaction"][0].startswith("<survivemem-investigation>")
    with closing(dbmod.connect(paths.db_path())) as conn:
        inv = store.open_investigation_of(conn, res.project_id)
        assert inv is not None
        notes = [e.text for e in store.entries_of(conn, inv.id) if e.kind == "note"]
    assert notes and notes[0].startswith("LOOP: make nonexistent-target failed identically ×3")
