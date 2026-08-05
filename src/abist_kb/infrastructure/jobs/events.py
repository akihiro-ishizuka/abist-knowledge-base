"""進捗イベントの永続化とインプロセス購読(設計書 §10.3)。

`job_events` への追記と、CLI がライブ描画するためのインプロセス購読は独立した
関心事である。`ProgressEvent` の形はどちらでも共通(将来の Web の SSE、MCP の
`job_status` も同じ形を参照する)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from abist_kb.domain.job import ProgressEvent, Severity
from abist_kb.infrastructure.db.connection import transaction

Subscriber = Callable[[ProgressEvent], None]


def append_event(conn: sqlite3.Connection, event: ProgressEvent) -> int:
    """`job_events` へ追記する。戻り値はそのジョブ内での連番(`seq`)。"""
    with transaction(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE job_id = ?",
            (event.job_id,),
        ).fetchone()
        seq = int(row[0])
        conn.execute(
            "INSERT INTO job_events "
            "(job_id, seq, phase, current, total, message, severity, item, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.job_id,
                seq,
                event.phase,
                event.current,
                event.total,
                event.message,
                event.severity.value,
                event.item,
                event.timestamp.isoformat(),
            ),
        )
    return seq


def list_events(conn: sqlite3.Connection, job_id: str) -> list[ProgressEvent]:
    """あるジョブの `job_events` を `seq` 昇順で返す(履歴/`jobs show` 用)。"""
    rows = conn.execute(
        "SELECT * FROM job_events WHERE job_id = ? ORDER BY seq", (job_id,)
    ).fetchall()
    return [
        ProgressEvent(
            job_id=row["job_id"],
            phase=row["phase"],
            current=row["current"],
            total=row["total"],
            message=row["message"] or "",
            severity=Severity(row["severity"]),
            item=row["item"],
            timestamp=datetime.fromisoformat(row["created_at"]),
        )
        for row in rows
    ]


class EventBus:
    """プロセス内購読(§10.3): CLI がライブ描画するための即時通知。

    `job_events` への永続化とは独立しており、購読者が居なくても
    `append_event` は常に成功する。API(SSE)/MCP(`job_status`)は
    `job_events` を読むため、このバスに登録しなくても最新状態を参照できる。
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def publish(self, event: ProgressEvent) -> None:
        for callback in list(self._subscribers):
            callback(event)


__all__ = ["EventBus", "Subscriber", "append_event", "list_events"]
