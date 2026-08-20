"""質問追跡とリマインド判定(設計 §7)。

`resolved` の判定は後続メッセージを読めることが前提だが、取得は probe 方式で
網羅性が保証されない(設計 §9-1)。返信を取りこぼすと不要な催促が出る。確証が
持てない場合はリマインドを送らない — 誤った催促を出すより見送るほうが害が
小さい(設計 §7.2)。その判断は Claude 側で行い、ここは閾値だけを決める。
"""

from __future__ import annotations

from datetime import datetime

from abist_kb.application.chat_watch.business_hours import elapsed_business_hours
from abist_kb.application.chat_watch.state import QuestionRecord, WatchState
from abist_kb.domain.chat_watch import QuestionStatus

#: リマインドを出しうる状態。`acknowledged` を含めるのが要点。
_REMINDABLE = frozenset({QuestionStatus.OPEN, QuestionStatus.ACKNOWLEDGED})


def track_question(
    state: WatchState, *, message_id: str, asked_by: str, asked_at: datetime
) -> None:
    """質問を追跡対象に加える。すでにあれば何もしない。"""
    if message_id in state.questions:
        return
    state.questions[message_id] = QuestionRecord(
        message_id=message_id,
        status=QuestionStatus.OPEN,
        asked_by=asked_by,
        asked_at=asked_at,
    )


def mark_question(state: WatchState, *, message_id: str, status: QuestionStatus) -> None:
    """質問の状態を更新する。"""
    question = state.questions.get(message_id)
    if question is None:
        return
    question.status = status


def due_reminders(
    state: WatchState, *, now: datetime, threshold_hours: int
) -> list[QuestionRecord]:
    """営業時間内経過が閾値に達した質問を古い順に返す。

    `resolved` / `reminded` / `stale` は対象外。1つの質問につきリマインドは1回
    なので、呼び出し側は送信後に `REMINDED` へ遷移させること。
    """
    due = [
        question
        for question in state.questions.values()
        if question.status in _REMINDABLE
        and elapsed_business_hours(question.asked_at, now) >= threshold_hours
    ]
    due.sort(key=lambda q: (q.asked_at, q.message_id))
    return due
