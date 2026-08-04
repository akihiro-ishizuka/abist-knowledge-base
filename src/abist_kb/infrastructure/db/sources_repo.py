"""`sources` テーブルの永続化(設計書 §9.2)。

`type`/`display_name`/`connection`(接続設定JSON)/`output_dir`/`enabled` を持つ。
`batches`/`batch_items` はこのテーブルへ参照(`source_id`)を持ち、M3 Task 3〜5 の
ソースアダプターは接続情報をここから読む。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.connection import transaction

_UPDATABLE_FIELDS: tuple[str, ...] = ("type", "display_name", "connection", "output_dir", "enabled")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["connection"] = json.loads(d["connection"]) if d["connection"] else {}
    d["enabled"] = bool(d["enabled"])
    return d


class SourceRepository:
    """`sources` テーブルの CRUD。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(
        self,
        *,
        type: str,
        display_name: str,
        connection: dict[str, Any] | None = None,
        output_dir: str,
        enabled: bool = True,
        id: str | None = None,
    ) -> dict[str, Any]:
        source_id = id or str(uuid4())
        now = _now_iso()
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO sources "
                "(id, type, display_name, connection, output_dir, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    type,
                    display_name,
                    json.dumps(connection or {}, ensure_ascii=False),
                    output_dir,
                    int(enabled),
                    now,
                    now,
                ),
            )
        result = self.get(source_id)
        assert result is not None
        return result

    def get(self, source_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return None if row is None else _row_to_dict(row)

    def list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM sources ORDER BY display_name").fetchall()
        return [_row_to_dict(row) for row in rows]

    def update(self, source_id: str, **fields: Any) -> dict[str, Any]:
        """部分更新。指定しなかった列は既存値を保持する。"""
        existing = self.get(source_id)
        if existing is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"ソースが見つかりません: {source_id}")
        updates = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}
        if not updates:
            return existing
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params: list[Any] = []
        for key, value in updates.items():
            if key == "connection":
                params.append(json.dumps(value or {}, ensure_ascii=False))
            elif key == "enabled":
                params.append(int(value))
            else:
                params.append(value)
        params.append(_now_iso())
        params.append(source_id)
        with transaction(self._conn):
            self._conn.execute(
                f"UPDATE sources SET {set_clause}, updated_at = ? WHERE id = ?", params
            )
        result = self.get(source_id)
        assert result is not None
        return result

    def delete(self, source_id: str) -> bool:
        with transaction(self._conn):
            cur = self._conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return cur.rowcount > 0


__all__ = ["SourceRepository"]
