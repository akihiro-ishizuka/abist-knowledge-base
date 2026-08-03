"""ジョブ・リース・進捗イベントのドメイン型(設計書 §10)。

`JobState`/`ResourceKind`/`ProgressEvent`/`Job` はどの層(CLI/TUI/Web/MCP)からも
同じ形で参照する。永続化(`infrastructure/jobs`)や提示(`presentation`)固有の
関心事はここに混ぜない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class JobState(StrEnum):
    """ジョブの状態(設計書 §10 冒頭)。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATES: frozenset[JobState] = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.PARTIAL,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.INTERRUPTED,
    }
)
"""これ以上状態遷移しない終端状態。"""

RETRYABLE_STATES: frozenset[JobState] = frozenset({JobState.FAILED, JobState.INTERRUPTED})
"""`jobs retry` で再投入を許す状態(設計書 §10.3: 利用者確認後のみ)。"""


class ResourceKind(StrEnum):
    """排他制御の対象となるリソース種別(設計書 §10.2)。"""

    DOCS_WRITE = "docs-write"
    CORPUS_WRITE = "corpus-write"
    RENDER = "render"


def resource_key(kind: ResourceKind, key: str | None = None) -> str:
    """`resource_leases.resource_key` の一意な文字列表現を組み立てる。

    `corpus-write` はコーパスごとに独立した排他区画を持つため `key`(コーパス名)を
    付与する。`docs-write`/`render` は全プロセス横断で単一の区画のため `key` は使わない。
    """
    return kind.value if key is None else f"{kind.value}:{key}"


class Severity(StrEnum):
    """`ProgressEvent.severity`。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """進捗イベント(設計書 §10.3)。

    `job_events` への永続化とインプロセス購読(CLI/TUIのライブ描画)の両方で
    同じ形を使う。将来の Web(SSE)/MCP(`job_status`)もこの形をそのまま参照する
    ため、提示層固有の概念(色・記号等)をここに混ぜない。
    """

    job_id: str
    phase: str
    current: int | None = None
    total: int | None = None
    message: str = ""
    severity: Severity = Severity.INFO
    item: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class Job:
    """`jobs` テーブル1行の型付き表現。"""

    id: str
    kind: str
    state: JobState
    params: dict[str, Any]
    created_at: datetime
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    cancel_requested: bool = False
    retry_of: str | None = None
    owner_id: str | None = None
    resource_key: str | None = None
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


__all__ = [
    "RETRYABLE_STATES",
    "TERMINAL_STATES",
    "Job",
    "JobState",
    "ProgressEvent",
    "ResourceKind",
    "Severity",
    "resource_key",
]
