"""リマインド判定(設計 §7)。

`acknowledged`(「確認します」)は未解決なのでリマインド対象に残す。`resolved` は
外す。1つの質問につきリマインドは1回。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from abist_kb.application.chat_watch.state import WatchState
from abist_kb.application.chat_watch.tick import (
    due_reminders,
    mark_question,
    track_question,
)
from abist_kb.domain.chat_watch import QuestionStatus

JST = ZoneInfo("Asia/Tokyo")


def _jst(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=JST)


def _state_with_question(asked_at: datetime) -> WatchState:
    state = WatchState()
    track_question(state, message_id="q1", asked_by="t_isaka@abist.co.jp", asked_at=asked_at)
    return state


def test_open_question_is_due_after_four_business_hours() -> None:
    state = _state_with_question(_jst(8, 20, 10))

    assert due_reminders(state, now=_jst(8, 20, 13, 59), threshold_hours=4) == []
    assert [q.message_id for q in due_reminders(state, now=_jst(8, 20, 14), threshold_hours=4)] == [
        "q1"
    ]


def test_friday_evening_question_fires_monday_noon() -> None:
    """設計 §7.1 の正本の例。2026-08-21 が金曜、2026-08-24 が月曜。"""
    state = _state_with_question(_jst(8, 21, 17))

    assert due_reminders(state, now=_jst(8, 24, 11, 59), threshold_hours=4) == []
    assert len(due_reminders(state, now=_jst(8, 24, 12), threshold_hours=4)) == 1


def test_acknowledged_still_reminds() -> None:
    """「確認します」は acknowledged であって resolved ではない。"""
    state = _state_with_question(_jst(8, 20, 10))
    mark_question(state, message_id="q1", status=QuestionStatus.ACKNOWLEDGED)

    assert len(due_reminders(state, now=_jst(8, 20, 14), threshold_hours=4)) == 1


def test_resolved_does_not_remind() -> None:
    state = _state_with_question(_jst(8, 20, 10))
    mark_question(state, message_id="q1", status=QuestionStatus.RESOLVED)

    assert due_reminders(state, now=_jst(8, 20, 14), threshold_hours=4) == []


def test_stale_does_not_remind() -> None:
    state = _state_with_question(_jst(8, 20, 10))
    mark_question(state, message_id="q1", status=QuestionStatus.STALE)

    assert due_reminders(state, now=_jst(8, 20, 14), threshold_hours=4) == []


def test_reminds_only_once() -> None:
    state = _state_with_question(_jst(8, 20, 10))
    due = due_reminders(state, now=_jst(8, 20, 14), threshold_hours=4)
    for question in due:
        mark_question(state, message_id=question.message_id, status=QuestionStatus.REMINDED)

    assert due_reminders(state, now=_jst(8, 21, 14), threshold_hours=4) == []
