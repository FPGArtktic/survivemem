# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from survivemem import transcript

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_basic.jsonl"


def test_user_prompts_skip_tool_results_sidechain_and_meta() -> None:
    prompts = transcript.user_prompts(transcript.tail(FIXTURE))
    assert prompts == ["Why does do_rootfs fail on the de10-nano image?"]


def test_assistant_messages_join_blocks_by_id_and_skip_thinking() -> None:
    messages = transcript.assistant_messages(transcript.tail(FIXTURE))
    assert messages == [
        "Let me look at the log.",
        "The package nonexistent-pkg in IMAGE_INSTALL does not exist.\n"
        "Remove it from local.conf and rebuild.",
    ]


def test_tail_limits_lines_and_tolerates_missing_file(tmp_path: Path) -> None:
    assert len(transcript.tail(FIXTURE, max_lines=2)) == 2
    assert transcript.tail(tmp_path / "absent.jsonl") == []
    assert transcript.last([]) == ""


def test_compaction_summary_is_not_a_prompt(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"type":"user","message":{"content":"real prompt"}}\n'
        '{"type":"user","isCompactSummary":true,"message":{"content":"This session is being '
        'continued from a previous conversation"}}\n'
    )
    assert transcript.user_prompts(transcript.tail(path)) == ["real prompt"]


def test_compact_keeps_what_hooks_read() -> None:
    from survivemem import state

    full = transcript.tail(FIXTURE)
    small = transcript.compact(full)
    assert transcript.user_prompts(small) == transcript.user_prompts(full)
    assert transcript.assistant_messages(small) == transcript.assistant_messages(full)
    assert state.from_transcript(small) == state.from_transcript(full)
    assert "tool_result" not in str(small) and "secret reasoning" not in str(small)
    assert "old_string" not in str(small)  # Edit inputs keep file_path only
