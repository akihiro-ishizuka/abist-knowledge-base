"""esa 同期のオーケストレーション(設計書 §10.2, §11.2, task-3-brief)。

`docs-write` リソースリースの下で実行し、ジョブ基盤(`infrastructure.jobs`)経由の
進捗通知・レポート出力までを一括して面倒を見る。HTTP通信・front matter生成・
差分判定・欠落判定は `infrastructure.sources.esa` に委ね、ここでは「1つの
source/batch/全batchを1回同期する」というユースケースの組み立てだけを持つ。

**レポートは旧 `sync-report.js` と互換な形式**(`reports/sync/sync-esa-<label>-
<timestamp>.json`)で書き出す。`ACTION_BUCKET`(`domain.sync_policy`)を経由した
`totals`/`actionCounts`/`items` の3点セットは旧実装のフィールド名
(camelCase)をそのまま踏襲する — 旧レポートを読む人間・ツールが変わらず
使えるようにするため。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.job import ResourceKind
from abist_kb.domain.sync_policy import ACTION_BUCKET, SyncAction
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.sources_repo import SourceRepository
from abist_kb.infrastructure.jobs.supervisor import JobRunContext
from abist_kb.infrastructure.sources.esa import (
    DEFAULT_MISSING_THRESHOLD,
    EsaClient,
    EsaSyncRunner,
    SyncItem,
    category_search_queries,
)

#: `JobService(resource_for_kind=...)` へそのまま渡せる既定リソース要求。
#: 設計書 §10.2 の「バッチ・sync を全プロセス横断で直列化する」に従い、
#: すべての esa 同期ジョブは `docs-write` の単一区画を取り合う。
BUILTIN_SYNC_RESOURCES: dict[str, tuple[ResourceKind, str | None]] = {
    "sync": (ResourceKind.DOCS_WRITE, None),
}


@dataclass(slots=True)
class SyncSummary:
    """旧 `newSyncSummary`(`sync-planner.js`)と同じ形の同期サマリ。"""

    source: str
    started_at: str
    finished_at: str | None = None
    full_sync_succeeded: bool | None = None
    totals: dict[str, int] = field(
        default_factory=lambda: {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "conflict": 0,
            "missing": 0,
            "error": 0,
        }
    )
    action_counts: dict[str, int] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    def to_report_dict(self) -> dict[str, Any]:
        """旧レポート形式(camelCase キー)。"""
        return {
            "source": self.source,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "fullSyncSucceeded": self.full_sync_succeeded,
            "totals": self.totals,
            "actionCounts": self.action_counts,
            "items": self.items,
            "options": self.options,
            "note": self.note,
        }


def new_sync_summary(source: str) -> SyncSummary:
    return SyncSummary(source=source, started_at=datetime.now(UTC).isoformat())


def _item_payload(item: SyncItem | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, SyncItem):
        return {
            "path": item.path,
            "filePath": item.file_path,
            "postNumber": item.post_number,
            "title": item.title,
            "action": item.action,
            "reason": item.reason,
            **({"error": item.error} if item.error else {}),
            **({"expectedPath": item.expected_path} if item.expected_path else {}),
        }
    return dict(item)


def record_sync_result(summary: SyncSummary, item: SyncItem | Mapping[str, Any]) -> SyncSummary:
    """判定結果をサマリへ足す(旧 `recordSyncResult` と同じ集計規則)。"""
    payload = _item_payload(item)
    action = payload.get("action")
    try:
        bucket = ACTION_BUCKET[SyncAction(action)] if action else None
    except ValueError:
        bucket = None
    if bucket is None:
        raise AppError(code=ErrorCode.INVALID_INPUT, message=f"未知の同期アクションです: {action}")
    summary.totals[bucket] = summary.totals.get(bucket, 0) + 1
    summary.action_counts[action] = summary.action_counts.get(action, 0) + 1
    summary.items.append(payload)
    return summary


def _sanitize_label(label: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\s]', "-", str(label))
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned[:80] or "sync"


def write_sync_report(summary: SyncSummary, *, reports_dir: Path, label: str) -> Path:
    """`reports/sync/sync-<source>-<label>-<timestamp>.json` へ書き出す。"""
    sync_dir = reports_dir / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    summary.finished_at = summary.finished_at or datetime.now(UTC).isoformat()
    stamp = re.sub(r"[:.]", "-", summary.finished_at)
    file_path = sync_dir / f"sync-{summary.source}-{_sanitize_label(label)}-{stamp}.json"
    file_path.write_text(
        json.dumps(summary.to_report_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return file_path


def _with_docs_prefix(directory: str) -> str:
    normalized = directory.replace("\\", "/").rstrip("/")
    if normalized == "docs" or normalized.startswith("docs/"):
        return normalized
    return f"docs/{normalized}"


class SyncService:
    """esa ソース・バッチの同期を実行する(設計書 §10.2)。"""

    def __init__(
        self,
        *,
        root_dir: Path,
        docs_dir: Path,
        reports_dir: Path,
        documents: DocumentRepository,
        sources: SourceRepository,
        batches: BatchRepository,
        missing_threshold: int = DEFAULT_MISSING_THRESHOLD,
    ) -> None:
        self._root_dir = root_dir
        self._docs_dir = docs_dir
        self._reports_dir = reports_dir
        self._documents = documents
        self._sources = sources
        self._batches = batches
        self._missing_threshold = missing_threshold

    # -- ソース単位の同期 -----------------------------------------------------

    def _require_esa_source(self, source_id: str) -> dict[str, Any]:
        source = self._sources.get(source_id)
        if source is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"ソースが見つかりません: {source_id}")
        if source["type"] != "esa":
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"esa 以外のソースは sync source では扱えません: {source['type']}",
                exit_code=ExitCode.INVALID_INPUT,
            )
        connection = source.get("connection") or {}
        team = connection.get("team")
        token = connection.get("access_token")
        if not team or not token:
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message="ソースの接続設定に team/access_token が不足しています。",
                exit_code=ExitCode.INVALID_INPUT,
            )
        return source

    async def _sync_categories(
        self,
        *,
        client: EsaClient,
        runner: EsaSyncRunner,
        categories: list[str],
        summary: SyncSummary,
        prune_orphans: bool,
        emit: Any = None,
    ) -> None:
        # ここに到達するまでに一覧取得(search_posts)が例外を投げていれば
        # 呼び出し元(ジョブハンドラ)まで伝播して同期全体が失敗として終わる
        # (旧実装の「一覧取得の失敗は握りつぶさない」契約)。したがってこの関数が
        # 正常に完了した時点では、処理した全カテゴリの一覧取得は成功している。
        summary.full_sync_succeeded = True
        for category in categories:
            posts: list[dict[str, Any]] = []
            # 一覧取得(search_posts)の失敗は例外として上へ伝播させ、握りつぶさない
            # (旧実装のコメント通り: 欠落判定の前提が崩れるため)。
            for query in category_search_queries(category):
                found = await client.search_posts(query)
                if found:
                    posts = found
                    break

            if not posts:
                # 0件でも「カテゴリごと消えた」とは判定しない(設計原則5)。
                continue

            for index, post in enumerate(posts):
                item = runner.save_post(post)
                record_sync_result(summary, item)
                if emit is not None:
                    emit(
                        phase="sync-esa",
                        current=index + 1,
                        total=len(posts),
                        message=f"{category}: {item.action}",
                        item=item.path or item.file_path,
                    )

            missing_items = await runner.detect_missing_posts(
                all_posts=posts,
                category_path=category,
                full_sync_succeeded=True,
                prune_orphans=prune_orphans,
                fetch_post=client.get_post,
            )
            for missing_item in missing_items:
                record_sync_result(summary, missing_item)

    async def _run_source_sync(
        self,
        source: Mapping[str, Any],
        *,
        categories: list[str],
        force: bool,
        dry_run: bool,
        prune_orphans: bool,
        emit: Any = None,
    ) -> SyncSummary:
        connection = source.get("connection") or {}
        summary = new_sync_summary("esa")
        summary.options = {
            "outputDir": source["output_dir"],
            "categories": categories,
            "force": force,
            "dryRun": dry_run,
            "pruneOrphans": prune_orphans,
        }
        runner = EsaSyncRunner(
            documents=self._documents,
            root_dir=self._root_dir,
            docs_dir=self._docs_dir,
            output_dir=source["output_dir"],
            force=force,
            dry_run=dry_run,
            missing_threshold=self._missing_threshold,
        )
        # `base_url` は接続設定の任意キー(既定は本物の esa.io API)。テストが
        # ローカルのモックサーバーへ向けるためだけに使う抜け道であり、通常運用の
        # ソース登録では設定しない。
        async with EsaClient(
            team=connection["team"],
            access_token=connection["access_token"],
            base_url=connection.get("base_url"),
        ) as client:
            await self._sync_categories(
                client=client,
                runner=runner,
                categories=categories,
                summary=summary,
                prune_orphans=prune_orphans and not dry_run,
                emit=emit,
            )
        summary.finished_at = datetime.now(UTC).isoformat()
        return summary

    def sync_source(
        self,
        source_id: str,
        *,
        categories: list[str],
        force: bool = False,
        dry_run: bool = False,
        prune_orphans: bool = False,
        emit: Any = None,
    ) -> tuple[SyncSummary, Path | None]:
        """1つの esa ソースを指定カテゴリで同期する。"""
        source = self._require_esa_source(source_id)
        summary = asyncio.run(
            self._run_source_sync(
                source,
                categories=categories,
                force=force,
                dry_run=dry_run,
                prune_orphans=prune_orphans,
                emit=emit,
            )
        )
        if dry_run:
            return summary, None
        label = categories[0] if len(categories) == 1 else source["display_name"]
        report_path = write_sync_report(summary, reports_dir=self._reports_dir, label=label)
        return summary, report_path

    # -- バッチ単位の同期 -----------------------------------------------------

    def _resolve_batch_source_id(self, batch: Mapping[str, Any]) -> str:
        item_source_ids = {
            item.get("source_id") for item in batch.get("items", []) if item.get("source_id")
        }
        if len(item_source_ids) == 1:
            return next(iter(item_source_ids))
        esa_sources = [s for s in self._sources.list() if s["type"] == "esa" and s["enabled"]]
        if len(esa_sources) == 1:
            return esa_sources[0]["id"]
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message="バッチの対象に紐づく esa ソースを一意に決められません。",
            hint=(
                "`batch edit --items` で各対象に source_id を指定するか、"
                "有効な esa ソースを1件だけにしてください。"
            ),
            exit_code=ExitCode.INVALID_INPUT,
        )

    def sync_batch(
        self,
        batch_id: str,
        *,
        force: bool = False,
        dry_run: bool = False,
        prune_orphans: bool = False,
        emit: Any = None,
    ) -> tuple[SyncSummary, Path | None]:
        """バッチに登録された全カテゴリを同期する。"""
        batch = self._batches.get(batch_id)
        if batch is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"バッチが見つかりません: {batch_id}")
        if batch["type"] != "esa":
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"esa 以外のバッチは sync batch では扱えません: {batch['type']}",
                exit_code=ExitCode.INVALID_INPUT,
            )
        categories = [item["target"] for item in batch["items"] if item.get("target")]
        source_id = self._resolve_batch_source_id(batch)
        source = self._require_esa_source(source_id)
        output_dir = batch.get("output_dir") or _with_docs_prefix(source["output_dir"])
        source_for_sync = dict(source)
        source_for_sync["output_dir"] = output_dir

        summary = asyncio.run(
            self._run_source_sync(
                source_for_sync,
                categories=categories,
                force=force,
                dry_run=dry_run,
                prune_orphans=prune_orphans,
                emit=emit,
            )
        )
        if dry_run:
            return summary, None
        report_path = write_sync_report(summary, reports_dir=self._reports_dir, label=batch["name"])
        return summary, report_path

    def sync_all(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        prune_orphans: bool = False,
        emit: Any = None,
    ) -> list[dict[str, Any]]:
        """有効な esa バッチをすべて同期する。"""
        results: list[dict[str, Any]] = []
        for batch in self._batches.list():
            if batch["type"] != "esa" or not batch["enabled"]:
                continue
            summary, report_path = self.sync_batch(
                batch["id"], force=force, dry_run=dry_run, prune_orphans=prune_orphans, emit=emit
            )
            results.append(
                {
                    "batch_id": batch["id"],
                    "batch_name": batch["name"],
                    "summary": summary.to_report_dict(),
                    "report_path": str(report_path) if report_path else None,
                }
            )
        return results


# --------------------------------------------------------------------------
# ジョブ基盤への配線(`docs-write` リースの下で実行する、brief Step 4)
# --------------------------------------------------------------------------


def _build_sync_service(
    conn: Any, *, root_dir: Path, docs_dir: Path, reports_dir: Path, missing_threshold: int
) -> SyncService:
    return SyncService(
        root_dir=root_dir,
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        documents=DocumentRepository(conn),
        sources=SourceRepository(conn),
        batches=BatchRepository(conn),
        missing_threshold=missing_threshold,
    )


def make_sync_job_handler(
    *,
    conn: Any,
    root_dir: Path,
    docs_dir: Path,
    reports_dir: Path,
    missing_threshold: int,
    outbox: dict[str, Any],
) -> Any:
    """`JobService(handlers={"sync": ...})` へ渡すハンドラを組み立てる。

    ジョブの `params` は `{"target": "source"|"batch"|"all", "id": str | None,
    "categories": list[str] | None, "force": bool, "dry_run": bool,
    "prune_orphans": bool}` を受け取る。実行はここで `docs-write` リース配下
    (`infrastructure.jobs.execution.run_job` 経由)に閉じ込められる。

    ジョブ基盤(`infrastructure.jobs`)は現時点でハンドラから `jobs.result` 列へ
    値を書き戻す経路を持たない(`run_job` の成功分岐は `repo.finish(..., state=
    SUCCEEDED)` を引数無しで呼ぶのみ)。インライン実行は同一プロセス・同一
    スレッドで完結するため、呼び出し側が渡す `outbox` 辞書へ直接書き込むことで
    サマリ・レポートパスを持ち帰る(ジョブ基盤自体には手を入れない)。
    """

    def handler(run: JobRunContext) -> None:
        params = run.job.params
        target = params.get("target", "source")
        service = _build_sync_service(
            conn,
            root_dir=root_dir,
            docs_dir=docs_dir,
            reports_dir=reports_dir,
            missing_threshold=missing_threshold,
        )
        force = bool(params.get("force", False))
        dry_run = bool(params.get("dry_run", False))
        prune_orphans = bool(params.get("prune_orphans", False))

        if target == "source":
            summary, report_path = service.sync_source(
                params["id"],
                categories=params.get("categories") or [],
                force=force,
                dry_run=dry_run,
                prune_orphans=prune_orphans,
                emit=run.emit,
            )
            outbox["summary"] = summary.to_report_dict()
            outbox["report_path"] = str(report_path) if report_path else None
            run.emit(
                phase="sync-esa",
                current=len(summary.items),
                total=len(summary.items),
                message="同期完了",
            )
        elif target == "batch":
            summary, report_path = service.sync_batch(
                params["id"],
                force=force,
                dry_run=dry_run,
                prune_orphans=prune_orphans,
                emit=run.emit,
            )
            outbox["summary"] = summary.to_report_dict()
            outbox["report_path"] = str(report_path) if report_path else None
        elif target == "all":
            results = service.sync_all(
                force=force, dry_run=dry_run, prune_orphans=prune_orphans, emit=run.emit
            )
            outbox["results"] = results
            run.emit(
                phase="sync-esa",
                current=len(results),
                total=len(results),
                message="全バッチ同期完了",
            )
        else:
            raise AppError(code=ErrorCode.INVALID_INPUT, message=f"未知の同期対象です: {target}")

    return handler


def run_sync_inline(
    conn: Any,
    *,
    root_dir: Path,
    docs_dir: Path,
    reports_dir: Path,
    missing_threshold: int = DEFAULT_MISSING_THRESHOLD,
    target: str,
    target_id: str | None = None,
    categories: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    prune_orphans: bool = False,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """CLI 既定の同期実行: `docs-write` リースの下でジョブ経由に実行する。"""
    # ローカル import: `application.job_service` は `infrastructure.jobs` へ
    # 依存するため、他サービスから常時 import すると循環しやすい箇所を避ける。
    from abist_kb.application.job_service import JobService

    outbox: dict[str, Any] = {}
    handler = make_sync_job_handler(
        conn=conn,
        root_dir=root_dir,
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        missing_threshold=missing_threshold,
        outbox=outbox,
    )
    job_service = JobService(
        conn,
        owner_id=owner_id or str(uuid.uuid4()),
        handlers={"sync": handler},
        resource_for_kind=BUILTIN_SYNC_RESOURCES,
    )
    job = job_service.run_inline(
        "sync",
        {
            "target": target,
            "id": target_id,
            "categories": categories,
            "force": force,
            "dry_run": dry_run,
            "prune_orphans": prune_orphans,
        },
    )
    return {
        "id": job.id,
        "kind": job.kind,
        "state": str(job.state),
        "params": job.params,
        "error": job.error,
        **outbox,
    }


__all__ = [
    "BUILTIN_SYNC_RESOURCES",
    "SyncService",
    "SyncSummary",
    "make_sync_job_handler",
    "new_sync_summary",
    "record_sync_result",
    "run_sync_inline",
    "write_sync_report",
]
