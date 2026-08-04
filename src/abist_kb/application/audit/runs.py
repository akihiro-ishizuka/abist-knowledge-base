"""`audit_runs`/`audit_findings` への記録(設計書 §9.2, M7 task-2)。

品質監査4種(verify-integrity / find-duplicates / check-contradictions /
backfill-metadata)はすべてここを通して実行履歴と指摘を記録する。
`infrastructure.db.audit.record_event`(`audit_events`)とは別テーブルであり、
そちらは文書削除等の破壊的操作1件ずつの監査証跡、こちらは監査スキャン自体の
実行履歴・指摘を保持する。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from abist_kb.infrastructure.db.connection import transaction


@dataclass(frozen=True, slots=True)
class Finding:
    """1件の指摘。`details` は JSON 化可能な dict であること。"""

    finding_type: str
    path: str | None = None
    details: dict[str, Any] | None = None


def start_run(
    conn: sqlite3.Connection,
    *,
    audit_type: str,
    mode: str = "report",
    params: dict[str, Any] | None = None,
) -> str:
    """監査実行を1件開始し `run_id` を返す。"""
    run_id = str(uuid4())
    with transaction(conn):
        conn.execute(
            "INSERT INTO audit_runs (id, audit_type, mode, started_at, status, params) "
            "VALUES (?, ?, ?, ?, 'running', ?)",
            (
                run_id,
                audit_type,
                mode,
                datetime.now(UTC).isoformat(),
                json.dumps(params, ensure_ascii=False) if params is not None else None,
            ),
        )
    return run_id


def record_findings(conn: sqlite3.Connection, run_id: str, findings: Iterable[Finding]) -> int:
    """指摘をまとめて記録する。記録した件数を返す。"""
    now = datetime.now(UTC).isoformat()
    count = 0
    with transaction(conn):
        for finding in findings:
            conn.execute(
                "INSERT INTO audit_findings (id, run_id, finding_type, path, details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    run_id,
                    finding.finding_type,
                    finding.path,
                    json.dumps(finding.details, ensure_ascii=False)
                    if finding.details is not None
                    else None,
                    now,
                ),
            )
            count += 1
    return count


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str = "completed",
    totals: dict[str, Any] | None = None,
    report_path: str | None = None,
) -> None:
    """監査実行を終了する。"""
    with transaction(conn):
        conn.execute(
            "UPDATE audit_runs SET finished_at = ?, status = ?, totals = ?, report_path = ? "
            "WHERE id = ?",
            (
                datetime.now(UTC).isoformat(),
                status,
                json.dumps(totals, ensure_ascii=False) if totals is not None else None,
                report_path,
                run_id,
            ),
        )


__all__ = ["Finding", "finish_run", "record_findings", "start_run"]
