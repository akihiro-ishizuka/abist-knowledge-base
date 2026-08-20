"""監視 state の永続化と遷移(設計 §6)。

`search_watermark`(検索の下限時刻を決める目印)と `messages`(応答済みかどうかの
判断材料)は別物である。混同すると検索インデックス遅延で取りこぼす。

書き込みは atomic write。素朴な `open(path, "w")` は途中で落ちると JSON 自体が
壊れて復旧不能になる。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from abist_kb.domain.chat_watch import MessageStatus, QuestionStatus
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


def utcnow() -> datetime:
    """テストで差し替えやすいよう1箇所に閉じる。"""
    return datetime.now(UTC)
