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


def mark_question(
    state: WatchState,
    *,
    message_id: str,
    status: QuestionStatus,
    now: datetime | None = None,
) -> None:
    """質問の状態を更新する。

    `REMINDED` へ遷移させるときは `reminded_at` を刻む。いつ催促したかが残らないと、
    二重催促が起きても後から検証できない。
    """
    question = state.questions.get(message_id)
    if question is None:
        return
    question.status = status
    if status is QuestionStatus.REMINDED and now is not None:
        question.reminded_at = now


#: skip してよい状態。判断待ちのものだけ。`unknown` を含めないのが要点で、
#: 「届いたか判らない」は上書きしてよい記録ではない(設計 §6.1.1)。
_SKIPPABLE = frozenset({MessageStatus.DISCOVERED, MessageStatus.PROCESSING, MessageStatus.FAILED})


def skip_message(state: WatchState, *, message_id: str, reason: str | None) -> MessageRecord:
    """「質問・依頼ではない」と判定したメッセージを `skipped` へ進める。

    `pending_for_decision` は選んだレコードを `processing` へ進めるだけで、
    `processing` は次 tick でも再選択される(`_RETRYABLE` に含まれるため)。
    質問でないと判定した場合はここを呼んで明示的に `_RETRYABLE` から外さないと、
    同じレコードが古株として選ばれ続け、新着メッセージが後回しになる(設計 §5.2,
    §6.1 の `skipped`)。呼び出し元不明の message_id は `NOT_FOUND` とする
    (`questions track` と同じ扱い)。
    """
    record = state.messages.get(message_id)
    if record is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"未知の message_id です: {message_id}",
            hint="先に `abist-kb teams inbox ingest` を実行してください。",
        )
    if record.status not in _SKIPPABLE:
        raise AppError(
            ErrorCode.CONFLICT,
            f"この状態のメッセージは skip できません: {record.status.value}",
            hint=(
                "`accepted` は回答済み、`sending` は送信中、`unknown` は"
                "「届いたか判らない」という監査上の記録、`closed_cold_start` は"
                "初回ティックの既存分です。"
            ),
            details={"message_id": message_id, "status": record.status.value},
        )
    record.status = MessageStatus.SKIPPED
    if reason is not None:
        record.note = reason
    return record


def assign_question(state: WatchState, *, message_id: str, owner: str | None) -> QuestionRecord:
    """質問の担当者を記録する。`None` を渡すとチーム TODO へ戻す。

    「誰が答えるべきか」は本文の名指しなどから Claude が読み取る判断であり、
    Python は結果を保持するだけ。毎朝判定し直すと担当が日によってブレるため、
    一度決めたらここに残す(`skip` / `defer` と同じ「判断を記録する」形)。
    """
    question = state.questions.get(message_id)
    if question is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"追跡していない質問です: {message_id}",
            hint="先に `abist-kb teams questions track` を実行してください。",
        )
    question.owner = owner.strip().lower() if owner else None
    return question


class OpenTodos(BaseModel):
    """朝の提示のうち、state だけで決まる部分。"""

    model_config = ConfigDict(frozen=True)

    by_owner: dict[str, list[QuestionRecord]]
    team: list[QuestionRecord]


#: 未解決として朝に出す状態。`reminded` を含めるのが要点で、催促済みでも
#: 解決していない以上 TODO からは消さない(消すと放置が見えなくなる)。
_OPEN_FOR_TODO = frozenset(
    {QuestionStatus.OPEN, QuestionStatus.ACKNOWLEDGED, QuestionStatus.REMINDED}
)


def open_todos(state: WatchState) -> OpenTodos:
    """未解決の追跡中の質問を、担当者ごととチーム分に分けて返す。

    担当が明確なものは担当者へ、明確でないものはチーム TODO として出す。
    esa・チャット上の約束・GitHub Issue といった他の情報源は Claude 側が集める
    (この関数は state だけで決まる部分に限る)。
    """
    by_owner: dict[str, list[QuestionRecord]] = {}
    team: list[QuestionRecord] = []
    ordered = sorted(state.questions.values(), key=lambda q: (q.asked_at, q.message_id))
    for question in ordered:
        if question.status not in _OPEN_FOR_TODO:
            continue
        if question.owner is None:
            team.append(question)
        else:
            by_owner.setdefault(question.owner, []).append(question)
    return OpenTodos(by_owner=by_owner, team=team)


