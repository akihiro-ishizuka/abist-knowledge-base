"""質問追跡とリマインド判定(設計 §7)。

`resolved` の判定は後続メッセージを読めることが前提だが、取得は probe 方式で
網羅性が保証されない(設計 §9-1)。返信を取りこぼすと不要な催促が出る。確証が
持てない場合はリマインドを送らない — 誤った催促を出すより見送るほうが害が
小さい(設計 §7.2)。その判断は Claude 側で行い、ここは閾値だけを決める。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict

from abist_kb.application.chat_watch.business_hours import elapsed_business_hours
from abist_kb.application.chat_watch.merge import merge_probe_results
from abist_kb.application.chat_watch.source import MessageSource
from abist_kb.application.chat_watch.state import (
    MessageRecord,
    QuestionRecord,
    WatchState,
    ingest_messages,
    load_state,
    pending_for_decision,
    prune,
    recover_interrupted,
    save_state,
)
from abist_kb.config import Settings
from abist_kb.domain.chat_watch import MessageStatus, QuestionStatus
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.notify.teams import (
    DeliveryOutcome,
    build_card,
    fingerprint,
    post_card,
)

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


class IngestResult(BaseModel):
    """`teams inbox ingest` の返り値。"""

    model_config = ConfigDict(frozen=True)

    pending: list[MessageRecord]
    per_probe: dict[str, int]
    merged: int
    duplicates: int
    recovered_unknown: list[str]
    cold_start: bool
    pruned: int


class ReplyResult(BaseModel):
    """`teams reply` の返り値。"""

    model_config = ConfigDict(frozen=True)

    message_id: str
    outcome: DeliveryOutcome


def run_ingest(settings: Settings, source: MessageSource, *, now: datetime) -> IngestResult:
    """検索結果を state へ取り込み、判断が必要な件を返す(設計 §5)。

    state が空の初回は cold start とし、何も返さない。
    """
    state_path = settings.teams_state_path
    assert state_path is not None  # `_derive_paths` で必ず埋まる
    state = load_state(state_path)
    # `not state.messages` で代用してはならない。初回に1件も取れなかった場合、
    # 永遠に cold start のままになり、以後どのメッセージにも応答しなくなる。
    cold_start = not state.initialised

    recovered = recover_interrupted(state)

    since = now - timedelta(minutes=settings.teams_overlap_minutes)
    if state.search_watermark is not None:
        since = state.search_watermark - timedelta(minutes=settings.teams_overlap_minutes)

    raw = source.fetch(since=since, probes=settings.teams_search_probes)
    merged = merge_probe_results(raw, chat_id=settings.teams_chat_id)

    ingest_messages(state, merged.messages, cold_start=cold_start)
    pruned = prune(state, now=now, retention_days=settings.teams_retention_days)

    pending: list[MessageRecord] = []
    if not cold_start:
        pending = pending_for_decision(state, limit=settings.teams_max_posts_per_tick)
    state.initialised = True
    save_state(state_path, state)

    return IngestResult(
        pending=pending,
        per_probe=merged.per_probe,
        merged=merged.merged,
        duplicates=merged.duplicates,
        recovered_unknown=recovered,
        cold_start=cold_start,
        pruned=pruned,
    )


def run_reply(
    settings: Settings,
    *,
    message_id: str,
    title: str,
    body: str,
    sources: list[str] | None,
    client: httpx.Client,
    now: datetime,
) -> ReplyResult:
    """回答を投稿し、state を遷移させる(設計 §6.1.1)。

    POST の直前に `sending` を書き込んで保存するのが要点。ここで落ちても次
    tick の `recover_interrupted` が `unknown` へ移し、自動再送はしない
    (at-most-once)。Webhook に idempotency 機構が無いため、再送すれば同じ回答を
    二重に投稿してしまう。保存を省いて「POST してから記録する」順序にすると、
    POST 成功直後のクラッシュで state には何も残らず、次 tick が同じメッセージを
    未処理のまま再選択して二重送信する — この事故を防ぐための順序であり、
    最適化で省いてはならない。
    """
    if not settings.teams_webhook_url:
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            "ABIST_KB_TEAMS_WEBHOOK_URL が未設定です。",
            hint=".env に設定してください。",
        )

    state_path = settings.teams_state_path
    assert state_path is not None  # `_derive_paths` で必ず埋まる
    state = load_state(state_path)
    record = state.messages.get(message_id)
    if record is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"未知の message_id です: {message_id}",
            hint="先に `abist-kb teams inbox ingest` を実行してください。",
        )

    payload = build_card(title, body, sources=sources)
    record.outbound_fingerprint = fingerprint(payload)
    record.status = MessageStatus.SENDING
    save_state(state_path, state)

    outcome = post_card(settings.teams_webhook_url, payload, client=client)

    # `DeliveryOutcome` と `MessageStatus` は文字列値が一致するように作ってある
    # (`infrastructure/notify/teams.py`)。どちらかの値を変える場合は両方を直すこと。
    record.status = MessageStatus(outcome.value)
    if outcome is DeliveryOutcome.ACCEPTED:
        record.accepted_at = now
    elif outcome is DeliveryOutcome.UNKNOWN:
        record.note = "送信結果が不明。自動再送しない(設計 §6.1.1)"
    save_state(state_path, state)

    return ReplyResult(message_id=message_id, outcome=outcome)
