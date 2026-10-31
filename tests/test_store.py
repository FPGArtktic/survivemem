# SPDX-License-Identifier: GPL-3.0-or-later
import sqlite3

import pytest

from survivemem import store
from survivemem.store import View

IDENTIFIERS = [
    ("CONFIG_ARM64_PTR_AUTH", "Enable CONFIG_ARM64_PTR_AUTH only on v8.3 cores."),
    ("do_rootfs", "do_rootfs fails when IMAGE_INSTALL lists a missing package."),
    ("10M50DAF484C7G", "The MAX10 part is 10M50DAF484C7G on this board."),
    ("de10-nano", "The de10-nano UART is on /dev/ttyUSB1."),
    ("linux-6.6.y", "Backports go to linux-6.6.y first."),
    ("arm/boot/dts", "Board device trees live in arm/boot/dts for 32-bit."),
    ("0xFF200000", "The lightweight bridge starts at 0xFF200000."),
    ("meta-intel-fpga", "meta-intel-fpga provides the socfpga kernel recipes."),
    ("socfpga_cyclone5_de10_nano.dts", "Edit socfpga_cyclone5_de10_nano.dts for the LEDs."),
]


def add(db: sqlite3.Connection, content: str, kind: str = "fact", **kw: object) -> str:
    return store.add_memory(db, content, kind, **kw).id  # type: ignore[arg-type]


def test_add_get_by_prefix(db: sqlite3.Connection) -> None:
    m = store.add_memory(db, "  Kernel pinned to 6.6 LTS.  ", "decision", tags=["#kernel", "bsp"])
    assert m.content == "Kernel pinned to 6.6 LTS."
    assert m.tags == ("bsp", "kernel")
    assert store.get_memory(db, m.id[:20].lower()).id == m.id


def test_ambiguous_and_missing_prefix(db: sqlite3.Connection) -> None:
    add(db, "one")
    add(db, "two")
    with pytest.raises(store.Ambiguous):
        store.get_memory(db, "0")
    with pytest.raises(store.NotFound):
        store.get_memory(db, "ZZZZ")
    with pytest.raises(store.NotFound):
        store.get_memory(db, "%")


@pytest.mark.parametrize(
    ("content", "kind", "kw"),
    [
        ("", "fact", {}),
        ("x", "nonsense", {}),
        ("x", "fact", {"scope": "repo"}),
        ("x", "fact", {"source": "web"}),
        ("x", "fact", {"refs": [{"type": "blob"}]}),
        ("x", "fact", {"refs": [{"type": "file"}]}),
        ("token ghp_" + "a" * 36, "fact", {}),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "fact", {}),
        ("api_key = s3cr3tvalue123", "fact", {}),
    ],
)
def test_invalid_memories_rejected(
    db: sqlite3.Connection, content: str, kind: str, kw: dict[str, object]
) -> None:
    with pytest.raises(store.Invalid):
        store.add_memory(db, content, kind, **kw)  # type: ignore[arg-type]


def test_env_reference_is_not_a_secret(db: sqlite3.Connection) -> None:
    add(db, "token = $GITHUB_TOKEN comes from the environment")


def test_refs_are_canonicalised(db: sqlite3.Connection) -> None:
    m = store.add_memory(db, "x", "fact", refs='{"type": "investigation", "id": "01AB"}')
    assert m.refs == '[{"type":"investigation","id":"01AB"}]'


def test_forget_and_restore(db: sqlite3.Connection) -> None:
    mid = add(db, "temporary fact")
    assert store.forget_memory(db, mid).deleted_at is not None
    with pytest.raises(store.NotFound):
        store.get_memory(db, mid)
    assert store.list_memories(db, store.ALL) == []
    assert store.search(db, "temporary", store.ALL) == []
    assert store.restore_memory(db, mid).deleted_at is None


def test_edit(db: sqlite3.Connection) -> None:
    mid = add(db, "old", "candidate", tags=["a", "b"])
    edit = store.Edit(content="new", kind="fact", add_tags=("c",), rm_tags=("a",), project="p")
    m = store.edit_memory(db, mid, edit)
    assert (m.content, m.kind, m.tags, m.project) == ("new", "fact", ("b", "c"), "p")
    assert store.edit_memory(db, mid, store.Edit(make_global=True)).project is None


def test_list_filters(db: sqlite3.Connection) -> None:
    add(db, "global pref", "preference", tags=["ui"])
    add(db, "project fact", project="p1")
    add(db, "other project", project="p2")
    add(db, "a candidate", "candidate", project="p1")
    view = View(projects=("p1",))
    # rows added within one millisecond tie on updated_at, so compare as sets
    assert {m.content for m in store.list_memories(db, view)} == {"project fact", "global pref"}
    assert [m.content for m in store.list_memories(db, View())] == ["global pref"]
    only_p1 = View(projects=("p1",), include_global=False)
    assert [m.content for m in store.list_memories(db, only_p1)] == ["project fact"]
    assert len(store.list_memories(db, store.ALL)) == 3
    assert [m.content for m in store.list_memories(db, store.ALL, tag="ui")] == ["global pref"]
    cands = store.list_memories(db, store.ALL, kind="candidate")
    assert [m.content for m in cands] == ["a candidate"]
    assert store.list_memories(db, store.ALL, since=10**15) == []
    assert len(store.list_memories(db, store.ALL, limit=1)) == 1


