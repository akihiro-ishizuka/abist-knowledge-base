"""state の遷移(設計 §5.2, §5.3, §6.1.1, §6.2)。

ここで固めるのは順序の要件。プロンプト任せにしない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from abist_kb.application.chat_watch.state import (
    MessageRecord,
    QuestionRecord,
    WatchState,
    ingest_messages,
    pending_for_decision,
    prune,
    recover_interrupted,
)
from abist_kb.domain.chat_watch import InboundMessage, MessageStatus, QuestionStatus

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _message(
    message_id: str, minute: int = 0, sender: str = "t_isaka@abist.co.jp"
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        chat_id="19:x@thread.v2",
        sender_email=sender,
        sender_name="井坂 孝",
        body="本文",
        created_at=NOW + timedelta(minutes=minute),
    )


def test_cold_start_marks_everything_closed_and_returns_nothing() -> None:
    state = WatchState()

    fresh = ingest_messages(state, [_message("a"), _message("b")], cold_start=True)

    assert fresh == []
    assert state.messages["a"].status is MessageStatus.CLOSED_COLD_START
    assert state.messages["b"].status is MessageStatus.CLOSED_COLD_START


def test_ingest_returns_only_unseen_messages() -> None:
    state = WatchState()
    ingest_messages(state, [_message("a")], cold_start=False)

    fresh = ingest_messages(state, [_message("a"), _message("b")], cold_start=False)

    assert [m.message_id for m in fresh] == ["b"]


def test_ingest_advances_watermark_to_max_created_at() -> None:
    state = WatchState()

    ingest_messages(state, [_message("a", minute=5), _message("b", minute=40)], cold_start=False)

    assert state.search_watermark == NOW + timedelta(minutes=40)


def test_overlap_refetch_does_not_duplicate() -> None:
    """overlap で同じメッセージを再取得しても二度と処理対象にしない。"""
    state = WatchState()
    ingest_messages(state, [_message("a")], cold_start=False)
    state.messages["a"].status = MessageStatus.ACCEPTED

    fresh = ingest_messages(state, [_message("a")], cold_start=False)

    assert fresh == []
    assert state.messages["a"].status is MessageStatus.ACCEPTED


def test_pending_is_capped_and_remainder_is_carried_over() -> None:
    """上限は「accepted へ遷移させる件数」。残りは discovered のまま次 tick へ。"""
    state = WatchState()
    messages = [_message(str(i), minute=i) for i in range(8)]
    ingest_messages(state, messages, cold_start=False)

    selected = pending_for_decision(state, limit=3)

    assert [m.message_id for m in selected] == ["0", "1", "2"]
    assert sum(1 for r in state.messages.values() if r.status is MessageStatus.DISCOVERED) == 5


def test_pending_skips_context_only_senders() -> None:
    state = WatchState()
    ingest_messages(
        state,
        [_message("a", sender="yamaura@abist.co.jp"), _message("b", minute=1)],
        cold_start=False,
    )

    selected = pending_for_decision(state, limit=3)

    assert [m.message_id for m in selected] == ["b"]
    assert state.messages["a"].status is MessageStatus.SKIPPED


def test_sending_becomes_unknown_and_is_not_retried() -> None:
    """設計 §6.1.1: 送信結果が不明なら自動再送しない(at-most-once)。"""
    state = WatchState(
        messages={
            "a": MessageRecord(
                message_id="a",
                status=MessageStatus.SENDING,
                sender_email="t_isaka@abist.co.jp",
                created_at=NOW,
            )
        }
    )

    recovered = recover_interrupted(state)

    assert recovered == ["a"]
    assert state.messages["a"].status is MessageStatus.UNKNOWN
    assert pending_for_decision(state, limit=3) == []


def test_failed_is_retried() -> None:
    """4xx/5xx を受領した明確な失敗は再送してよい。"""
    state = WatchState(
        messages={
            "a": MessageRecord(
                message_id="a",
                status=MessageStatus.FAILED,
                sender_email="t_isaka@abist.co.jp",
                created_at=NOW,
            )
        }
    )

    assert [m.message_id for m in pending_for_decision(state, limit=3)] == ["a"]


def test_prune_removes_old_messages_only() -> None:
    state = WatchState(
        messages={
            "old": MessageRecord(
                message_id="old",
                status=MessageStatus.ACCEPTED,
                sender_email="t_isaka@abist.co.jp",
                created_at=NOW - timedelta(days=31),
            ),
            "recent": MessageRecord(
                message_id="recent",
                status=MessageStatus.ACCEPTED,
                sender_email="t_isaka@abist.co.jp",
                created_at=NOW - timedelta(days=29),
            ),
        }
    )

    removed = prune(state, now=NOW, retention_days=30)

    assert removed == 1
    assert set(state.messages) == {"recent"}


def test_prune_keeps_tracked_questions_but_marks_them_stale() -> None:
    """追跡中の質問は削除せず stale にしてリマインドを止める(設計 §6.2)。"""
    state = WatchState(
        questions={
            "old": QuestionRecord(
                message_id="old",
                status=QuestionStatus.OPEN,
                asked_by="t_isaka@abist.co.jp",
                asked_at=NOW - timedelta(days=31),
            )
        }
    )

    prune(state, now=NOW, retention_days=30)

    assert state.questions["old"].status is QuestionStatus.STALE
