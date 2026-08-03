"""破壊的操作の監査証跡(設計書 §12)。

「文書削除、バッチ削除、強制同期は監査イベントへ記録する」を満たす最小限の
書込経路。M7 の `audit_runs`/`audit_findings`(監査スキャンの実行履歴・指摘)とは
別物で、こちらは個々の破壊的操作を1件ずつ追記する台帳(`audit_events`)であり、
参照・集計クエリの整備は M7 で行う想定(このタスクでは記録のみを保証する)。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from abist_kb.infrastructure.db.connection import transaction


def record_event(
    conn: sqlite3.Connection,
    *,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None = None,
    actor: str | None = None,
) -> None:
    """`audit_events` へ1件追記する。"""
    with transaction(conn):
        conn.execute(
            "INSERT INTO audit_events "
            "(id, occurred_at, action, target_type, target_id, details, actor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                datetime.now(UTC).isoformat(),
                action,
                target_type,
                target_id,
                json.dumps(details, ensure_ascii=False) if details is not None else None,
                actor,
            ),
        )


__all__ = ["record_event"]
