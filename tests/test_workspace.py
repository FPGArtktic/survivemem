# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from survivemem import workspace
from tests.helpers import make_repo
from tests.test_cli import Runner


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Runner:
    return Runner(capsys)


def write_bsp(tmp_path: Path) -> Path:
    bsp = make_repo(tmp_path / "bsp", {"origin": "https://example.com/bsp.git"})
    make_repo(tmp_path / "layers/meta-foo", {"origin": "https://example.com/meta-foo.git"})
    make_repo(tmp_path / "layers/meta-bar", {"origin": "https://example.com/meta-bar.git"})
    (bsp / "build/conf").mkdir(parents=True)
    (bsp / "build/conf/bblayers.conf").write_text(
        'BBPATH = "${TOPDIR}"\n'
        'BBLAYERS ?= " \\\n'
        f"  {tmp_path}/layers/meta-foo \\\n"
        "  ${TOPDIR}/../../layers/meta-bar \\\n"
        "  ${OEROOT}/meta \\\n"
        "  /nonexistent/meta-gone \\\n"
        '  "\n'
    )
    return bsp


def test_bblayers(tmp_path: Path) -> None:
    bsp = write_bsp(tmp_path)
    layers = workspace.bblayers(bsp / "build/conf/bblayers.conf")
    assert tmp_path / "layers/meta-foo" in layers and tmp_path / "layers/meta-bar" in layers
    assert Path("/nonexistent/meta-gone") in layers and len(layers) == 3


def test_kas_and_manifest(tmp_path: Path) -> None:
    (tmp_path / "kas.yml").write_text(
        "header:\n  version: 14\nrepos:\n  meta-foo:\n    url: https://x/meta-foo\n"
        "  poky:\n    url: https://x/poky\n    path: sources/poky  # pinned\n"
        "  local:\nmachine: cyclone5\n"
    )
    assert workspace.kas_repos(tmp_path / "kas.yml") == [
        tmp_path / "meta-foo",
        tmp_path / "sources/poky",
        tmp_path / "local",
    ]
    (tmp_path / ".repo").mkdir()
    (tmp_path / ".repo/manifest.xml").write_text(
        '<manifest><project name="u-boot" path="src/u-boot"/><project name="linux"/></manifest>'
    )
    assert workspace.manifest_projects(tmp_path) == [tmp_path / "src/u-boot", tmp_path / "linux"]


def test_autodetect_link_unlink(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bsp = write_bsp(tmp_path)
    monkeypatch.chdir(bsp)
    run.json("add", "bsp fact")
    proposed = run.json("projects", "autodetect")
    assert {r["label"] for r in proposed} == {"meta-foo (new)", "meta-bar (new)"}
    assert not any(r["linked"] for r in proposed)
    applied = run.json("projects", "autodetect", "--yes")
    assert {r["label"] for r in applied} == {"meta-foo", "meta-bar"}
    monkeypatch.chdir(tmp_path / "layers/meta-foo")
    run.json("add", "layer fact")
    assert {m["content"] for m in run.json("search", "fact")} == {"bsp fact", "layer fact"}
    assert run.json("projects", "unlink", "bsp", "meta-foo")["linked"] is False
    assert {m["content"] for m in run.json("search", "fact")} == {"layer fact"}
    assert run.json("projects", "link", "bsp", "meta-foo")["linked"] is True


def test_env_and_doctor_stale(run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.helpers import git

    repo = make_repo(tmp_path / "linux", {"origin": "https://example.com/linux.git"})
    monkeypatch.chdir(repo)
    run.json("add", "MACHINE=cyclone5", "--kind", "environment")
    head = git(repo, "rev-parse", "HEAD")
    run.json("add", "fresh fact", "--anchor", head)
    run.json("add", "old fact", "--anchor", "0123456789abcdef")
    code, out, _ = run("env", "--format", "table")
    assert code == 0 and "MACHINE" in out and "cyclone5" in out
    stale = [c for c in run.json("doctor", "--stale")["checks"] if c["name"] == "stale"]
    assert len(stale) == 1 and "old fact" in stale[0]["detail"]
