"""`jobs` コマンド群(設計書 §7, §10): submit/list/show/cancel/retry。

`submit` は CLI 既定の契約を実演する: 既定ではプロセス内で同期実行し、
Rich 進捗を表示する。`--detach` を指定した場合のみキューへ投入して即座に
終了するが、有効な worker heartbeat が無ければ `WORKER_UNAVAILABLE` で失敗する
(§10.1: 実行されないジョブを放置しない)。

組み込みジョブ種別のハンドラは `infrastructure.jobs.builtin_registry` が
提供する(`noop`/`batch`/`kb_download_*`/`render_scene`)。複数回の副作用を
繰り返すハンドラは各反復の間で `JobRunContext.check_lease()` を呼ぶこと
(`JobRunContext` のクラス docstring・`run_job` の docstring 参照)。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import typer

from abist_kb.application.job_service import JobService, ResourceRequirement
from abist_kb.domain.job import Job, JobState, ProgressEvent, Severity
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.infrastructure.jobs.builtin_registry import (
    build_builtin_handlers,
    build_builtin_resources,
)
from abist_kb.presentation.cli.context import AppTyper, get_context
from abist_kb.presentation.console.presenter import Presenter
from abist_kb.presentation.console.progress import progress_scope

jobs_app = AppTyper(help="ジョブの投入・確認・キャンセル・再試行。", no_args_is_help=True)

#: 後方互換: 設定・接続を閉じ込めたハンドラが必要な呼び出し側は
#: `build_builtin_handlers(settings=..., conn=...)` を使うこと。
#: モジュール定数は「登録される種別の集合」の参照用に空の静的表を残さない。
BUILTIN_HANDLERS: dict[str, Any] = {}
BUILTIN_RESOURCE_FOR_KIND: dict[str, ResourceRequirement] = build_builtin_resources()


def _build_service(settings: Any) -> tuple[JobService, Any]:
    # `open_jobs_db` ではなく `open_app_db` を使う(`infrastructure/db/schema.py`
    # のモジュール docstring 参照: 同じ app.sqlite を sources/batches/documents と
    # 共有するため、単独の版一覧でブートストラップすると呼び出し順序によっては
    # `MIGRATION_FAILED` になる)。
    conn = open_app_db(settings.app_db_path)
    handlers = build_builtin_handlers(settings=settings, conn=conn)
    # テストや外部参照向けに最新のハンドラ表を公開する。
    BUILTIN_HANDLERS.clear()
    BUILTIN_HANDLERS.update(handlers)
    service = JobService(
        conn,
        owner_id=str(uuid.uuid4()),
        handlers=handlers,
        resource_for_kind=BUILTIN_RESOURCE_FOR_KIND,
    )
    return service, conn


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "state": str(job.state),
        "params": job.params,
        "result": job.result,
        "error": job.error,
        "cancel_requested": job.cancel_requested,
        "retry_of": job.retry_of,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _present_job(presenter: Presenter, job: Job) -> None:
    if presenter.is_json:
        presenter.json_result(_job_to_dict(job))
        return
    presenter.table(
        "ジョブ",
        ["項目", "値"],
        [
            ["id", job.id],
            ["kind", job.kind],
            ["state", str(job.state)],
            ["error", job.error],
        ],
    )


@jobs_app.command("submit")
def jobs_submit(
    ctx: typer.Context,
    kind: Annotated[
        str, typer.Argument(help="ジョブ種別(組み込み: noop/batch/kb_download_*/render_scene)。")
    ],
    detach: Annotated[
        bool,
        typer.Option(
            "--detach", help="キューへ投入して即座に終了する(既定はプロセス内で同期実行)。"
        ),
    ] = False,
) -> None:
    """ジョブを投入する。既定は同期実行、`--detach` はキュー投入のみ。"""
    cli_ctx = get_context(ctx)
    service, conn = _build_service(cli_ctx.settings)
    try:
        if detach:
            job = service.detach(kind)
            _present_job(cli_ctx.presenter, job)
            return

        with progress_scope(cli_ctx.presenter, description=f"ジョブ実行: {kind}") as handle:

            def on_event(event: ProgressEvent) -> None:
                if event.total is not None:
                    handle.set_total(event.total)
                if event.severity is Severity.ERROR:
                    handle.fail(item=event.item)
                elif event.current is not None:
                    handle.advance(1, item=event.item)

            service.events.subscribe(on_event)
            job = service.run_inline(kind)
        _present_job(cli_ctx.presenter, job)
    finally:
        conn.close()


@jobs_app.command("list")
def jobs_list(
    ctx: typer.Context,
    state: Annotated[
        str | None, typer.Option("--state", help="状態で絞り込む(queued/running/... )。")
    ] = None,
) -> None:
    """ジョブ一覧。"""
    cli_ctx = get_context(ctx)
    service, conn = _build_service(cli_ctx.settings)
    try:
        state_filter = JobState(state) if state is not None else None
        jobs = service.list(state=state_filter)
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result({"jobs": [_job_to_dict(job) for job in jobs]})
            return
        rows = [[job.id, job.kind, str(job.state), job.created_at.isoformat()] for job in jobs]
        cli_ctx.presenter.table("ジョブ一覧", ["id", "kind", "state", "created_at"], rows)
    finally:
        conn.close()


@jobs_app.command("show")
def jobs_show(ctx: typer.Context, job_id: Annotated[str, typer.Argument()]) -> None:
    """ジョブの詳細と進捗履歴。"""
    cli_ctx = get_context(ctx)
    service, conn = _build_service(cli_ctx.settings)
    try:
        job = service.get(job_id)
        history = service.history(job_id)
        if cli_ctx.presenter.is_json:
            payload = _job_to_dict(job)
            payload["history"] = [
                {
                    "phase": event.phase,
                    "current": event.current,
                    "total": event.total,
                    "message": event.message,
                    "severity": str(event.severity),
                    "item": event.item,
                    "timestamp": event.timestamp.isoformat(),
                }
                for event in history
            ]
            cli_ctx.presenter.json_result(payload)
            return
        _present_job(cli_ctx.presenter, job)
        rows = [[e.phase, e.message, str(e.severity), e.timestamp.isoformat()] for e in history]
        cli_ctx.presenter.table("進捗履歴", ["phase", "message", "severity", "timestamp"], rows)
    finally:
        conn.close()


@jobs_app.command("cancel")
def jobs_cancel(ctx: typer.Context, job_id: Annotated[str, typer.Argument()]) -> None:
    """ジョブのキャンセルを要求する。"""
    cli_ctx = get_context(ctx)
    service, conn = _build_service(cli_ctx.settings)
    try:
        service.cancel(job_id)
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result({"job_id": job_id, "cancel_requested": True})
            return
        cli_ctx.presenter.success(f"キャンセルを要求しました: {job_id}")
    finally:
        conn.close()


@jobs_app.command("retry")
def jobs_retry(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument()],
) -> None:
    """失敗/中断したジョブを再投入する(利用者確認が必須、§10.3)。"""
    cli_ctx = get_context(ctx)
    service, conn = _build_service(cli_ctx.settings)
    try:
        original = service.get(job_id)
        confirmed = cli_ctx.presenter.confirm(
            f"ジョブ '{job_id}'(状態: {original.state}, 種別: {original.kind})を再投入しますか?"
            "中断が原因で部分的な状態が残っている可能性があります。",
            assume_yes=cli_ctx.assume_yes,
        )
        if not confirmed:
            cli_ctx.presenter.info("再投入を中止しました。")
            return
        new_job = service.retry(job_id)
        _present_job(cli_ctx.presenter, new_job)
    finally:
        conn.close()


__all__ = ["BUILTIN_HANDLERS", "BUILTIN_RESOURCE_FOR_KIND", "jobs_app"]
