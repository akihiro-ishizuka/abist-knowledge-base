"""`BatchService`(設計書 §9.2, §10, §11.2): バッチの CRUD・旧設定の一方向インポート・実行。

**バッチは app.sqlite が正であり、旧 `batch-config.js` へは書き戻さない。**
`import_from_old_config` は `migration.batch_config_parser`(JavaScript を実行しない
リテラル限定パーサ)を使って読み取るだけの一方向インポートである。

**`run` は `infrastructure.jobs.builtin_registry` の共有 `batch` ハンドラ経由で
`SyncService.sync_batch` を実行する**(`docs-write` リソースリースで全プロセス横断に
直列化、§10.2)。UI detach (`{"batch_id"}`) と MCP `start_run_batch` (`{"batch"}`)
も同じハンドラを使う。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from abist_kb.config import Settings, load_settings
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import Job, ResourceKind
from abist_kb.domain.metadata_schema import safe_batch_name
from abist_kb.infrastructure.db import audit
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.migration.batch_config_parser import parse_batch_config

ConfirmFn = Callable[[str], bool]

#: 旧 `batch-config.js` の web/git エントリのキー(JS由来の camelCase)を、
#: `batch_items.options` の正準キー(snake_case、`application.sync_service` が
#: 読む形)へ変換する対応表。旧設定に無いキー(`max_pages`/`max_size_bytes`/
#: `concurrency` 等、本システムで新規追加したクロール上限)は変換対象に無いため
#: そのまま欠落し、`infrastructure.sources.web`/`sync_service` 側の既定値が使われる。
#: 対応表に無いキーはそのまま(名前を変えず)保持する(旧設定にしか無い未知の
#: フィールドを黙って捨てない)。
_WEB_OPTION_KEY_MAP: dict[str, str] = {
    "url": "url",
    "outputDir": "output_dir",
    "maxDepth": "max_depth",
    "delay": "delay",
}


#: 後方互換のエイリアス。実ハンドラは `build_builtin_handlers` が組み立てる。
#: リソース要求だけは settings 非依存なので静的に公開する。
BUILTIN_BATCH_RESOURCES: dict[str, tuple[ResourceKind, str | None]] = {
    "batch": (ResourceKind.DOCS_WRITE, None),
    "kb_download_batch": (ResourceKind.DOCS_WRITE, None),
}


def _with_docs_prefix(directory: str) -> str:
    normalized = directory.replace("\\", "/").rstrip("/")
    if normalized == "docs" or normalized.startswith("docs/"):
        return normalized
    return f"docs/{normalized}"


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "state": str(job.state),
        "params": job.params,
        "result": job.result,
        "error": job.error,
    }


class BatchService:
    """`BatchRepository` を包み、確認・監査・一方向インポート・実行を提供する。"""

    def __init__(self, conn: sqlite3.Connection, *, settings: Settings | None = None) -> None:
        self._conn = conn
        self._repo = BatchRepository(conn)
        self._settings = settings

    # -- CRUD -------------------------------------------------------------

    def add(
        self,
        *,
        name: str,
        type: str,
        output_dir: str | None = None,
        enabled: bool = True,
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if type == "git":
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=(
                    "Git 同期は削除されました。リポジトリは git / gh で docs/ に置いてから "
                    "document register-disk --apply で登録してください。"
                ),
            )
        return self._repo.create(
            name=name, type=type, output_dir=output_dir, enabled=enabled, items=items
        )

    def list(self) -> list[dict[str, Any]]:
        return self._repo.list()

    def list_by_name(self, name: str) -> dict[str, Any]:
        found = self._repo.get_by_name(name)
        if found is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"バッチが見つかりません: {name}")
        return found

    def show(self, batch_id: str) -> dict[str, Any]:
        batch = self._repo.get(batch_id)
        if batch is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"バッチが見つかりません: {batch_id}")
        return batch

    def edit(self, batch_id: str, **fields: Any) -> dict[str, Any]:
        self.show(batch_id)  # NOT_FOUND を先に出す
        return self._repo.update(batch_id, **fields)

    def remove(self, batch_id: str, *, confirm: ConfirmFn, actor: str | None = None) -> bool:
        existing = self.show(batch_id)  # NOT_FOUND を先に出す
        if not confirm(f"バッチ '{existing['name']}' を削除しますか?"):
            return False
        deleted = self._repo.delete(batch_id)
        if deleted:
            audit.record_event(
                self._conn,
                action="batch.delete",
                target_type="batch",
                target_id=batch_id,
                details={"name": existing["name"]},
                actor=actor,
            )
        return deleted

    # -- 旧 batch-config.js からの一方向インポート ----------------------------

    def import_from_old_config(self, config_path: Path) -> dict[str, Any]:
        """旧 `batch-config.js` を読み取り、各エントリを新バッチとして作成する。

        **旧ファイルへは一切書き込まない**(`migration.batch_config_parser` の
        モジュール docstring 参照。JavaScript を実行せず、リテラルのみ解釈する)。
        既に同名バッチが存在する場合は上書きせず `AppError` で失敗する
        (意図しない一括上書きを防ぐため、個別に `edit`/`remove` で調整させる)。
        """
        text = Path(config_path).read_text(encoding="utf-8")
        config = parse_batch_config(text)

        imported: list[str] = []
        for name, entry in config.items():
            if self._repo.get_by_name(name) is not None:
                raise AppError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"同名のバッチが既に存在するためインポートできません: {name}",
                )
            if isinstance(entry, list):
                self.add(
                    name=name,
                    type="esa",
                    output_dir=f"docs/{safe_batch_name(name)}",
                    items=[{"target": category} for category in entry],
                )
            elif isinstance(entry, dict) and entry.get("type") == "web":
                output_dir = entry.get("outputDir") or safe_batch_name(name)
                options = {
                    _WEB_OPTION_KEY_MAP.get(key, key): value
                    for key, value in entry.items()
                    if key != "type"
                }
                self.add(
                    name=name,
                    type="web",
                    output_dir=_with_docs_prefix(output_dir),
                    items=[{"options": options}],
                )
            elif isinstance(entry, dict) and entry.get("type") == "git":
                continue
            else:
                raise AppError(
                    code=ErrorCode.UNSUPPORTED_BATCH_CONFIG,
                    message=f"未知のバッチ形式です: {name}",
                )
            imported.append(name)
        return {"imported": len(imported), "names": imported}

    # -- 実行 -------------------------------------------------------------

    def run(
        self,
        batch_id: str,
        *,
        owner_id: str | None = None,
        settings: Settings | None = None,
    ) -> dict[str, Any]:
        """バッチの実行ジョブを投入し、完了まで同期実行する(CLI 既定の契約)。

        実同期は共有レジストリの `batch` ハンドラ(`SyncService.sync_batch`)が行う。
        """
        self.show(batch_id)  # NOT_FOUND を先に出す

        # ローカル import: `application.job_service` は `infrastructure.jobs` へ
        # 依存するため、他サービスから常時 import すると循環しやすい箇所を避ける。
        from abist_kb.application.job_service import JobService
        from abist_kb.infrastructure.jobs.builtin_registry import (
            build_builtin_handlers,
            build_builtin_resources,
        )

        effective = settings or self._settings
        if effective is None:
            effective = load_settings()

        handlers = build_builtin_handlers(settings=effective, conn=self._conn)
        job_service = JobService(
            self._conn,
            owner_id=owner_id or str(uuid.uuid4()),
            handlers=handlers,
            resource_for_kind=build_builtin_resources(),
        )
        job = job_service.run_inline("batch", {"batch_id": batch_id})
        return _job_to_dict(job)


#: 後方互換: 実ハンドラは settings+conn が必要なため、モジュール定数は空。
#: 呼び出し側は `BatchService.run` または `build_builtin_handlers` を使うこと。
BUILTIN_BATCH_HANDLERS: dict[str, Any] = {}


__all__ = [
    "BUILTIN_BATCH_HANDLERS",
    "BUILTIN_BATCH_RESOURCES",
    "BatchService",
    "ConfirmFn",
]