@pytest.mark.parametrize(("query", "content"), IDENTIFIERS)
def test_search_keeps_identifiers_whole(db: sqlite3.Connection, query: str, content: str) -> None:
    for _, text in IDENTIFIERS:
        add(db, text)
    hits = store.search(db, query, store.ALL)
    assert hits[0].memory.content == content
    assert hits[0].matched_by == ("fts",)


def test_search_substring_falls_back_to_trigram(db: sqlite3.Connection) -> None:
    add(db, "Enable CONFIG_ARM64_PTR_AUTH only on v8.3 cores.")
    add(db, "Unrelated memory about bitbake.")
    hits = store.search(db, "PTR_AUTH", store.ALL)
    assert [h.memory.content[:6] for h in hits] == ["Enable"]
    assert hits[0].matched_by == ("trigram",)


def test_search_matches_word_before_sentence_punctuation(db: sqlite3.Connection) -> None:
    add(db, "Kernel pinned to 6.6 LTS.")
    assert store.search(db, "which LTS?", store.ALL)


def test_search_excludes_candidates_and_stopword_only_queries(db: sqlite3.Connection) -> None:
    add(db, "the quartus build uses 23.1", "candidate")
    add(db, "the board is a de10-nano")
    assert store.search(db, "quartus", store.ALL) == []
    assert store.search(db, "what is the", store.ALL) == []


def test_search_visibility(db: sqlite3.Connection) -> None:
    add(db, "zephyr global")
    add(db, "zephyr p1", project="p1")
    add(db, "zephyr p2", project="p2")
    add(db, "zephyr branch", project="p1", scope="branch:socfpga-6.6")
    add(db, "zephyr clone", project="p1", scope="checkout:abc")
    plain = View(projects=("p1",), scopes=store.scopes_for("master", None))
    assert {h.memory.content for h in store.search(db, "zephyr", plain)} == {
        "zephyr global",
        "zephyr p1",
    }
    scoped = View(projects=("p1",), scopes=store.scopes_for("socfpga-6.6", "abc"))
    assert len(store.search(db, "zephyr", scoped)) == 4
    assert {h.memory.content for h in store.search(db, "zephyr", View())} == {"zephyr global"}


def test_linked_projects_share_memories(db: sqlite3.Connection) -> None:
    parent = store.create_project(db, "bsp")
    child = store.create_project(db, "meta-foo")
    add(db, "zephyr in child", project=child.id)
    add(db, "zephyr in parent", project=parent.id)
    store.link_projects(db, parent.id, child.id)
    from_parent = store.visible_view(db, parent.id)
    from_child = store.visible_view(db, child.id)
    assert len(store.search(db, "zephyr", from_parent)) == 2
    assert len(store.search(db, "zephyr", from_child)) == 2


def test_fuse_combines_rankings(db: sqlite3.Connection) -> None:
    a = store.add_memory(db, "a", "fact")
    b = store.add_memory(db, "b", "fact")
    hits = store.fuse([(a, -2.0, "fts"), (b, -1.0, "fts")], [(b, 0.1)], limit=8)
    assert hits[0].memory.id == b.id
    assert hits[0].matched_by == ("fts", "vec")
    assert hits[0].vec_distance == 0.1


def test_projects_merge_and_aliases(db: sqlite3.Connection) -> None:
    src = store.create_project(db, "src")
    dst = store.create_project(db, "dst")
    store.attach_identities(db, src.id, ["remote:example.com/a", "path:/a/.git"])
    add(db, "moved", project=src.id)
    store.upsert_checkout(db, src.id, store.CheckoutState("/a", "main", None, None))
    store.merge_projects(db, src.id, dst.id)
    assert {i.identity for i in store.identities_of(db, dst.id)} == {
        "remote:example.com/a",
        "path:/a/.git",
    }
    assert store.list_memories(db, View(projects=(dst.id,)))[0].content == "moved"
    assert store.list_checkouts(db, dst.id)[0].path == "/a"
    with pytest.raises(store.NotFound):
        store.get_project(db, src.id)


def test_alias_add_steals_only_with_consent(db: sqlite3.Connection) -> None:
    a = store.create_project(db, "a")
    b = store.create_project(db, "b")
    store.attach_identities(db, a.id, ["remote:example.com/x"])
    with pytest.raises(store.Invalid):
        store.alias_add(db, b.id, "remote:example.com/x", steal=False)
    assert store.alias_add(db, b.id, "remote:example.com/x", steal=True).project_id == b.id
    with pytest.raises(store.Invalid):
        store.alias_add(db, b.id, "bogus:x", steal=True)
    store.alias_rm(db, "remote:example.com/x")
    assert store.find_identity(db, "remote:example.com/x") is None


