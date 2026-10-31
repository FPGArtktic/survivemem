# SPDX-License-Identifier: GPL-3.0-or-later
import sqlite3
from pathlib import Path

import pytest

from survivemem import identity, store
from survivemem.config import Config
from tests.helpers import clone, git, make_repo

ALIASES = {"gl": ("gitlab.lan", 12345), "github.com": ("github.com", 22)}


def fake_ssh(host: str) -> tuple[str, int | None]:
    return ALIASES.get(host, (host, 22))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:FPGArtktic/foo.git", "github.com/FPGArtktic/foo"),
        ("https://user@github.com/FPGArtktic/foo.git/", "github.com/FPGArtktic/foo"),
        ("ssh://git@github.com:22/FPGArtktic/foo", "github.com/FPGArtktic/foo"),
        ("https://GitHub.COM/FPGArtktic/Foo", "github.com/FPGArtktic/Foo"),
        ("http://user:pw@example.org:80/a/b.git", "example.org/a/b"),
        ("git://git.kernel.org/pub/scm/linux.git", "git.kernel.org/pub/scm/linux"),
        ("ssh://git@gitlab.lan:12345/grp/sub/repo.git", "gitlab.lan:12345/grp/sub/repo"),
        ("git@gitlab.lan:grp/sub/repo.git", "gitlab.lan/grp/sub/repo"),
        ("ssh://git@gitlab.lan/grp/sub/repo.git", "gitlab.lan/grp/sub/repo"),
        ("git@gl:grp/repo.git", "gitlab.lan:12345/grp/repo"),
        ("ssh://gl:2222/grp/repo.git", "gitlab.lan:2222/grp/repo"),
        ("https://192.168.1.10:8443/grp/repo.git", "192.168.1.10:8443/grp/repo"),
        ("file:///srv/git/foo.git", "file:/srv/git/foo"),
        ("/srv/git/foo.git/", "file:/srv/git/foo"),
        ("../up/foo.git", "file:/base/up/foo"),
    ],
)
def test_normalise_remote_url(url: str, expected: str) -> None:
    got = identity.normalise_remote_url(url, base=Path("/base/repo"), resolve=fake_ssh)
    assert got == expected


@pytest.mark.parametrize(
    ("url", "hosts", "expected"),
    [
        (
            "https://192.168.1.10/grp/repo.git",
            {"192.168.1.10": "gitlab.lan"},
            "gitlab.lan/grp/repo",
        ),
        (
            "ssh://git@gitlab.lan:12345/grp/repo.git",
            {"gitlab.lan:12345": "gitlab.lan"},
            "gitlab.lan/grp/repo",
        ),
        (
            "ssh://git@10.0.0.5:12345/grp/repo.git",
            {"10.0.0.5": "gitlab.lan"},
            "gitlab.lan:12345/grp/repo",
        ),
        ("git@gl:grp/repo.git", {"gitlab.lan:12345": "gitlab.lan"}, "gitlab.lan/grp/repo"),
    ],
)
def test_normalise_with_hosts_map(url: str, hosts: dict[str, str], expected: str) -> None:
    assert identity.normalise_remote_url(url, hosts=hosts, resolve=fake_ssh) == expected


def test_scp_and_ssh_forms_converge() -> None:
    a = identity.normalise_remote_url("git@gitlab.lan:grp/repo.git", resolve=None)
    b = identity.normalise_remote_url("ssh://git@gitlab.lan:22/grp/repo", resolve=None)
    assert a == b == "gitlab.lan/grp/repo"


def test_ssh_resolve_rejects_option_like_host() -> None:
    assert identity.ssh_resolve("-oProxyCommand=x") == ("-oProxyCommand=x", None)


def resolve(db: sqlite3.Connection, cwd: Path, cfg: Config | None = None) -> identity.Resolution:
    return identity.resolve(db, cwd, cfg or Config(), env={})


def test_two_clones_of_one_repo_are_one_project(db: sqlite3.Connection, tmp_path: Path) -> None:
    url = "git@github.com:FPGArtktic/linux.git"
    a = make_repo(tmp_path / "w/linux-de10", {"origin": url})
    b = make_repo(tmp_path / "w/elsewhere/linux-up", {"origin": url})
    ra, rb = resolve(db, a), resolve(db, b)
    assert ra.step == "created" and rb.step == "remote"
    assert ra.project_id == rb.project_id
    assert ra.checkout and rb.checkout and ra.checkout.id != rb.checkout.id
    assert len(store.list_checkouts(db, ra.project_id)) == 2


def test_two_repos_with_one_basename_do_not_collide(db: sqlite3.Connection, tmp_path: Path) -> None:
    a = make_repo(tmp_path / "a/tools", {"origin": "https://example.com/a/tools.git"})
    b = make_repo(tmp_path / "b/tools", {"origin": "https://example.com/b/tools.git"})
    assert resolve(db, a).project_id != resolve(db, b).project_id


