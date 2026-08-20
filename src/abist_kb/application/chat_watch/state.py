"""監視 state の永続化と遷移(設計 §6)。

`search_watermark`(検索の下限時刻を決める目印)と `messages`(応答済みかどうかの
判断材料)は別物である。混同すると検索インデックス遅延で取りこぼす。

書き込みは atomic write。素朴な `open(path, "w")` は途中で落ちると JSON 自体が
壊れて復旧不能になる。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from abist_kb.application.chat_watch.membership import classify, is_self_post
from abist_kb.domain.chat_watch import InboundMessage, MemberRole, MessageStatus, QuestionStatus
from abist_kb.domain.errors import AppError, ErrorCode

#: state 形式のバージョン。`domain.chat_watch` の列挙値を変えたら上げること。
SCHEMA_VERSION = 1


class MessageRecord(BaseModel):
    """inbound メッセージ1件の処理状態(設計 §6.1)。"""

    message_id: str
    status: MessageStatus
    sender_email: str
    created_at: datetime
    #: 監査用。どの inbound に対して何を投稿したか。安全機構ではない(設計 §6.1.1)。
    outbound_fingerprint: str | None = None
    accepted_at: datetime | None = None
    note: str | None = None


class QuestionRecord(BaseModel):
    """追跡中の質問(設計 §6.3)。"""

    message_id: str
    status: QuestionStatus
    asked_by: str
    asked_at: datetime
    reminded_at: datetime | None = None
    #: 「今回は催促しないと決めた」時刻。設計 §7.2 は、返信を見落としていないか
    #: 確証が持てなければ催促するなと定めるが、その判断を記録する場所が無いと
    #: 期限超過の質問が毎ティック再提示され続ける。ここに刻むと営業時間の時計が
    #: 振り出しに戻り、次の閾値までは対象から外れる。
    deferred_at: datetime | None = None


class BackoffState(BaseModel):
    """429 バックオフ(設計 §3.2)。"""

    interval_minutes: int = 20
    next_allowed_at: datetime | None = None


class WatchState(BaseModel):
    """`data/teams-watch-state.json` の全体。"""

    schema_version: int = SCHEMA_VERSION
    #: 初回 tick を通過したか。`messages` の空判定で代用してはならない。初回に
    #: 1件も取れなかった場合、永遠に cold start のままになる(設計 §5.3)。
    initialised: bool = False
    search_watermark: datetime | None = None
    messages: dict[str, MessageRecord] = Field(default_factory=dict)
    questions: dict[str, QuestionRecord] = Field(default_factory=dict)
    backoff: BackoffState = Field(default_factory=BackoffState)


def load_state(path: Path) -> WatchState:
    """state を読む。存在しなければ空の state を返す。

    未知の `schema_version` や壊れた JSON では起動を中止する。誤った解釈のまま
    投稿するより停止するほうが安全(設計 §6.4)。
    """
    if not path.is_file():
        return WatchState()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            f"監視 state を読めません: {path}",
            hint="ファイルが壊れています。中身を確認するか、削除して再開してください。",
            details={"cause_type": type(exc).__name__},
        ) from exc

    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            f"監視 state の schema_version が未知です: {version}",
            hint=f"このビルドが解釈できるのは {SCHEMA_VERSION} だけです。",
            details={"found": version, "expected": SCHEMA_VERSION},
        )

    try:
        return WatchState.model_validate(raw)
    except ValidationError as exc:
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            f"監視 state の内容が不正です: {path}",
            details={"cause_type": type(exc).__name__},
        ) from exc


def save_state(path: Path, state: WatchState) -> None:
    """atomic write で置換する。

    tmp へ書く → flush → fsync → `Path.replace()`。`Path.replace` は Windows でも
    既存ファイルを置換できる(`os.replace` と同じ syscall だが `PTH105` に触れ
    ない)。tmp は `path` と同じディレクトリに作るため同一ファイルシステム上の
    置換になり、`replace` の原子性が成立する。途中で落ちても既存 state は
    壊れない。
    """
    payload = state.model_dump_json(indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


#: 次 tick で処理し直してよい状態。`unknown` を含めないのが要点(設計 §6.1.1)。
_RETRYABLE = frozenset({MessageStatus.DISCOVERED, MessageStatus.PROCESSING, MessageStatus.FAILED})


def recover_interrupted(state: WatchState) -> list[str]:
    """`sending` のまま残ったエントリを `unknown` へ移す。

    前 tick が応答を受け取る前に中断したことを意味する。投稿が届いたか確認する
    手段は無い(Adaptive Card 本文は検索に掛からないことがある)。したがって
    自動再送しない。再送は人間が判断する(設計 §6.1.1)。
    """
    recovered: list[str] = []
    for record in state.messages.values():
        if record.status is MessageStatus.SENDING:
            record.status = MessageStatus.UNKNOWN
            record.note = "送信結果が不明。自動再送しない(設計 §6.1.1)"
            recovered.append(record.message_id)
    return sorted(recovered)


def ingest_messages(
    state: WatchState, messages: list[InboundMessage], *, cold_start: bool
) -> list[InboundMessage]:
    """取得したメッセージを state へ取り込み、未処理のものを返す。

    判定基準は `created_at > search_watermark` ではなく
    `message_id not in state.messages`。時刻は重複取得を許容する(設計 §5.1)。

    `cold_start` のとき(state が空の初回)はすべて `closed_cold_start` として
    記録し、何も返さない。過去ログへの一斉投稿を防ぐ(設計 §5.3)。
    """
    fresh: list[InboundMessage] = []
    for message in messages:
        if message.message_id in state.messages:
            continue
        if cold_start:
            status = MessageStatus.CLOSED_COLD_START
        elif is_self_post(message) or classify(message) is not MemberRole.ACTIVE:
            # context_only と system はここで落とす。本文は state に残らないので
            # 分類は取り込み時にしかできない。
            status = MessageStatus.SKIPPED
        else:
            status = MessageStatus.DISCOVERED
        state.messages[message.message_id] = MessageRecord(
            message_id=message.message_id,
            status=status,
            sender_email=message.sender_email,
            created_at=message.created_at,
        )
        if status is MessageStatus.DISCOVERED:
            fresh.append(message)

    if messages:
        newest = max(m.created_at for m in messages)
        if state.search_watermark is None or newest > state.search_watermark:
            state.search_watermark = newest

    return fresh


def pending_for_decision(state: WatchState, *, limit: int) -> list[MessageRecord]:
    """判断が必要なメッセージを古い順に最大 `limit` 件返す。

    上限は「1 tick で `accepted` へ遷移させる件数」であり、取得件数の上限では
    ない。溢れた分は `discovered` のまま次 tick へ持ち越す(設計 §5.2)。選ばれた
    分は `processing` へ進めるが、`processing` は `_RETRYABLE` に含まれるため
    これは「再選択を止める」効果を持たない。呼び出し側が各レコードを必ず
    `accepted`/`failed`/`skipped` のいずれかへ進めて初めて `_RETRYABLE` から
    外れる。何もせず放置すると次 tick でも同じレコードが古株として選ばれ続け、
    後続の新着メッセージがいつまでも順番待ちになる(設計 §5.2 の意図はこの
    「持ち越し」であって「無限保留」ではない)。質問でないと判定した場合は
    `abist-kb teams inbox skip` で明示的に `skipped` へ進めること。

    返すのは `MessageRecord`(ID と送信者)であって `InboundMessage` ではない。
    本文を必要とするのは Claude 側であり、state は本文を保持しない。
    """
    candidates = [record for record in state.messages.values() if record.status in _RETRYABLE]
    candidates.sort(key=lambda r: (r.created_at, r.message_id))
    selected = candidates[:limit]
    for record in selected:
        record.status = MessageStatus.PROCESSING
    return selected


def prune(state: WatchState, *, now: datetime, retention_days: int) -> int:
    """保持期間を過ぎたメッセージを削除する。

    通常の forward-only 運用では overlap(30分)より遥かに古いため、削除済み ID が
    再取得されて二重投稿になることはない。過去へ遡る手段(`--since` 等)を将来
    足す場合は、既定 dry-run のガードを併せて実装すること(設計 §6.2)。

    追跡中の質問は削除しない。`stale` にしてリマインドだけ止める。
    """
    cutoff = now - timedelta(days=retention_days)
    stale_ids = [mid for mid, record in state.messages.items() if record.created_at < cutoff]
    for message_id in stale_ids:
        del state.messages[message_id]

    for question in state.questions.values():
        if question.asked_at < cutoff and question.status in {
            QuestionStatus.OPEN,
            QuestionStatus.ACKNOWLEDGED,
            QuestionStatus.REMINDED,
        }:
            question.status = QuestionStatus.STALE

    return len(stale_ids)


NORMAL_INTERVAL_MINUTES = 20
MAX_INTERVAL_MINUTES = 240


def apply_rate_limit(
    state: WatchState, *, now: datetime, retry_after_seconds: int | None
) -> datetime:
    """429 を受けたときの次回実行可能時刻を決める(設計 §3.2)。

    `Retry-After` があればそれに従う(このとき間隔は据え置く)。無ければ間隔を
    倍にして上限 240分でとめる。
    """
    if retry_after_seconds is not None:
        next_at = now + timedelta(seconds=retry_after_seconds)
    else:
        doubled = min(state.backoff.interval_minutes * 2, MAX_INTERVAL_MINUTES)
        state.backoff.interval_minutes = doubled
        next_at = now + timedelta(minutes=doubled)

    state.backoff.next_allowed_at = next_at
    return next_at


def clear_rate_limit(state: WatchState) -> None:
    """成功したら通常間隔へ戻す。"""
    state.backoff.interval_minutes = NORMAL_INTERVAL_MINUTES
    state.backoff.next_allowed_at = None


def is_allowed(state: WatchState, *, now: datetime) -> bool:
    """バックオフ中でなければ True。"""
    next_at = state.backoff.next_allowed_at
    return next_at is None or now >= next_at
