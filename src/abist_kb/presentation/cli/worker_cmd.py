"""`worker` コマンド群(設計書 §7, §10.1)。

`worker run` は `WorkerSupervisor` を起動する。Web/デスクトップ/TUI/MCP の
各常駐エントリポイントも起動時に同じ Supervisor を開始する想定だが、常駐UIを
使わないヘッドレス環境ではこのコマンドを明示的に起動する必要がある。
"""

from __future__ import annotations

import uuid
from typing import Annotated

import typer

from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.db import open_jobs_db
from abist_kb.infrastructure.jobs.supervisor import WorkerSupervisor
from abist_kb.presentation.cli.context import AppTyper, get_context
from abist_kb.presentation.cli.jobs_cmd import BUILTIN_HANDLERS

worker_app = AppTyper(
    help="ワーカー(リーダー選出・ジョブ実行)の起動と状態確認。", no_args_is_help=True
)


@worker_app.command("run")
def worker_run(
    ctx: typer.Context,
    once: Annotated[
        bool, typer.Option("--once", help="1周だけ実行して終了する(テスト・診断用)。")
    ] = False,
) -> None:
    """Supervisor を起動する。既定は常駐(Ctrl+C で終了)、`--once` は1周のみ。"""
    cli_ctx = get_context(ctx)
    conn = open_jobs_db(cli_ctx.settings.app_db_path)
    try:
        supervisor = WorkerSupervisor(conn, owner_id=str(uuid.uuid4()), handlers=BUILTIN_HANDLERS)
        if once:
            did_work = supervisor.tick()
            cli_ctx.presenter.success(
                f"1周実行しました(leader={supervisor.is_leader}, did_work={did_work})。"
            )
            return
        cli_ctx.presenter.info(
            f"ワーカーを起動しました(owner_id={supervisor.owner_id})。Ctrl+C で終了します。"
        )
        try:
            supervisor.run_forever()
        except KeyboardInterrupt:
            cli_ctx.presenter.info("ワーカーを停止しました。")
    finally:
        conn.close()


@worker_app.command("status")
def worker_status(ctx: typer.Context) -> None:
    """現在のリーダー(worker_leases)を表示する。"""
    cli_ctx = get_context(ctx)
    conn = open_jobs_db(cli_ctx.settings.app_db_path)
    try:
        lease = leases.current_worker_lease(conn)
        if lease is None:
            if cli_ctx.presenter.is_json:
                cli_ctx.presenter.json_result({"leader": None})
                return
            cli_ctx.presenter.warning("現在リーダーは存在しません。")
            return
        owner_id, expires_at = lease
        is_live = leases.has_live_worker(conn)
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result(
                {"owner_id": owner_id, "expires_at": expires_at.isoformat(), "live": is_live}
            )
            return
        cli_ctx.presenter.line(
            f"owner_id={owner_id} expires_at={expires_at.isoformat()} live={is_live}"
        )
    finally:
        conn.close()


__all__ = ["worker_app"]