def test_worktree_resolves_to_main_repo(db: sqlite3.Connection, tmp_path: Path) -> None:
    main = make_repo(tmp_path / "repo")
    git(main, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "wt"))
    r_main, r_wt = resolve(db, main), resolve(db, tmp_path / "wt")
    assert r_main.project_id == r_wt.project_id
    assert r_wt.step == "path"
    assert r_wt.checkout and r_wt.checkout.last_branch == "feature"


def test_detached_bisect_clone(db: sqlite3.Connection, tmp_path: Path) -> None:
    url = "https://example.com/k/linux.git"
    src = make_repo(tmp_path / "src", commits=3)
    work = clone(src, tmp_path / "linux", url)
    bisect = clone(src, tmp_path / "linux-bisect", url)
    git(bisect, "checkout", "-q", "--detach", "HEAD~1")
    r1, r2 = resolve(db, work), resolve(db, bisect)
    assert r1.project_id == r2.project_id
    assert r2.checkout and r2.checkout.last_branch == "detached"
    assert r2.checkout.last_head == git(bisect, "rev-parse", "HEAD")


def test_subdirectory_resolves_like_root(db: sqlite3.Connection, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r", {"origin": "https://example.com/r.git"})
    (repo / "sub/dir").mkdir(parents=True)
    assert resolve(db, repo / "sub/dir").project_id == resolve(db, repo).project_id


def test_fork_is_adopted_by_root_commit(db: sqlite3.Connection, tmp_path: Path) -> None:
    src = make_repo(tmp_path / "upstream", {"origin": "https://github.com/up/tool.git"})
    fork = clone(src, tmp_path / "fork", "https://github.com/me/tool.git")
    first = resolve(db, src)
    adopted = resolve(db, fork)
    assert adopted.step == "root" and adopted.project_id == first.project_id
    assert adopted.hint is None
    owner = store.find_identity(db, "remote:github.com/me/tool")
    assert owner and owner.project_id == first.project_id


def test_fork_proposed_when_configured(db: sqlite3.Connection, tmp_path: Path) -> None:
    src = make_repo(tmp_path / "upstream", {"origin": "https://example.com/up/linux.git"})
    fork = clone(src, tmp_path / "fork", "https://example.com/vendor/linux.git")
    first = resolve(db, src)
    proposed = resolve(db, fork, Config(root_match="propose"))
    assert proposed.step == "created" and proposed.project_id != first.project_id
    assert proposed.hint and "projects alias add" in proposed.hint and first.project
    assert first.project.label in proposed.hint


def test_family_forks_are_proposed_by_default(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = make_repo(tmp_path / "u-boot", {"origin": "https://example.com/u-boot.git"})
    root = git(src, "rev-list", "--max-parents=0", "HEAD")
    monkeypatch.setattr(identity, "families", lambda: {root: "u-boot"})
    first = resolve(db, src)
    assert first.family == "u-boot"
    vendor = clone(src, tmp_path / "u-boot-vendor", "https://example.com/vendor/u-boot.git")
    proposed = resolve(db, vendor)
    assert proposed.step == "created" and proposed.project_id != first.project_id
    assert proposed.hint and first.project and first.project.id in proposed.hint
    other = clone(src, tmp_path / "u-boot-other", "https://example.com/other/u-boot.git")
    cfg = Config(family_root_match={"u-boot": "adopt"})
    assert resolve(db, other, cfg).project_id == first.project_id


def test_repo_without_remotes_falls_back_to_path(db: sqlite3.Connection, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r", {"origin": "https://example.com/r.git"})
    first = resolve(db, repo)
    git(repo, "remote", "remove", "origin")
    again = resolve(db, repo)
    assert again.project_id == first.project_id and again.step == "path"


def test_new_remote_is_attached_after_hit(db: sqlite3.Connection, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r", {"origin": "https://example.com/r.git"})
    first = resolve(db, repo)
    git(repo, "remote", "add", "mirror", "https://mirror.example.com/r.git")
    resolve(db, repo)
    owner = store.find_identity(db, "remote:mirror.example.com/r")
    assert owner and owner.project_id == first.project_id


def test_survivemem_toml_name_wins(db: sqlite3.Connection, tmp_path: Path) -> None:
    a = make_repo(tmp_path / "a", {"origin": "https://example.com/a.git"})
    b = make_repo(tmp_path / "b", {"origin": "https://example.com/b.git"})
    for repo in (a, b):
        (repo / ".survivemem.toml").write_text('project = "bsp"\n')
    ra, rb = resolve(db, a), resolve(db, b)
    assert ra.project and ra.project.label == "bsp"
    assert rb.step == "name" and rb.project_id == ra.project_id


def test_env_override(db: sqlite3.Connection, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    res = identity.resolve(db, repo, Config(), env={"SURVIVEMEM_PROJECT": "forced"})
    assert res.step == "env" and res.project and res.project.label == "forced"


def test_outside_git_is_global(db: sqlite3.Connection, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert resolve(db, plain).project is None


def test_quartus_project_without_git(db: sqlite3.Connection, tmp_path: Path) -> None:
    qdir = tmp_path / "fpga"
    (qdir / "output_files").mkdir(parents=True)
    (qdir / "top.qpf").write_text("")
    res = resolve(db, qdir)
    assert res.project and res.project.label == "top"
    assert resolve(db, qdir).project_id == res.project_id


def test_git_missing_from_path(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "r")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert resolve(db, repo).project is None


def test_insteadof_rewrite_is_applied(db: sqlite3.Connection, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r", {"origin": "gh:FPGArtktic/r.git"})
    git(repo, "config", "url.https://github.com/.insteadOf", "gh:")
    ids = identity.identities(identity.collect(repo, Config()) or pytest.fail(), Config())
    assert "remote:github.com/FPGArtktic/r" in ids


def test_kernel_config_hash_recorded(db: sqlite3.Connection, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "linux")
    (repo / ".config").write_text("CONFIG_DEBUG_INFO_BTF=y\n")
    res = resolve(db, repo)
    assert res.checkout and res.checkout.last_config
    assert len(res.checkout.last_config) == 64


def test_checkout_line(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    url = "https://example.com/linux.git"
    a = make_repo(tmp_path / "w/linux-de10", {"origin": url})
    (a / ".config").write_text("x\n")
    res_a = resolve(db, a)
    assert identity.checkout_line(db, res_a) is None
    assert identity.checkout_line(db, res_a, always=True)
    b = make_repo(tmp_path / "w/linux-up", {"origin": url})
    resolve(db, b)
    line = identity.checkout_line(db, resolve(db, a))
    assert line is not None
    assert line.startswith("linux-de10 · this clone ~/w/linux-de10 @main (")
    assert ".config " in line and line.endswith("· others: ~/w/linux-up @main")


def test_readonly_lookup_never_writes(tmp_path: Path) -> None:
    import os

    from survivemem import db as dbmod

    repo = make_repo(tmp_path / "r", {"origin": "https://example.com/r.git"})
    path = tmp_path / "ro.db"
    conn = dbmod.connect(path)
    assert identity.resolve(conn, repo, Config(), writable=False, env={}).project is None
    assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
    created = identity.resolve(conn, repo, Config(), env={})
    conn.close()
    os.chmod(path, 0o444)
    ro = dbmod.connect(path)
    found = identity.resolve(ro, repo, Config(), writable=False, env={})
    assert found.step == "lookup" and found.project_id == created.project_id
    assert found.checkout and created.checkout and found.checkout.id == created.checkout.id


def test_remoteless_repo_walks_history_only_once(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "local-only")
    first = resolve(db, repo)
    assert first.root is not None

    def no_walk(loc: identity.Location) -> str:
        raise AssertionError("git rev-list must not run once the path is known")

    monkeypatch.setattr(identity, "root_commit", no_walk)
    again = resolve(db, repo)
    assert again.step == "path" and again.project_id == first.project_id


def test_shallow_clone_records_no_root(db: sqlite3.Connection, tmp_path: Path) -> None:
    import subprocess

    src = make_repo(tmp_path / "src", {"origin": "https://example.com/up/x.git"}, commits=3)
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{src}", str(shallow)],
        check=True,
        capture_output=True,
    )
    git(shallow, "remote", "set-url", "origin", "https://example.com/fork/x.git")
    resolve(db, src)
    res = resolve(db, shallow)
    assert res.root is None
    assert not any(i.startswith("root:") for i in res.identities)


def test_unknown_toml_name_falls_through_to_remote(db: sqlite3.Connection, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "a", {"origin": "https://example.com/a.git"})
    first = resolve(db, repo)
    (repo / ".survivemem.toml").write_text('project = "bsp"\n')
    again = resolve(db, repo)
    assert again.step == "remote" and again.project_id == first.project_id
    owner = store.find_identity(db, "name:bsp")
    assert owner and owner.project_id == first.project_id


def test_history_walk_happens_outside_the_write_lock(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "r", {"origin": "https://example.com/r.git"})
    real = identity.root_commit
    calls: list[bool] = []

    def spy(loc: identity.Location) -> str | None:
        calls.append(db.in_transaction)
        return real(loc)

    monkeypatch.setattr(identity, "root_commit", spy)
    first = resolve(db, repo)
    assert calls == [False] and first.root is not None
    resolve(db, repo)
    assert calls == [False]  # known by its remote: no second walk


def test_probe_roundtrip_and_need_root(db: sqlite3.Connection, tmp_path: Path) -> None:
    import json

    repo = make_repo(tmp_path / "r", {"origin": "https://example.com/r.git"})
    observed = identity.probe(repo, Config())
    sent = json.loads(json.dumps(identity.probe_to_json(observed, with_root=False)))
    remote = identity.probe_from_json(sent)
    assert remote.location == observed.location and remote.identities == observed.identities
    with pytest.raises(identity.NeedRoot):
        identity.resolve(db, Path("/nonexistent"), Config(), env={}, observed=remote)
    with_root = identity.probe_from_json(identity.probe_to_json(observed, with_root=True))
    res = identity.resolve(db, Path("/nonexistent"), Config(), env={}, observed=with_root)
    assert res.step == "created" and res.root == observed.root()
    again = identity.resolve(db, Path("/nonexistent"), Config(), env={}, observed=remote)
    assert again.project_id == res.project_id and again.step == "remote"
