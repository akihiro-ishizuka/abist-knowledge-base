"""リマインド判定(設計 §7)。

`acknowledged`(「確認します」)は未解決なのでリマインド対象に残す。`resolved` は
外す。1つの質問につきリマインドは1回。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from abist_kb.application.chat_watch.state import WatchState
from abist_kb.application.chat_watch.tick import (
    assign_question,
    defer_reminder,
    due_reminders,
    mark_question,
    open_todos,
    track_question,
)
from abist_kb.domain.chat_watch import QuestionStatus
from abist_kb.domain.errors import AppError, ErrorCode

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


def test_mark_reminded_stamps_reminded_at() -> None:
    """`reminded_at` はいつ催促したかの唯一の手掛かり(設計 §7 / 12.A)。"""
    state = _state_with_question(_jst(8, 20, 10))
    reminded_at = _jst(8, 20, 14)

    mark_question(state, message_id="q1", status=QuestionStatus.REMINDED, now=reminded_at)

    assert state.questions["q1"].reminded_at == reminded_at


def test_mark_reminded_without_now_leaves_reminded_at_unset() -> None:
    """`now` を渡さない既存呼び出し(Task 10)はそのまま通ること。"""
    state = _state_with_question(_jst(8, 20, 10))

    mark_question(state, message_id="q1", status=QuestionStatus.REMINDED)

    assert state.questions["q1"].reminded_at is None


def test_mark_non_reminded_status_does_not_stamp_reminded_at() -> None:
    """`REMINDED` 以外への遷移では `now` を渡しても `reminded_at` を刻まない。"""
    state = _state_with_question(_jst(8, 20, 10))

    mark_question(state, message_id="q1", status=QuestionStatus.RESOLVED, now=_jst(8, 20, 14))

    assert state.questions["q1"].reminded_at is None


def test_deferring_restarts_the_business_hours_clock() -> None:
    """「催促しないと決めた」を記録すると、次の閾値まで再提示されない(残課題1)。

    設計 §7.2 は、返信を見落としていないか確証が持てなければ催促するなと定める。
    しかし記録する手段が無いと、期限超過の質問が毎ティック再提示され続ける。
    `defer_reminder` は営業時間の時計を振り出しに戻す。
    """
    state = _state_with_question(_jst(8, 20, 10))
    assert len(due_reminders(state, now=_jst(8, 20, 14), threshold_hours=4)) == 1

    defer_reminder(state, message_id="q1", now=_jst(8, 20, 14))

    # 直後は対象外
    assert due_reminders(state, now=_jst(8, 20, 15), threshold_hours=4) == []
    # 営業時間で4時間経てば再び対象
    assert len(due_reminders(state, now=_jst(8, 21, 9), threshold_hours=4)) == 1


def test_defer_keeps_the_question_status_unchanged() -> None:
    """defer は「まだ未解決」という事実を変えない。`reminded` にしてはならない。"""
    state = _state_with_question(_jst(8, 20, 10))
    mark_question(state, message_id="q1", status=QuestionStatus.ACKNOWLEDGED)

    defer_reminder(state, message_id="q1", now=_jst(8, 20, 14))

    assert state.questions["q1"].status is QuestionStatus.ACKNOWLEDGED
    assert state.questions["q1"].deferred_at == _jst(8, 20, 14)


def test_defer_on_unknown_question_is_a_not_found_error() -> None:
    state = WatchState()

    with pytest.raises(AppError) as exc_info:
        defer_reminder(state, message_id="nope", now=_jst(8, 20, 14))

    assert exc_info.value.code is ErrorCode.NOT_FOUND


def test_assign_records_the_owner_and_survives_reassessment() -> None:
    """担当が明確な質問は担当者を記録する(毎朝判定し直すとブレるため)。"""
    state = _state_with_question(_jst(8, 20, 10))

    assign_question(state, message_id="q1", owner="t_isaka@abist.co.jp")

    assert state.questions["q1"].owner == "t_isaka@abist.co.jp"


def test_assign_accepts_none_to_return_it_to_the_team() -> None:
    """担当が明確でなくなったらチーム TODO へ戻せる。"""
    state = _state_with_question(_jst(8, 20, 10))
    assign_question(state, message_id="q1", owner="t_isaka@abist.co.jp")

    assign_question(state, message_id="q1", owner=None)

    assert state.questions["q1"].owner is None


def test_assign_on_unknown_question_is_a_not_found_error() -> None:
    with pytest.raises(AppError) as exc_info:
        assign_question(WatchState(), message_id="nope", owner="t_isaka@abist.co.jp")

    assert exc_info.value.code is ErrorCode.NOT_FOUND


def test_open_todos_split_into_owned_and_team() -> None:
    """担当が明確なら個人、不明確ならチーム TODO(朝の提示の機械的な部分)。"""
    state = WatchState()
    track_question(
        state, message_id="owned", asked_by="y_osawa@abist.co.jp", asked_at=_jst(8, 20, 9)
    )
    track_question(
        state, message_id="team", asked_by="d_suzuki@abist.co.jp", asked_at=_jst(8, 20, 10)
    )
    track_question(
        state, message_id="done", asked_by="ma_ishii@abist.co.jp", asked_at=_jst(8, 20, 11)
    )
    assign_question(state, message_id="owned", owner="t_isaka@abist.co.jp")
    mark_question(state, message_id="done", status=QuestionStatus.RESOLVED)

    todos = open_todos(state)

    assert list(todos.by_owner) == ["t_isaka@abist.co.jp"]
    assert [q.message_id for q in todos.by_owner["t_isaka@abist.co.jp"]] == ["owned"]
    assert [q.message_id for q in todos.team] == ["team"]


def test_open_todos_excludes_resolved_and_stale() -> None:
    state = WatchState()
    for mid, status in [("a", QuestionStatus.RESOLVED), ("b", QuestionStatus.STALE)]:
        track_question(
            state, message_id=mid, asked_by="d_suzuki@abist.co.jp", asked_at=_jst(8, 20, 9)
        )
        mark_question(state, message_id=mid, status=status)

    todos = open_todos(state)

    assert todos.by_owner == {}
    assert todos.team == []


def test_open_todos_keeps_reminded_questions() -> None:
    """催促済みでも未解決なら TODO に残る。消えると放置が見えなくなる。"""
    state = _state_with_question(_jst(8, 20, 10))
    mark_question(state, message_id="q1", status=QuestionStatus.REMINDED)

    assert [q.message_id for q in open_todos(state).team] == ["q1"]
