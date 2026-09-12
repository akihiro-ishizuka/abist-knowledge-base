"""組み込みジョブ種別のハンドラ/リソース要求レジストリ。

CLI `worker run`・`BatchService.run` が同じ語彙
(`noop`/`batch`/`kb_download_*`/`render_scene`)を実行できるようにする。
MCP `start_*`(`presentation.mcp.jobs_tools._JOB_KIND_FOR_TOOL`)がキューへ
投入する `kind` と、facade/`actions.batch_run` が使う `"batch"` をここで揃える。

**`batch` と `kb_download_batch` の語彙統一**: API/MCP admin は `{"batch_id": ...}`、
MCP `start_run_batch` は `{"batch": name}` を渡す。どちらも同一ハンドラが
解決して `SyncService.sync_batch` を呼ぶ(旧スタブの「M3 Task 3〜5」メッセージは
使わない)。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.application.sync_service import (
    SyncService,
    new_sync_summary,
    record_sync_result,
    write_sync_report,
)
from abist_kb.application.video.render_job import (
    BUILTIN_VIDEO_HANDLERS,
    BUILTIN_VIDEO_RESOURCES,
)
from abist_kb.application.visualization.render_job import (
    BUILTIN_RENDER_HANDLERS,
    BUILTIN_RENDER_RESOURCES,
)
from abist_kb.config import Settings
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.job import JobState, ResourceKind, ResourceRequirement
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.sources_repo import SourceRepository
from abist_kb.infrastructure.jobs.supervisor import JobHandler, JobRunContext
from abist_kb.infrastructure.sources.base import with_docs_prefix
from abist_kb.infrastructure.sources.esa import EsaClient, EsaSyncRunner

#: MCP `start_*` が投入するジョブ種別(`jobs_tools._JOB_KIND_FOR_TOOL` と一致)。
#: `render_scene` は `BUILTIN_RENDER_*` 側で登録するためここには含めない。
MCP_DOWNLOAD_JOB_KINDS: tuple[str, ...] = (
    "kb_download_batch",
    "kb_download_esa_post",
    "kb_download_esa_category",
    "kb_download_esa_search",
    "kb_download_web",
)

_DOCS_WRITE: ResourceRequirement = (ResourceKind.DOCS_WRITE, None)


def _noop_handler(run: JobRunContext) -> None:
    run.emit(phase="noop", current=1, total=1, message="ノーオペレーション完了")


def _build_sync_service(settings: Settings, conn: sqlite3.Connection) -> SyncService:
    return SyncService(
        root_dir=settings.root_dir,
        docs_dir=settings.docs_dir,  # type: ignore[arg-type]
        reports_dir=settings.reports_dir,  # type: ignore[arg-type]
        documents=DocumentRepository(conn),
        sources=SourceRepository(conn),
        batches=BatchRepository(conn),
        missing_threshold=settings.missing_threshold,
        settings=settings,
    )


def _resolve_batch_id(conn: sqlite3.Connection, params: dict[str, Any]) -> str:
    """UI (`batch_id`) と MCP (`batch` 名) のどちらの params 形でもバッチ ID を返す。"""
    batch_id = params.get("batch_id")
    if isinstance(batch_id, str) and batch_id.strip():
        return batch_id.strip()

    name = params.get("batch")
    if isinstance(name, str) and name.strip():
        found = BatchRepository(conn).get_by_name(name.strip())
        if found is None:
            raise AppError(
                code=ErrorCode.NOT_FOUND,
                message=f"バッチが見つかりません: {name}",
            )
        return found["id"]

    raise AppError(
        code=ErrorCode.INVALID_INPUT,
        message="バッチ実行には params.batch_id または params.batch が必要です。",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _finish_sync(
    run: JobRunContext,
    summary: Any,
    report_path: Path | None,
    *,
    phase: str,
) -> None:
    """同期サマリをジョブ結果へ反映する。致命失敗は AppError で failed にする。"""
    result: dict[str, Any] = {
        "summary": summary.to_report_dict(),
        "report_path": str(report_path) if report_path else None,
    }
    if summary.full_sync_succeeded is False:
        raise AppError(
            code=ErrorCode.FAILURE,
            message=summary.note or "同期に失敗しました。",
            details=result,
        )
    errors = [item for item in summary.items if item.get("action") == "error"]
    if errors:
        run.finish_as(
            JobState.PARTIAL,
            result=result,
            error={
                "code": "PARTIAL",
                "message": f"同期が完了しましたが {len(errors)} 件のエラーがあります。",
            },
        )
        run.emit(phase=phase, current=1, total=1, message="同期が部分失敗で終了しました")
        return

    run.finish_as(JobState.SUCCEEDED, result=result)
    run.emit(phase=phase, current=1, total=1, message="同期完了")


def _esa_client_kwargs(settings: Settings) -> dict[str, str | None]:
    """esa 資格情報。Settings(`.env`/`ABIST_KB_*`)を優先し、旧 MCP 互換の
    `ESA_TEAM_NAME`/`ESA_ACCESS_TOKEN` へフォールバックする。
    """
    team = settings.esa_team_name or os.environ.get("ESA_TEAM_NAME")
    token = settings.esa_access_token or os.environ.get("ESA_ACCESS_TOKEN")
    if not team or not token:
        raise AppError(
            code=ErrorCode.CONFIG_ERROR,
            message=(
                "esa の接続情報が不足しています。環境変数 "
                "ABIST_KB_ESA_TEAM_NAME / ABIST_KB_ESA_ACCESS_TOKEN "
                "(または ESA_TEAM_NAME / ESA_ACCESS_TOKEN) を設定してください。"
            ),
            exit_code=ExitCode.CONFIG_ERROR,
        )
    base_url = os.environ.get("ESA_BASE_URL")
    return {"team": team, "access_token": token, "base_url": base_url}


def _make_batch_handler(settings: Settings, conn: sqlite3.Connection) -> JobHandler:
    def handler(run: JobRunContext) -> None:
        batch_id = _resolve_batch_id(conn, run.job.params)
        run.check_lease()
        service = _build_sync_service(settings, conn)
        summary, report_path = service.sync_batch(
            batch_id,
            emit=run.emit,
            check_lease=run.check_lease,
        )
        _finish_sync(run, summary, report_path, phase="batch-run")

    return handler


def _make_esa_post_handler(settings: Settings, conn: sqlite3.Connection) -> JobHandler:
    def handler(run: JobRunContext) -> None:
        params = run.job.params
        post_number = int(params["post"])
        output_dir = with_docs_prefix(params.get("outputDir") or "docs")
        creds = _esa_client_kwargs(settings)
        run.check_lease()

        async def _run() -> Any:
            summary = new_sync_summary("esa")
            summary.options = {"outputDir": output_dir, "post": post_number}
            runner = EsaSyncRunner(
                documents=DocumentRepository(conn),
                root_dir=settings.root_dir,
                docs_dir=settings.docs_dir,  # type: ignore[arg-type]
                output_dir=output_dir,
                missing_threshold=settings.missing_threshold,
            )
            async with EsaClient(
                team=creds["team"],
                access_token=creds["access_token"],
                base_url=creds["base_url"],
            ) as client:
                post = await client.get_post(post_number)
            run.check_lease()
            item = runner.save_post(post)
            record_sync_result(summary, item)
            summary.full_sync_succeeded = True
            summary.finished_at = datetime.now(UTC).isoformat()
            return summary

        summary = asyncio.run(_run())
        report_path = write_sync_report(
            summary,
            reports_dir=settings.reports_dir,  # type: ignore[arg-type]
            label=f"post-{post_number}",
        )
        _finish_sync(run, summary, report_path, phase="kb_download_esa_post")

    return handler


def _make_esa_category_handler(settings: Settings, conn: sqlite3.Connection) -> JobHandler:
    def handler(run: JobRunContext) -> None:
        params = run.job.params
        category = params["category"]
        output_dir = with_docs_prefix(params.get("outputDir") or "docs")
        creds = _esa_client_kwargs(settings)
        source = {
            "output_dir": output_dir,
            "connection": {
                "team": creds["team"],
                "access_token": creds["access_token"],
                "base_url": creds["base_url"],
            },
        }
        run.check_lease()
        service = _build_sync_service(settings, conn)
        summary = asyncio.run(
            service._run_source_sync(  # noqa: SLF001 - kb_download と同じ経路を再利用
                source,
                categories=[category],
                force=False,
                dry_run=False,
                prune_orphans=False,
                emit=run.emit,
                check_lease=run.check_lease,
            )
        )
        report_path = write_sync_report(
            summary,
            reports_dir=settings.reports_dir,  # type: ignore[arg-type]
            label=category,
        )
        _finish_sync(run, summary, report_path, phase="kb_download_esa_category")

    return handler


def _make_esa_search_handler(settings: Settings, conn: sqlite3.Connection) -> JobHandler:
    def handler(run: JobRunContext) -> None:
        params = run.job.params
        query = params["query"]
        output_dir = with_docs_prefix(params.get("outputDir") or "docs")
        creds = _esa_client_kwargs(settings)
        run.check_lease()

        async def _run() -> Any:
            summary = new_sync_summary("esa")
            summary.options = {"outputDir": output_dir, "query": query}
            runner = EsaSyncRunner(
                documents=DocumentRepository(conn),
                root_dir=settings.root_dir,
                docs_dir=settings.docs_dir,  # type: ignore[arg-type]
                output_dir=output_dir,
                missing_threshold=settings.missing_threshold,
            )
            async with EsaClient(
                team=creds["team"],
                access_token=creds["access_token"],
                base_url=creds["base_url"],
            ) as client:
                posts = await client.search_posts(query)
            summary.full_sync_succeeded = True
            for index, post in enumerate(posts):
                run.check_lease()
                item = runner.save_post(post)
                record_sync_result(summary, item)
                run.emit(
                    phase="sync-esa",
                    current=index + 1,
                    total=len(posts),
                    message=f"{query}: {item.action}",
                    item=item.path or item.file_path,
                )
            summary.finished_at = datetime.now(UTC).isoformat()
            return summary

        summary = asyncio.run(_run())
        report_path = write_sync_report(
            summary,
            reports_dir=settings.reports_dir,  # type: ignore[arg-type]
            label=query,
        )
        _finish_sync(run, summary, report_path, phase="kb_download_esa_search")

    return handler


def _make_web_handler(settings: Settings, conn: sqlite3.Connection) -> JobHandler:
    def handler(run: JobRunContext) -> None:
        params = run.job.params
        url = params["url"]
        output_dir = with_docs_prefix(params.get("outputDir") or "docs")
        max_depth = params.get("maxDepth", 3)
        delay = params.get("delay", 1000)
        concurrency = params.get("concurrency", 5)
        run.check_lease()
        service = _build_sync_service(settings, conn)
        summary, report_path = service._sync_web_target(  # noqa: SLF001
            items=[
                {
                    "options": {
                        "url": url,
                        "max_depth": max_depth,
                        "delay": delay,
                        "concurrency": concurrency,
                    }
                }
            ],
            batch_output_dir=output_dir,
            label="download_web",
            force=False,
            dry_run=False,
            emit=run.emit,
            check_lease=run.check_lease,
        )
        _finish_sync(run, summary, report_path, phase="kb_download_web")

    return handler


def build_builtin_handlers(
    *, settings: Settings, conn: sqlite3.Connection
) -> dict[str, JobHandler]:
    """ワーカー/インライン実行向けの組み込み JobHandler 辞書を組み立てる。

    `settings` と `conn` をクロージャで閉じ込める(ジョブ params に秘密情報や
    パスを増やさない — MCP `start_*` の params 形との互換を維持するため)。
    """
    batch_handler = _make_batch_handler(settings, conn)
    handlers: dict[str, JobHandler] = {
        "noop": _noop_handler,
        "batch": batch_handler,
        "kb_download_batch": batch_handler,
        "kb_download_esa_post": _make_esa_post_handler(settings, conn),
        "kb_download_esa_category": _make_esa_category_handler(settings, conn),
        "kb_download_esa_search": _make_esa_search_handler(settings, conn),
        "kb_download_web": _make_web_handler(settings, conn),
        **BUILTIN_RENDER_HANDLERS,
        **BUILTIN_VIDEO_HANDLERS,
    }
    return handlers


def build_builtin_resources() -> dict[str, ResourceRequirement]:
    """組み込みジョブ種別ごとのリソース要求。settings/conn に依存しない。"""
    resources: dict[str, ResourceRequirement] = {
        "batch": _DOCS_WRITE,
        "kb_download_batch": _DOCS_WRITE,
        "kb_download_esa_post": _DOCS_WRITE,
        "kb_download_esa_category": _DOCS_WRITE,
        "kb_download_esa_search": _DOCS_WRITE,
        "kb_download_web": _DOCS_WRITE,
        **BUILTIN_RENDER_RESOURCES,
        # 動画も render 区画を使う（Manim は全プロセス横断で単一実行）
        **BUILTIN_VIDEO_RESOURCES,
    }
    return resources


__all__ = [
    "MCP_DOWNLOAD_JOB_KINDS",
    "build_builtin_handlers",
    "build_builtin_resources",
]
