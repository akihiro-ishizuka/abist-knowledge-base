"""`DocumentService`(設計書 §9.2, §12): 一覧・取得・メタデータ更新・安全削除。

削除は破壊的操作のため、対象を確認してから実行し、`audit_events` へ記録する
(§12「文書削除、バッチ削除、強制同期は監査イベントへ記録する」)。確認方法
(対話確認/`--yes`)は呼び出し側(CLI の `Presenter.confirm`)の責務であり、
このサービスは `confirm` コールバック(prompt: str -> bool)を受け取るだけで
Presenter に依存しない(TUI/Web からも同じサービスを再利用できるようにするため)。

**一覧・件数は「不在=非存在」を意味しない**(`tests/fixtures/PROVENANCE.md` §4)。
旧 `sync-state.sqlite` は参照コーパス等を既定除外した1回限りのバックフィル・
スナップショットであり、`documents` テーブルに無い実ファイルが多数存在する
ことが実測で確認されている。`list`/`count_by_source` の結果をそのまま
「実ファイルの完全な一覧」として扱ってはならない。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db import audit
from abist_kb.infrastructure.db.documents_repo import DocumentRepository

ConfirmFn = Callable[[str], bool]


class DocumentService:
    """`DocumentRepository` を包み、確認・監査を伴う操作を提供する。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._repo = DocumentRepository(conn)

    # -- 参照 -------------------------------------------------------------

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
        return self._repo.list(
            source=source,
            sync_status=sync_status,
            status=status,
            path_prefix=path_prefix,
            category_prefix=category_prefix,
            managed_only=managed_only,
        )

    def count_by_source(self) -> dict[str, int]:
        return self._repo.count_by_source()

    def get_or_none(self, path: str) -> dict[str, Any] | None:
        return self._repo.get(path)

    def get(self, path: str) -> dict[str, Any]:
        doc = self._repo.get(path)
        if doc is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"文書が見つかりません: {path}")
        return doc

    # -- 書込 -------------------------------------------------------------

    def upsert(self, record: dict[str, Any]) -> None:
        self._repo.upsert(record)

    def update_metadata(self, path: str, fields: dict[str, Any]) -> dict[str, Any]:
        """メタデータの部分更新。`path` は変更しない(部分upsertは repo が保証)。"""
        self.get(path)  # 存在確認(NOT_FOUND を先に出す)
        self._repo.upsert({"path": path, **fields})
        return self.get(path)

    # -- 破壊的操作 ---------------------------------------------------------

    def delete(
        self,
        path: str,
        *,
        confirm: ConfirmFn,
        actor: str | None = None,
    ) -> bool:
        """文書を削除する。確認されなければ何もせず `False` を返す。

        対象パスを事前に提示してから `confirm` を呼ぶのは呼び出し側(CLI)の責務
        (`confirm` の prompt 文字列にパスを含める)。ここでは存在確認→確認→
        削除→監査記録の順序だけを保証する。
        """
        existing = self.get(path)  # NOT_FOUND を先に出す
        if not confirm(f"文書 '{existing['path']}' を削除しますか?"):
            return False
        deleted = self._repo.delete(path)
        if deleted:
            audit.record_event(
                self._conn,
                action="document.delete",
                target_type="document",
                target_id=existing["path"],
                details={"source": existing.get("source")},
                actor=actor,
            )
        return deleted


__all__ = ["ConfirmFn", "DocumentService"]
