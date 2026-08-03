"""`sync` コマンド群(設計書 §7, §11.2, task-3-brief Step 4): source/batch/all。

`docs-write` リソースリース配下でインライン実行する(`SyncService.run_sync_inline`
経由)。旧 `download-article.js` の CLI 引数(`--force`/`--dry-run`/
`--prune-orphans`)をそのまま踏襲する。
"""

from __future__ import annotations

from typing import Annotated

import typer

from abist_kb.application.sync_service import run_sync_inline
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.cli.context import AppTyper, get_context

sync_app = AppTyper(help="esa 差分同期の実行。", no_args_is_help=True)


def _present_result(cli_ctx, result: dict) -> None:  # type: ignore[no-untyped-def]
    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result)
        return
    if result.get("error"):
        cli_ctx.presenter.danger(f"同期に失敗しました: {result['error']}")
        return
    summary = result.get("summary")
    if summary is not None:
        totals = summary.get("totals", {})
        cli_ctx.presenter.success(
            "同期完了: "
            f"追加 {totals.get('added', 0)} / 更新 {totals.get('updated', 0)} / "
            f"変更なし等 {totals.get('skipped', 0)} / 競合 {totals.get('conflict', 0)} / "
            f"欠落候補 {totals.get('missing', 0)} / エラー {totals.get('error', 0)}"
        )
        if result.get("report_path"):
            cli_ctx.presenter.info(f"レポート: {result['report_path']}")
        return
    results = result.get("results")
    if results is not None:
        cli_ctx.presenter.success(f"{len(results)} バッチを同期しました。")
        return
    cli_ctx.presenter.table("ジョブ", ["列", "値"], [[k, v] for k, v in result.items()])


@sync_app.command("source")
def sync_source(
    ctx: typer.Context,
    source_id: Annotated[str, typer.Argument(help="ソースID(esa)。")],
    category: Annotated[
        list[str], typer.Option("--category", help="同期するカテゴリパス(複数指定可)。")
    ],
    force: Annotated[
        bool, typer.Option("--force", help="ローカル編集との競合を取得元優先で上書きする。")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="ファイルにも DB にも書き込まない。")
    ] = False,
    prune_orphans: Annotated[
        bool,
        typer.Option(
            "--prune-orphans", help="カテゴリ移動で取り残された旧パスのファイルを削除する。"
        ),
    ] = False,
) -> None:
    """1つの esa ソースを指定カテゴリで同期する。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        result = run_sync_inline(
            conn,
            root_dir=cli_ctx.settings.root_dir,
            docs_dir=cli_ctx.settings.docs_dir,
            reports_dir=cli_ctx.settings.reports_dir,
            missing_threshold=cli_ctx.settings.missing_threshold,
            target="source",
            target_id=source_id,
            categories=list(category),
            force=force,
            dry_run=dry_run,
            prune_orphans=prune_orphans,
        )
        _present_result(cli_ctx, result)
    finally:
        conn.close()


@sync_app.command("batch")
def sync_batch(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="バッチID(type=esa)。")],
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    prune_orphans: Annotated[bool, typer.Option("--prune-orphans")] = False,
) -> None:
    """バッチに登録された全カテゴリを同期する。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        result = run_sync_inline(
            conn,
            root_dir=cli_ctx.settings.root_dir,
            docs_dir=cli_ctx.settings.docs_dir,
            reports_dir=cli_ctx.settings.reports_dir,
            missing_threshold=cli_ctx.settings.missing_threshold,
            target="batch",
            target_id=batch_id,
            force=force,
            dry_run=dry_run,
            prune_orphans=prune_orphans,
        )
        _present_result(cli_ctx, result)
    finally:
        conn.close()


@sync_app.command("all")
def sync_all(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    prune_orphans: Annotated[bool, typer.Option("--prune-orphans")] = False,
) -> None:
    """有効な esa バッチをすべて同期する。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        result = run_sync_inline(
            conn,
            root_dir=cli_ctx.settings.root_dir,
            docs_dir=cli_ctx.settings.docs_dir,
            reports_dir=cli_ctx.settings.reports_dir,
            missing_threshold=cli_ctx.settings.missing_threshold,
            target="all",
            force=force,
            dry_run=dry_run,
            prune_orphans=prune_orphans,
        )
        _present_result(cli_ctx, result)
    finally:
        conn.close()


__all__ = ["sync_app"]
