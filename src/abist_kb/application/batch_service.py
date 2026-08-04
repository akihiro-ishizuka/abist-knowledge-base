"""`BatchService`(設計書 §9.2, §10, §11.2): バッチの CRUD・旧設定の一方向インポート・実行。

**バッチは app.sqlite が正であり、旧 `batch-config.js` へは書き戻さない。**
`import_from_old_config` は `migration.batch_config_parser`(JavaScript を実行しない
リテラル限定パーサ)を使って読み取るだけの一方向インポートである。

**`run` は M3 Task 3〜5(esa/web/git ソースアダプター)がまだ実装されていない
現時点では、実際の同期処理を行わない。** `JobService` 経由でジョブ種別 `"batch"`
を投入する配線だけをここで用意し(§10.2: バッチ・sync は `docs-write` リソース
リースで全プロセス横断に直列化する)、既定のハンドラは「同期処理は後続タスクで
実装される」旨を記録して即座に成功として終了する。Task 3〜5 が実ハンドラを
`BUILTIN_HANDLERS["batch"]` へ差し替える想定。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import Job, ResourceKind
from abist_kb.domain.metadata_schema import safe_batch_name
from abist_kb.infrastructure.db import audit
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.infrastructure.jobs.supervisor import JobRunContext
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
_GIT_OPTION_KEY_MAP: dict[str, str] = {
    "repository": "repository",
    "branch": "branch",
    "outputDir": "output_dir",
}


def _batch_run_handler(run: JobRunContext) -> None:
    run.emit(
        phase="batch-run",
        current=1,
        total=1,
        message=(
            "バッチの実同期処理は M3 Task 3〜5(esa/web/git ソースアダプター)で"
            "実装されます。このジョブはジョブ基盤の配線確認のみを行いました。"
        ),
    )


#: `JobService(handlers=...)` へそのまま渡せる既定ハンドラ。
BUILTIN_BATCH_HANDLERS: dict[str, Any] = {"batch": _batch_run_handler}
#: `JobService(resource_for_kind=...)` へそのまま渡せる既定リソース要求。
BUILTIN_BATCH_RESOURCES: dict[str, tuple[ResourceKind, str | None]] = {
    "batch": (ResourceKind.DOCS_WRITE, None)
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

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._repo = BatchRepository(conn)

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
            elif isinstance(entry, dict) and entry.get("type") in ("web", "git"):
                output_dir = entry.get("outputDir") or safe_batch_name(name)
                key_map = _WEB_OPTION_KEY_MAP if entry["type"] == "web" else _GIT_OPTION_KEY_MAP
                options = {
                    key_map.get(key, key): value for key, value in entry.items() if key != "type"
                }
                self.add(
                    name=name,
                    type=entry["type"],
                    output_dir=_with_docs_prefix(output_dir),
                    items=[{"options": options}],
                )
            else:
                raise AppError(
                    code=ErrorCode.UNSUPPORTED_BATCH_CONFIG,
                    message=f"未知のバッチ形式です: {name}",
                )
            imported.append(name)
        return {"imported": len(imported), "names": imported}

    # -- 実行 -------------------------------------------------------------

    def run(self, batch_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """バッチの実行ジョブを投入し、完了まで同期実行する(CLI 既定の契約)。

        実同期処理は M3 Task 3〜5 が `BUILTIN_BATCH_HANDLERS["batch"]` を
        差し替えるまでの間、モジュール docstring の既定ハンドラが応答する。
        """
        self.show(batch_id)  # NOT_FOUND を先に出す

        # ローカル import: `application.job_service` は `infrastructure.jobs` へ
        # 依存するため、他サービスから常時 import すると循環しやすい箇所を避ける。
        from abist_kb.application.job_service import JobService

        job_service = JobService(
            self._conn,
            owner_id=owner_id or str(uuid.uuid4()),
            handlers=BUILTIN_BATCH_HANDLERS,
            resource_for_kind=BUILTIN_BATCH_RESOURCES,
        )
        job = job_service.run_inline("batch", {"batch_id": batch_id})
        return _job_to_dict(job)


__all__ = [
    "BUILTIN_BATCH_HANDLERS",
    "BUILTIN_BATCH_RESOURCES",
    "BatchService",
    "ConfirmFn",
]
