# SPDX-License-Identifier: GPL-3.0-or-later
import sqlite3
from pathlib import Path

import pytest

from survivemem import identity, investigate, store
from survivemem.config import Config
from survivemem.investigate import normalise_command
from survivemem.model import Entry
from tests.helpers import make_repo


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cd build && bitbake core-image-minimal 2>&1 | tee log.txt", "bitbake core-image-minimal"),
        ("sudo dmesg | tail -50", "dmesg"),
        (
            "env MACHINE=cyclone5 bitbake -c compile linux",
            "MACHINE=cyclone5 bitbake -c compile linux",
        ),
        (
            "ARCH=arm CROSS_COMPILE=arm-linux- make -j8 zImage",
            "ARCH=arm CROSS_COMPILE=arm-linux- make -j8 zImage",
        ),
        ("cd a && cd b && sudo -E make   -C  x", "make -C x"),
        ("openocd -f board.cfg 2>&1 | head -n 20", "openocd -f board.cfg"),
        ("cp out.rbf /srv/tftp/out-2026-10-31T08:12:00.rbf", "cp out.rbf /srv/tftp/out-.rbf"),
        ("quartus_pgm -c 1 -m jtag -o p;top.sof", "quartus_pgm -c 1 -m jtag -o p;top.sof"),
    ],
)
def test_normalise_command(raw: str, expected: str) -> None:
    assert normalise_command(raw) == expected


def test_normalise_workspace_and_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "w/linux"
    assert normalise_command(f"make -C {ws}/drivers", ws) == "make -C $WS/drivers"
    assert normalise_command(f"cat {tmp_path}/notes.txt") == "cat ~/notes.txt"


@pytest.fixture
def res(db: sqlite3.Connection, tmp_path: Path) -> identity.Resolution:
    repo = make_repo(tmp_path / "linux", {"origin": "https://example.com/linux.git"})
    (repo / ".config").write_text("CONFIG_X=y\n")
    return identity.resolve(db, repo, Config(), env={})


def experiment(
    db: sqlite3.Connection,
    res: identity.Resolution,
    inv_id: str,
    cmd: str,
    result: str = "bad",
    sha: str | None = "e1",
) -> Entry:
    inv = store.get_investigation(db, inv_id)
    return investigate.log_entry(
        db,
        res,
        inv,
        store.NewEntry("experiment", f"run {cmd}", command=cmd, result=result, excerpt_sha=sha),
    )


def test_anchor_includes_config(res: identity.Resolution) -> None:
    anchor = investigate.current_anchor(res)
    assert anchor and len(anchor) == 40 + 1 + 8 and "+" in anchor


def test_open_snapshots_environment(db: sqlite3.Connection, res: identity.Resolution) -> None:
    store.add_memory(db, "MACHINE=cyclone5", "environment", project=res.project_id)
    inv = investigate.open_new(db, res, "UART hang", "hangs after fpga load", "board=de10-nano")
    setup = investigate.setup_line(inv.env)
    assert setup.startswith("MACHINE=cyclone5 linux@") and setup.endswith("board=de10-nano")
    assert inv.checkout_id == (res.checkout.id if res.checkout else None)


def test_guard_blocks_only_unanswered_failure_at_same_anchor(
    db: sqlite3.Connection, res: identity.Resolution
) -> None:
    inv = investigate.open_new(db, res, "t", "s")
    anchor = investigate.current_anchor(res)
    assert investigate.guard([], "dmesg", anchor, {}) is None
    first = experiment(db, res, inv.id, "sudo dmesg | tail")
    assert first.command == "dmesg" and first.anchor == anchor
    entries = store.entries_of(db, inv.id)
    verdict = investigate.guard(entries, "dmesg", anchor, {})
    assert (
        verdict
        and verdict.blocking
        and verdict.message.startswith(f"LOOP GUARD: identical to experiment #{first.seq} (")
    )
    other = investigate.guard(entries, "dmesg", "deadbeef", {})
    assert other and not other.blocking and other.message.startswith("seen as #1 (bad")
    investigate.log_entry(db, res, inv, store.NewEntry("hypothesis", "bridges now enabled"))
    answered = investigate.guard(store.entries_of(db, inv.id), "dmesg", anchor, {})
    assert answered and not answered.blocking


