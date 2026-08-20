"""連絡チャット監視の値オブジェクト(設計 §4, §6)。

ここの列挙値は `data/teams-watch-state.json` へそのまま永続化される。値を変える
と既存 state が読めなくなるため、変更時は `schema_version` を上げること。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class MessageStatus(StrEnum):
    """inbound メッセージの処理状態(設計 §6.1)。"""

    DISCOVERED = "discovered"
    PROCESSING = "processing"
    SENDING = "sending"
    ACCEPTED = "accepted"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"
    CLOSED_COLD_START = "closed_cold_start"


class QuestionStatus(StrEnum):
    """追跡中の質問の状態(設計 §6.3)。

    `acknowledged`(「確認します」)と `resolved`(具体的な回答)を区別する。
    「誰かが発言した」で `resolved` にしてはならない。
    """

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    REMINDED = "reminded"
    STALE = "stale"


class MemberRole(StrEnum):
    """送信者の役割(設計 §4)。

    `context_only` は「除外」ではない。発言は文脈として読むが応答トリガーには
    しない。コード側で捨てられないよう役割としてモデル化している。
    """

    ACTIVE = "active"
    CONTEXT_ONLY = "context_only"
    SYSTEM = "system"


class InboundMessage(BaseModel):
    """Teams から取得した1件のメッセージ。"""

    model_config = ConfigDict(frozen=True)

    message_id: str
    chat_id: str
    sender_email: str
    sender_name: str
    body: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("sender_email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()