def test_project_lookup_by_label_and_prefix(db: sqlite3.Connection) -> None:
    p = store.create_project(db, "linux")
    assert store.get_project(db, "linux").id == p.id
    assert store.get_project(db, p.id[:12]).id == p.id
    store.create_project(db, "linux")
    with pytest.raises(store.Ambiguous):
        store.get_project(db, "linux")


def test_checkouts_prune_and_purge(db: sqlite3.Connection) -> None:
    p = store.create_project(db, "linux")
    c = store.upsert_checkout(db, p.id, store.CheckoutState("/w/a", "main", "abc", None))
    keep = store.upsert_checkout(db, p.id, store.CheckoutState("/w/b", "dev", None, None))
    add(db, "fact about b", project=p.id, scope=f"checkout:{keep.id}")
    assert store.label_checkout(db, c.id[:6], "bisect").label == "bisect"
    store.mark_checkout_gone(db, c.id)
    store.mark_checkout_gone(db, keep.id)
    assert store.purge_checkouts(db) == 1
    assert [x.path for x in store.list_checkouts(db)] == ["/w/b"]


def test_fts_rebuild_and_check(db: sqlite3.Connection) -> None:
    add(db, "something to index")
    store.rebuild_fts(db)
    assert store.fts_ok(db)
    assert store.kind_counts(db) == {"fact": 1}


def test_investigation_lifecycle(db: sqlite3.Connection) -> None:
    inv = store.open_investigation(
        db, "p1", "UART hang", "hangs after fpga load", env={"environment": ["MACHINE=cyclone5"]}
    )
    assert inv.status == "open" and store.open_investigation_of(db, "p1") == inv
    with pytest.raises(store.Invalid, match="already open"):
        store.open_investigation(db, "p1", "another", "x")
    h = store.add_entry(db, inv.id, store.NewEntry("hypothesis", "bridge not enabled"))
    e = store.add_entry(
        db,
        inv.id,
        store.NewEntry(
            "experiment", "reload with bridges", command="dmesg", result="bad", anchor="abc"
        ),
    )
    assert (h.seq, e.seq) == (1, 2)
    assert [x.seq for x in store.entries_by_command(db, inv.id, "dmesg")] == [2]
    with pytest.raises(store.Invalid, match="command"):
        store.add_entry(db, inv.id, store.NewEntry("experiment", "no command"))
    with pytest.raises(store.Invalid, match="result"):
        store.add_entry(db, inv.id, store.NewEntry("experiment", "x", command="c", result="ok"))
    paused = store.set_investigation_status(db, inv.id, "paused")
    assert paused.closed_at is None and store.open_investigation_of(db, "p1") is None
    store.open_investigation(db, "p1", "second", "y")
    solved = store.set_investigation_status(db, inv.id, "solved", root_cause="rc", fix="f")
    assert solved.closed_at is not None and (solved.root_cause, solved.fix) == ("rc", "f")
    assert {i.title for i in store.list_investigations(db, "p1")} == {"UART hang", "second"}
    assert [i.id for i in store.list_investigations(db, grep="bridge")] == [inv.id]
    assert store.get_investigation(db, inv.id[:12]).id == inv.id


def test_evidence_excerpt_cut(db: sqlite3.Connection) -> None:
    inv = store.open_investigation(db, "p1", "t", "s")
    ev = store.add_evidence(db, inv.id, "/tmp/dmesg.txt", "ab" * 32, 100, "dmesg", "x" * 2000)
    assert len(ev.excerpt) == 1000 and store.get_evidence(db, ev.id) == ev
    assert store.evidence_bytes_under(db, inv.id, "/srv/") == 0
    assert store.evidence_bytes_under(db, inv.id, "/tmp/") == 100


def test_merge_moves_investigations(db: sqlite3.Connection) -> None:
    src, dst = store.create_project(db, "src"), store.create_project(db, "dst")
    inv = store.open_investigation(db, src.id, "t", "s")
    store.merge_projects(db, src.id, dst.id)
    assert store.get_investigation(db, inv.id).project == dst.id


def test_secret_command_and_ambiguous_merge_refused(db: sqlite3.Connection) -> None:
    a, b = store.create_project(db, "a"), store.create_project(db, "b")
    inv = store.open_investigation(db, a.id, "t", "s")
    store.open_investigation(db, b.id, "t", "s")
    with pytest.raises(store.Invalid, match="secret"):
        store.add_entry(
            db,
            inv.id,
            store.NewEntry("experiment", "x", command="curl -H 'token: ghp_" + "c" * 36 + "'"),
        )
    with pytest.raises(store.Invalid, match="open investigation"):
        store.merge_projects(db, a.id, b.id)


def test_trigram_skips_short_plain_words(db: sqlite3.Connection) -> None:
    add(db, "Quartus fitter needs the LogicLock region removed")
    add(db, "Kernel pinned because the vendor BSP breaks")
    assert store.search(db, "UART speed", store.ALL) == []
    assert store.search(db, "which version do we use", store.ALL) == []
    assert store.search(db, "gicLock", store.ALL)[0].matched_by == ("trigram",)
