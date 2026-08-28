"""`visualizations` / `visualization_sources` の読み書き。

**このテーブルは索引であり正本ではない。** 正本は
`reports/visualizations/<id>/manifest.json` で、DB を消してもディスクから
完全に再構築できる（`application.visualization.catalog.reconcile_from_disk`）。
したがってここでは manifest 本文を持たず、`manifest_sha256` だけを保持して
ドリフト検知に使う。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from abist_kb.infrastructure.db.connection import transaction

_COLUMNS = (
    "id",
    "job_id",
    "state",
    "code",
    "schema_version",
    "scene_kind",
    "template",
    "output_format",
    "title",
    "query",
    "output_dir",
    "manifest_path",
    "manifest_sha256",
    "output_path",
    "output_sha256",
    "output_size_bytes",
    "duration_ms",
    "warnings",
    "source",
    "created_at",
    "updated_at",
)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    # sqlite3.Row は Mapping ではないので `in row` は使えない(SIM118 は不適用)。
    record = {key: row[key] for key in row.keys()}  # noqa: SIM118
    raw = record.get("warnings")
    record["warnings"] = json.loads(raw) if raw else []
    return record


class VisualizationRepository:
    """可視化カタログのリポジトリ。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, record: dict[str, Any], sources: list[dict[str, Any]]) -> None:
        """1件を挿入または更新する（子テーブルは総入れ替え）。"""
        values = {key: record.get(key) for key in _COLUMNS}
        values["warnings"] = json.dumps(record.get("warnings") or [], ensure_ascii=False)
        placeholders = ", ".join(f":{key}" for key in _COLUMNS)
        updates = ", ".join(f"{key} = excluded.{key}" for key in _COLUMNS if key != "id")
        with transaction(self._conn):
            self._conn.execute(
                f"INSERT INTO visualizations ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                values,
            )
            self._conn.execute(
                "DELETE FROM visualization_sources WHERE visualization_id = ?", (record["id"],)
            )
            self._conn.executemany(
                "INSERT INTO visualization_sources "
                "(visualization_id, source_id, path, start_line, end_line, content_hash, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record["id"],
                        src["source_id"],
                        src["path"],
                        src.get("start_line"),
                        src.get("end_line"),
                        src.get("content_hash"),
                        src.get("status") or "unknown",
                    )
                    for src in sources
                ],
            )

    def get(self, visualization_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM visualizations WHERE id = ?", (visualization_id,)
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_sources(self, visualization_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT source_id, path, start_line, end_line, content_hash, status "
            "FROM visualization_sources WHERE visualization_id = ? ORDER BY source_id",
            (visualization_id,),
        ).fetchall()
        return [
            {key: row[key] for key in row.keys()}  # noqa: SIM118
            for row in rows
        ]

    def list(
        self,
        *,
        state: str | None = None,
        scene_kind: str | None = None,
        output_format: str | None = None,
        query: str | None = None,
        source_path: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """新しい順に一覧する。戻り値は `(ページ, 総件数)`。"""
        where: list[str] = []
        params: list[Any] = []
        if state:
            where.append("v.state = ?")
            params.append(state)
        if scene_kind:
            where.append("v.scene_kind = ?")
            params.append(scene_kind)
        if output_format:
            where.append("v.output_format = ?")
            params.append(output_format)
        if query:
            where.append("(v.title LIKE ? OR IFNULL(v.query, '') LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        if source_path:
            where.append(
                "EXISTS (SELECT 1 FROM visualization_sources s "
                "WHERE s.visualization_id = v.id AND s.path LIKE ?)"
            )
            params.append(f"%{source_path}%")
        clause = f" WHERE {' AND '.join(where)}" if where else ""

        total = self._conn.execute(
            f"SELECT COUNT(*) FROM visualizations v{clause}", params
        ).fetchone()[0]
        rows = self._conn.execute(
            f"SELECT v.* FROM visualizations v{clause} "
            "ORDER BY v.created_at DESC, v.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_dict(row) for row in rows], int(total)

    def all_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT id FROM visualizations").fetchall()
        return {row["id"] for row in rows}

    def delete(self, visualization_id: str) -> bool:
        with transaction(self._conn):
            cursor = self._conn.execute(
                "DELETE FROM visualizations WHERE id = ?", (visualization_id,)
            )
        return cursor.rowcount > 0

    def source_counts(self, visualization_id: str) -> tuple[int, int]:
        """`(出典数, 不良だった出典数)` を返す。一覧の軽量表示に使う。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status NOT IN ('ok', 'unknown') THEN 1 ELSE 0 END) AS bad "
            "FROM visualization_sources WHERE visualization_id = ?",
            (visualization_id,),
        ).fetchone()
        return int(row["total"] or 0), int(row["bad"] or 0)


__all__ = ["VisualizationRepository"]
