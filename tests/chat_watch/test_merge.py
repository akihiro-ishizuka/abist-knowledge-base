"""probe 結果のマージ(設計 §5.1)。

チャットIDで絞れずクエリ必須のため、複数 probe の結果を統合して重複を除く。
検索品質を観測できるよう probe ごとの件数を残す。
"""

from __future__ import annotations

from datetime import UTC, datetime

from abist_kb.application.chat_watch.merge import merge_probe_results
from abist_kb.domain.chat_watch import InboundMessage

CHAT_ID = "19:meeting_target@thread.v2"
OTHER_CHAT = "19:meeting_other@thread.v2"


def _message(message_id: str, chat_id: str = CHAT_ID, minute: int = 0) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_email="t_isaka@abist.co.jp",
        sender_name="井坂 孝",
        body="本文",
        created_at=datetime(2026, 8, 20, 8, minute, tzinfo=UTC),
    )


def test_deduplicates_across_probes() -> None:
    result = merge_probe_results(
        {"い": [_message("a"), _message("b")], "の": [_message("b"), _message("c")]},
        chat_id=CHAT_ID,
    )

    assert [m.message_id for m in result.messages] == ["a", "b", "c"]
    assert result.merged == 3
    assert result.duplicates == 1


def test_filters_other_chats() -> None:
    result = merge_probe_results(
        {"い": [_message("a"), _message("x", chat_id=OTHER_CHAT)]},
        chat_id=CHAT_ID,
    )

    assert [m.message_id for m in result.messages] == ["a"]


def test_per_probe_counts_are_after_chat_filter() -> None:
    """probe 別件数は対象チャットに絞ったあとの数。検索品質の観測が目的。"""
    result = merge_probe_results(
        {
            "い": [_message("a"), _message("x", chat_id=OTHER_CHAT)],
            "の": [_message("b")],
        },
        chat_id=CHAT_ID,
    )

    assert result.per_probe == {"い": 1, "の": 1}


def test_sorted_by_created_at() -> None:
    result = merge_probe_results(
        {"い": [_message("late", minute=30), _message("early", minute=5)]},
        chat_id=CHAT_ID,
    )

    assert [m.message_id for m in result.messages] == ["early", "late"]


def test_empty_input() -> None:
    result = merge_probe_results({}, chat_id=CHAT_ID)

    assert result.messages == []
    assert result.merged == 0
    assert result.duplicates == 0
    assert result.per_probe == {}