def _reminder_clock_start(question: QuestionRecord) -> datetime:
    """リマインド判定の起点。defer されていればその時刻から測り直す。"""
    if question.deferred_at is None:
        return question.asked_at
    return max(question.asked_at, question.deferred_at)


def defer_reminder(state: WatchState, *, message_id: str, now: datetime) -> QuestionRecord:
    """「今回は催促しないと決めた」を記録する(設計 §7.2)。

    確証が持てないまま催促するより見送るほうが害が小さい、というのが設計の方針
    だが、見送った事実を残さないと同じ質問が毎ティック再提示され、運用者が同じ
    判断を延々とやり直すことになる。ここに刻むと営業時間の時計が振り出しに戻る。

    状態(`open` / `acknowledged`)は変えない。見送りは「解決した」でも
    「催促した」でもなく、判断を先送りしただけだからである。
    """
    question = state.questions.get(message_id)
    if question is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"追跡していない質問です: {message_id}",
            hint="先に `abist-kb teams questions track` を実行してください。",
        )
    question.deferred_at = now
    return question


def due_reminders(
    state: WatchState, *, now: datetime, threshold_hours: int
) -> list[QuestionRecord]:
    """営業時間内経過が閾値に達した質問を古い順に返す。

    `resolved` / `reminded` / `stale` は対象外。1つの質問につきリマインドは1回
    なので、呼び出し側は送信後に `REMINDED` へ遷移させること。

    起点は `asked_at` ではなく `max(asked_at, deferred_at)`。`defer_reminder` で
    「今回は催促しない」と記録された質問は、そこから改めて閾値ぶんの営業時間が
    経つまで再提示しない。でなければ確証が持てない質問が毎ティック出続ける。
    """
    due = [
        question
        for question in state.questions.values()
        if question.status in _REMINDABLE
        and elapsed_business_hours(_reminder_clock_start(question), now) >= threshold_hours
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


#: `--force` 無しで再送を拒否する状態。`accepted` は既に届いている可能性が高く、
#: `unknown` は届いたかどうか判らない(設計 §6.1.1)。どちらも再送は二重投稿の
#: リスクを負う。
_REFUSE_WITHOUT_FORCE = frozenset({MessageStatus.ACCEPTED, MessageStatus.UNKNOWN})


def run_reply(
    settings: Settings,
    *,
    message_id: str,
    title: str,
    body: str,
    sources: list[str] | None,
    client: httpx.Client,
    now: datetime,
    force: bool = False,
) -> ReplyResult:
    """回答を投稿し、state を遷移させる(設計 §6.1.1)。

    POST の直前に `sending` を書き込んで保存するのが要点。ここで落ちても次
    tick の `recover_interrupted` が `unknown` へ移し、自動再送はしない
    (at-most-once)。Webhook に idempotency 機構が無いため、再送すれば同じ回答を
    二重に投稿してしまう。保存を省いて「POST してから記録する」順序にすると、
    POST 成功直後のクラッシュで state には何も残らず、次 tick が同じメッセージを
    未処理のまま再選択して二重送信する — この事故を防ぐための順序であり、
    最適化で省いてはならない。

    `record.status` が `accepted`(既に届いている)または `unknown`(届いたか
    不明)のときは `force=True` を渡さない限り拒否する。ここを素通りさせると、
    同じ message_id で `reply` を再実行しただけで同じカードが十人へ二重投稿
    される(設計 §6.1.1 が防ごうとしている事故そのもの)。
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

    if record.status in _REFUSE_WITHOUT_FORCE and not force:
        if record.status is MessageStatus.ACCEPTED:
            reason = "既に配信済みです(accepted)。再送すると同じ回答が二重に投稿されます。"
        else:
            reason = "前回の配信結果が不明です(unknown)。再送すると二重投稿の恐れがあります。"
        raise AppError(
            ErrorCode.CONFLICT,
            reason,
            hint="それでも再送する場合は `--force` を付けてください。",
            details={"message_id": message_id, "status": record.status.value},
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