def test_loop_detection_after_three_identical_failures(
    db: sqlite3.Connection, res: identity.Resolution
) -> None:
    inv = investigate.open_new(db, res, "t", "s")
    for _ in range(2):
        experiment(db, res, inv.id, "make zImage")
    assert investigate.loop_note(store.entries_of(db, inv.id), "make zImage", "e1") is None
    experiment(db, res, inv.id, "make zImage")
    note = investigate.loop_note(store.entries_of(db, inv.id), "make zImage", "e1")
    assert note == "LOOP: make zImage failed identically ×3 — #1, #2, #3"
    investigate.log_entry(db, res, inv, store.NewEntry("note", note))
    entries = store.entries_of(db, inv.id)
    assert investigate.loop_note(entries, "make zImage", "e1") is None
    assert investigate.blocker(entries) == note
    assert investigate.loops(entries)[0]["failures"] == 3
    investigate.log_entry(db, res, inv, store.NewEntry("hypothesis", "stale .config"))
    assert investigate.blocker(store.entries_of(db, inv.id)) is None


def test_render_sections_and_budget(
    db: sqlite3.Connection,
    res: identity.Resolution,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # clone path renders as ~/linux
    inv = investigate.open_new(db, res, "UART hang", "hangs " * 100)
    for i in range(60):
        investigate.log_entry(
            db,
            res,
            inv,
            store.NewEntry(
                "ruled_out", f"cause number {i} ruled out because of a long reason " * 3
            ),
        )
    investigate.log_entry(db, res, inv, store.NewEntry("hypothesis", "bridge reset order"))
    experiment(db, res, inv.id, "dmesg")
    ctx = investigate.context(db, inv, res, ["check h2f bridge"])
    out = investigate.render(ctx)
    assert len(out) <= investigate.BUDGET
    lines = out.splitlines()
    assert lines[0].startswith(f'INVESTIGATION {inv.id} "UART hang"  status=open  since ')
    assert lines[1].startswith("SYMPTOM   hangs") and len(lines[1]) <= 10 + 300
    assert "RULED OUT (60, last 40)" in lines and "  (+20 older)" in lines
    assert sum(1 for x in lines if x.startswith("  - ") and "[#" in x and "cause" in x) == 40
    assert lines[-1] == "DO NOT RE-RUN without a new hypothesis: dmesg"
    assert any(x.startswith("NEXT      check h2f bridge") for x in lines)
    assert "opened in this clone" in out


def test_close_distils_pitfall(db: sqlite3.Connection, res: identity.Resolution) -> None:
    inv = investigate.open_new(db, res, "UART hang", "UART hangs after fpga load")
    with pytest.raises(store.Invalid):
        investigate.close(db, res, inv, "", "x")
    closed, pitfall = investigate.close(db, res, inv, "bridges disabled", "enable in overlay")
    assert closed.status == "solved" and pitfall is not None
    assert pitfall.content == (
        "UART hangs after fpga load → root cause: bridges disabled → fix: enable in overlay"
    )
    assert pitfall.kind == "pitfall" and pitfall.source == "investigation"
    assert pitfall.refs == f'[{{"type":"investigation","id":"{inv.id}"}}]'
    assert pitfall.anchor == investigate.current_anchor(res)
    other = investigate.open_new(db, res, "t2", "s2")
    assert investigate.close(db, res, other, "unknown", "none", "abandoned")[1] is None


def test_attach_bisect_and_postmortem(
    db: sqlite3.Connection, res: identity.Resolution, tmp_path: Path
) -> None:
    inv = investigate.open_new(db, res, "fit fails", "Can't fit design")
    log = Path(__file__).parent / "fixtures" / "logs" / "quartus-fit.log"
    ev = investigate.attach(db, inv, log, "quartus")
    assert Path(ev.path).name == "001-quartus-fit.log" and Path(ev.path).exists()
    assert "Error (11802)" in ev.excerpt and ev.tool == "quartus"
    store.add_entry(
        db,
        inv.id,
        store.NewEntry("experiment", "build A", command="quartus_sh", result="good", anchor="aaa"),
    )
    store.add_entry(
        db,
        inv.id,
        store.NewEntry(
            "experiment",
            "build B",
            command="quartus_sh",
            result="bad",
            anchor="bbb",
            evidence_id=ev.id,
        ),
    )
    table = investigate.bisect(store.entries_of(db, inv.id))
    assert table["boundary"]["last_good"]["anchor"] == "aaa"
    assert table["boundary"]["first_bad_after"]["anchor"] == "bbb"
    md = investigate.postmortem(db, inv)
    for heading in (
        "# fit fails",
        "## Symptom",
        "## Setup under test",
        "## Timeline",
        "## Ruled out",
        "## Root cause",
        "## Fix",
        "## Evidence",
    ):
        assert heading in md
    assert "| 2 | experiment | bad | build B — `quartus_sh` | bbb |" in md
    assert f"`001-quartus-fit.log` sha256 `{ev.sha256}`" in md


def test_excerpt_sha_ignores_pids_and_timestamps() -> None:
    from survivemem import ingest

    log = (Path(__file__).parent / "fixtures" / "logs" / "bitbake-compile.log").read_text()
    a = ingest.extract(log, "bitbake").excerpt
    b = ingest.extract(log.replace("log.do_compile.5678", "log.do_compile.9911"), "bitbake").excerpt
    assert a != b and investigate.excerpt_sha(a) == investigate.excerpt_sha(b)
    dmesg = "[    3.401220] fpga_manager fpga0: Error after writing image data to FPGA"
    later = dmesg.replace("3.401220", "17.000001")
    assert investigate.excerpt_sha(dmesg) == investigate.excerpt_sha(later)
    assert investigate.excerpt_sha(dmesg) != investigate.excerpt_sha(dmesg + " (other)")


def test_loop_note_for_one_command_does_not_hide_another(
    db: sqlite3.Connection, res: identity.Resolution
) -> None:
    inv = investigate.open_new(db, res, "t", "s")
    investigate.log_entry(
        db, res, inv, store.NewEntry("note", "LOOP: make -j8 failed identically ×3 — #1, #2, #3")
    )
    for _ in range(3):
        experiment(db, res, inv.id, "make")
    assert investigate.loop_note(store.entries_of(db, inv.id), "make", "e1") is not None


@pytest.mark.parametrize(
    ("command", "debug"),
    [
        ("make -j8 zImage", True),
        ("cd build && sudo -E /usr/bin/make modules", True),
        ("ARCH=arm make dtbs 2>&1 | tail", True),
        ("dmesg | grep fpga", True),
        ("quartus_sh --flow compile top", True),
        ("arm-none-eabi-gdb vmlinux", True),
        ("nios2-download -g elf", True),
        ("sudo dd if=x.wic of=/dev/sdb", True),
        ("git add -A", False),
        ("yarn add left-pad", False),
        ("cmake --build .", False),
        ("makepkg -si", False),
        ("grep -rn make .", False),
    ],
)
def test_is_debug_command(command: str, debug: bool) -> None:
    from survivemem.config import DEFAULT_DEBUG_COMMANDS

    assert investigate.is_debug_command(command, DEFAULT_DEBUG_COMMANDS) is debug


def test_evidence_cap_checked_before_copying(
    db: sqlite3.Connection,
    res: identity.Resolution,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from survivemem import paths

    inv = investigate.open_new(db, res, "t", "s")
    big = tmp_path / "big.log"
    big.write_text("x" * 300)
    monkeypatch.setattr(store, "EVIDENCE_CAP_BYTES", 200)
    with pytest.raises(store.Invalid, match="--no-copy"):
        investigate.attach(db, inv, big, "other")
    assert not paths.evidence_dir(inv.id).exists()
    linked = investigate.attach(db, inv, big, "other", copy=False)
    assert linked.path == str(big)
