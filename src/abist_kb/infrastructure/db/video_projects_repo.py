"""`video_projects` / `video_project_inputs` の読み書き。

`visualizations_repo` と同じ流儀。**このテーブルは索引であり正本ではない。**
正本は `reports/videos/<id>/project-spec.json` で、DB を消しても
`application/video/catalog.reconcile_from_disk` で再構築できる。
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
    "title",
    "purpose",
    "language",
    "aspect_ratio",
    "classification",
    "public_candidate",
    "project_dir",
    "spec_path",
    "spec_sha256",
    "output_path",
    "output_sha256",
    "output_size_bytes",
    "duration_sec",
    "scene_count",
    "warnings",
    "source",
    "created_at",
    "updated_at",
)

_INPUT_COLUMNS = (
    "video_id",
    "path",
    "content_hash",
    "selection",
    "require_usage",
    "origin_type",
    "origin_selector",
    "esa_url",
    "esa_post_id",
    "used",
)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    # sqlite3.Row は Mapping ではないので `in row` は使えない(SIM118 は不適用)。
    record = {key: row[key] for key in row.keys()}  # noqa: SIM118
    raw = record.get("warnings")
    record["warnings"] = json.loads(raw) if raw else []
    if "public_candidate" in record:
        record["public_candidate"] = bool(record["public_candidate"])
    return record


class VideoProjectRepository:
    """動画カタログのリポジトリ。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, record: dict[str, Any], inputs: list[dict[str, Any]]) -> None:
        values = {key: record.get(key) for key in _COLUMNS}
        values["warnings"] = json.dumps(record.get("warnings") or [], ensure_ascii=False)
        values["public_candidate"] = 1 if record.get("public_candidate") else 0
        placeholders = ", ".join(f":{key}" for key in _COLUMNS)
        updates = ", ".join(f"{key} = excluded.{key}" for key in _COLUMNS if key != "id")
        with transaction(self._conn):
            self._conn.execute(
                f"INSERT INTO video_projects ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                values,
            )
            self._conn.execute(
                "DELETE FROM video_project_inputs WHERE video_id = ?", (record["id"],)
            )
            self._conn.executemany(
                f"INSERT INTO video_project_inputs ({', '.join(_INPUT_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _INPUT_COLUMNS)})",
                [
                    (
                        record["id"],
                        item["path"],
                        item.get("content_hash"),
                        item["selection"],
                        1 if item.get("require_usage") else 0,
                        item["origin_type"],
                        item.get("origin_selector"),
                        item.get("esa_url"),
                        item.get("esa_post_id"),
                        None if item.get("used") is None else (1 if item["used"] else 0),
                    )
                    for item in inputs
                ],
            )

    def get(self, video_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM video_projects WHERE id = ?", (video_id,)
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_inputs(self, video_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM video_project_inputs WHERE video_id = ? ORDER BY path",
            (video_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}  # noqa: SIM118
            item["require_usage"] = bool(item["require_usage"])
            if item.get("used") is not None:
                item["used"] = bool(item["used"])
            result.append(item)
        return result

    def list(
        self,
        *,
        state: str | None = None,
        classification: str | None = None,
        query: str | None = None,
        source_path: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if state:
            where.append("v.state = ?")
            params.append(state)
        if classification:
            where.append("v.classification = ?")
            params.append(classification)
        if query:
            where.append("(v.title LIKE ? OR IFNULL(v.purpose, '') LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        if source_path:
            where.append(
                "EXISTS (SELECT 1 FROM video_project_inputs i "
                "WHERE i.video_id = v.id AND i.path LIKE ?)"
            )
            params.append(f"%{source_path}%")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM video_projects v{clause}", params
        ).fetchone()[0]
        rows = self._conn.execute(
            f"SELECT v.* FROM video_projects v{clause} "
            "ORDER BY v.created_at DESC, v.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_dict(r) for r in rows], int(total)

    def all_ids(self) -> set[str]:
        return {r["id"] for r in self._conn.execute("SELECT id FROM video_projects").fetchall()}

    def delete(self, video_id: str) -> bool:
        with transaction(self._conn):
            cursor = self._conn.execute("DELETE FROM video_projects WHERE id = ?", (video_id,))
        return cursor.rowcount > 0

    def mark_used(self, video_id: str, used_paths: set[str]) -> None:
        """本編で使われた入力に印を付ける（QA が呼ぶ）。"""
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE video_project_inputs SET used = 0 WHERE video_id = ?", (video_id,)
            )
            if used_paths:
                marks = ", ".join("?" for _ in used_paths)
                self._conn.execute(
                    f"UPDATE video_project_inputs SET used = 1 "
                    f"WHERE video_id = ? AND path IN ({marks})",
                    [video_id, *sorted(used_paths)],
                )


__all__ = ["VideoProjectRepository"]
