"""`batches`/`batch_items` テーブルの永続化(設計書 §9.2)。

**バッチは app.sqlite が正であり、旧 `batch-config.js` へは書き戻さない。**
一方向 import(旧ファイルを読んでここへ取り込む)は `application.batch_service`
+ `migration.batch_config_parser` の役目。ここは新スキーマ上の CRUD のみを担う。

`items` はバッチ内の個別対象を順序(`position`)付きで持つ。esa バッチは
カテゴリ文字列1件につき1行(`target`)、web/git バッチは対象1件につき1行
(`source_id`/`options` に URL・リポジトリ等を持たせ、`target` は使わない)。
`update(..., items=...)` は渡された場合のみ既存 `batch_items` を全置換する
(渡さなければ既存の items は変更しない)。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.connection import transaction

_UPDATABLE_BATCH_FIELDS: tuple[str, ...] = ("name", "type", "output_dir", "enabled")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _item_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["options"] = json.loads(d["options"]) if d["options"] else {}
    return d


class BatchRepository:
    """`batches`/`batch_items` テーブルの CRUD。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- 作成 -------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        type: str,
        output_dir: str | None = None,
        enabled: bool = True,
        items: list[dict[str, Any]] | None = None,
        id: str | None = None,
    ) -> dict[str, Any]:
        batch_id = id or str(uuid4())
        now = _now_iso()
        try:
            with transaction(self._conn):
                self._conn.execute(
                    "INSERT INTO batches "
                    "(id, name, type, output_dir, enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (batch_id, name, type, output_dir, int(enabled), now, now),
                )
                self._insert_items(batch_id, items or [])
        except sqlite3.IntegrityError as exc:
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"バッチ名は既に使われています: {name}",
            ) from exc
        result = self.get(batch_id)
        assert result is not None
        return result

    def _insert_items(self, batch_id: str, items: list[dict[str, Any]]) -> None:
        now = _now_iso()
        for position, item in enumerate(items):
            self._conn.execute(
                "INSERT INTO batch_items "
                "(id, batch_id, source_id, position, target, options, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.get("id") or str(uuid4()),
                    batch_id,
                    item.get("source_id"),
                    position,
                    item.get("target"),
                    json.dumps(item["options"], ensure_ascii=False)
                    if item.get("options")
                    else None,
                    now,
                    now,
                ),
            )

    # -- 参照 -------------------------------------------------------------

    def get(self, batch_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        return self._hydrate(row)

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM batches WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        return self._hydrate(row)

    def _hydrate(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        item_rows = self._conn.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position", (d["id"],)
        ).fetchall()
        d["items"] = [_item_to_dict(item) for item in item_rows]
        return d

    def list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM batches ORDER BY name").fetchall()
        return [self._hydrate(row) for row in rows]

    # -- 更新・削除 ---------------------------------------------------------

    def update(
        self,
        batch_id: str,
        *,
        items: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """部分更新。`items` を渡した場合のみ既存 `batch_items` を全置換する。"""
        existing = self.get(batch_id)
        if existing is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"バッチが見つかりません: {batch_id}")

        updates = {k: v for k, v in fields.items() if k in _UPDATABLE_BATCH_FIELDS}
        try:
            with transaction(self._conn):
                if updates:
                    set_clause = ", ".join(f"{k} = ?" for k in updates)
                    params = [int(v) if k == "enabled" else v for k, v in updates.items()]
                    params.append(_now_iso())
                    params.append(batch_id)
                    self._conn.execute(
                        f"UPDATE batches SET {set_clause}, updated_at = ? WHERE id = ?", params
                    )
                else:
                    self._conn.execute(
                        "UPDATE batches SET updated_at = ? WHERE id = ?",
                        (_now_iso(), batch_id),
                    )
                if items is not None:
                    self._conn.execute("DELETE FROM batch_items WHERE batch_id = ?", (batch_id,))
                    self._insert_items(batch_id, items)
        except sqlite3.IntegrityError as exc:
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"バッチ名は既に使われています: {updates.get('name')}",
            ) from exc
        result = self.get(batch_id)
        assert result is not None
        return result

    def delete(self, batch_id: str) -> bool:
        with transaction(self._conn):
            self._conn.execute("DELETE FROM batch_items WHERE batch_id = ?", (batch_id,))
            cur = self._conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
        return cur.rowcount > 0


__all__ = ["BatchRepository"]
