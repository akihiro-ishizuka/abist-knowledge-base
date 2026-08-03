"""`documents` テーブルの永続化(設計書 §9.2、`tests/fixtures/PROVENANCE.md` §4)。

旧 `tools/lib/sync-state.js`(`SyncState` クラス)の移植。**このテーブルの列は
旧実装の27列を1つも欠落・改名させずに全保持し、`uuid`(新規の安定識別子)と
`source_id`(新設 `sources` への参照)を追加する。** M8 の移行はこのテーブルへ
旧 `sync-state.sqlite` を列単位で import するため、列の改名・欠落はここで
気付かなければ移行時まで表面化しない。

**`tests/fixtures/PROVENANCE.md` §4 が `design/system-design.md` §9.2 を上書きする
実測事実に注意。** 旧 `sync-state.sqlite` は履歴的な同期台帳ではなく、
`backfill-metadata.js` の `defaultExclude`(`knowledge/B32doc` 等の参照コーパスを
既定除外)による1回限りのバックフィル・スナップショットである。したがって
**このテーブルに行が無いことは、対応する実ファイルが存在しないことの証拠にはならない**
(実測で `docs/knowledge/catiadoc` の1,313ファイルと `docs/knowledge/generated` の
4ファイルがどちらのDBにも登録されていないメタデータ孤児であることが判明している)。
`list_documents`/`count_by_source` 等の呼び出し側は、この不在をファイル不在と
混同してはならない。

継承する2つの意味論(旧実装 `upsertDocument`/`SyncState` と同一):

- **部分upsert**: レコードに含まれない列は既存値を保持する。`WRITABLE_COLUMNS`
  に無いキーは無視する(例外にしない)。旧ダウンローダーはこれに依存して
  `last_checked_at` だけを更新し、ハッシュ等の他の列を巻き戻さない。
- **パス正規化**: Windows区切り(`\\`)を POSIX(`/`)へ正規化してから主キーとして
  使う。同じ文書が区切り文字違いで2行に分裂しないようにする。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.metadata_schema import to_posix_path
from abist_kb.infrastructure.db.connection import transaction

#: 旧 `sync-state.js` の27列(順序も維持) + `uuid` + `source_id`。
ALL_COLUMNS: tuple[str, ...] = (
    "path",
    "uuid",
    "source_id",
    "source",
    "managed_by",
    "document_type",
    "status",
    "title",
    "url",
    "post_number",
    "category",
    "source_key",
    "source_updated_at",
    "sync_status",
    "source_content_hash",
    "local_content_hash",
    "downloaded_at",
    "last_checked_at",
    "etag",
    "last_modified",
    "missing_count",
    "missing_since",
    "sync_error",
    "indexed_at",
    "embedding_model",
    "embedding_dimensions",
    "embedding_hash",
    "created_at",
    "updated_at",
)

#: upsert で受け付ける列(`path`/`created_at`/`updated_at` を除く残り26列)。
#: これ以外のキーは黙って無視する(旧実装 `WRITABLE_COLUMNS` と同じ契約)。
WRITABLE_COLUMNS: tuple[str, ...] = tuple(
    c for c in ALL_COLUMNS if c not in {"path", "created_at", "updated_at"}
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(value: Any) -> Any:
    """SQLite が扱える型へ寄せる(bool/list/dict をそのまま渡すと落ちるため)。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class DocumentRepository:
    """`documents` テーブルの読み書き。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- 書込 -------------------------------------------------------------

    def upsert(self, record: dict[str, Any]) -> None:
        """文書レコードを作成または部分更新する。

        `record` に含まれない列は既存値を保持する(段階的に情報を足せるようにする
        ため)。`WRITABLE_COLUMNS` に無いキーは無視する。
        """
        raw_path = record.get("path")
        if not raw_path:
            raise AppError(code=ErrorCode.INVALID_INPUT, message="upsert: path は必須です")
        path = to_posix_path(raw_path)

        fields = [c for c in WRITABLE_COLUMNS if c in record]
        values = {c: _normalize(record[c]) for c in fields}

        now = _now_iso()
        with transaction(self._conn):
            existing = self._conn.execute(
                "SELECT path FROM documents WHERE path = ?", (path,)
            ).fetchone()
            if existing is None:
                insert_fields = list(fields)
                if "uuid" not in values:
                    values["uuid"] = str(uuid4())
                    insert_fields.append("uuid")
                cols = ["path", *insert_fields, "created_at", "updated_at"]
                placeholders = ", ".join("?" for _ in cols)
                params = [path, *(values[c] for c in insert_fields), now, now]
                self._conn.execute(
                    f"INSERT INTO documents ({', '.join(cols)}) VALUES ({placeholders})",
                    params,
                )
                return

            if not fields:
                self._conn.execute(
                    "UPDATE documents SET updated_at = ? WHERE path = ?", (now, path)
                )
                return

            set_clause = ", ".join(f"{c} = ?" for c in fields)
            params = [*(values[c] for c in fields), now, path]
            self._conn.execute(
                f"UPDATE documents SET {set_clause}, updated_at = ? WHERE path = ?", params
            )

    # -- 参照 -------------------------------------------------------------

    def get(self, path: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE path = ?", (to_posix_path(path),)
        ).fetchone()
        return None if row is None else dict(row)

    def list(
        self,
        *,
        source: str | None = None,
        sync_status: str | None = None,
        status: str | None = None,
        path_prefix: str | None = None,
        category_prefix: str | None = None,
        managed_only: bool = False,
    ) -> list[dict[str, Any]]:
        """フィルタ付き一覧。

        **不在をファイル不在と解釈しないこと**(モジュール docstring 参照)。
        旧 sync-state.sqlite は参照コーパス等を既定除外した1回限りのバックフィル・
        スナップショットであり、このテーブルに無い行が実ファイルとして存在する
        ことは実測で確認されている。
        """
        where: list[str] = []
        params: list[Any] = []
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if sync_status is not None:
            where.append("sync_status = ?")
            params.append(sync_status)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if managed_only:
            where.append("managed_by IS NOT NULL AND managed_by <> 'human'")
        if path_prefix:
            prefix = to_posix_path(path_prefix).rstrip("/")
            where.append("path LIKE ?")
            params.append(f"{prefix}/%")
        if category_prefix:
            prefix = str(category_prefix).rstrip("/")
            where.append("(category = ? OR category LIKE ?)")
            params.append(prefix)
            params.append(f"{prefix}/%")

        sql = "SELECT * FROM documents"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY path"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_by_source(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT source, COUNT(*) AS n FROM documents GROUP BY source ORDER BY source"
        ).fetchall()
        return {(row["source"] or "(none)"): row["n"] for row in rows}

    # -- 欠落追跡(§設計原則5: 削除誤判定の防止。sync_status は変えない) --------

    def mark_missing(self, path: str, *, at: str | None = None) -> int:
        """取得元一覧に見つからなかった回数を1増やす。`sync_status` は変えない。"""
        p = to_posix_path(path)
        at_value = at or _now_iso()
        now = _now_iso()
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE documents SET missing_count = missing_count + 1, "
                "missing_since = COALESCE(missing_since, ?), updated_at = ? WHERE path = ?",
                (at_value, now, p),
            )
            row = self._conn.execute(
                "SELECT missing_count FROM documents WHERE path = ?", (p,)
            ).fetchone()
        return row["missing_count"] if row is not None else 0

    def clear_missing(self, path: str) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE documents SET missing_count = 0, missing_since = NULL, "
                "updated_at = ? WHERE path = ?",
                (_now_iso(), to_posix_path(path)),
            )

    # -- 削除 -------------------------------------------------------------

    def delete(self, path: str) -> bool:
        with transaction(self._conn):
            cur = self._conn.execute("DELETE FROM documents WHERE path = ?", (to_posix_path(path),))
        return cur.rowcount > 0


__all__ = ["ALL_COLUMNS", "WRITABLE_COLUMNS", "DocumentRepository"]
