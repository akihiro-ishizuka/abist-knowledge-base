"""`sync` コマンド群(設計書 §7, §11.2, task-3-brief Step 4, task-5b): source/batch/all。

`docs-write` リソースリース配下でインライン実行する(`SyncService.run_sync_inline`
経由)。旧 `download-article.js`/`download-web.js`/`download-git.js` の CLI 引数
(`--force`/`--dry-run`/`--prune-orphans`)をそのまま踏襲する(web/git には
該当しない引数は `SyncService` 側で無視・拒否される)。

**`sync all` の部分失敗(task-5b の判断)**: 1バッチの失敗で残りを止めず、
`SyncService.sync_all` が全バッチを実行し切ってから結果を返す(該当箇所の
docstring参照)。1件でも失敗があれば、結果を提示した *後* に
`AppError(ErrorCode.CONFLICT, exit_code=ExitCode.CONFLICT)` を送出して
終了コード5(部分成功)で終わる。
"""

from __future__ import annotations

from typing import Annotated

import typer

from abist_kb.application.batch_service import BatchService
from abist_kb.application.sync_service import run_sync_inline
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.cli.batch_cmd import resolve_batch
from abist_kb.presentation.cli.context import AppTyper, get_context

sync_app = AppTyper(help="ソース同期(esa/web)の実行。", no_args_is_help=True)


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
        if summary.get("note"):
            cli_ctx.presenter.info(f"備考: {summary['note']}")
        if result.get("report_path"):
            cli_ctx.presenter.info(f"レポート: {result['report_path']}")
        return
    results = result.get("results")
    if results is not None:
        failed = [r for r in results if r.get("error")]
        if failed:
            cli_ctx.presenter.danger(f"{len(results)}バッチ中{len(failed)}件が同期に失敗しました。")
            for r in failed:
                err = r["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                cli_ctx.presenter.danger(f"  - {r['batch_name']}: {message}")
        else:
            cli_ctx.presenter.success(f"{len(results)} バッチを同期しました。")
        return
    cli_ctx.presenter.table("ジョブ", ["列", "値"], [[k, v] for k, v in result.items()])


def _fail_if_partial(result: dict) -> None:  # type: ignore[no-untyped-def]
    """`sync all` の部分失敗を検出し、結果提示 *後* に終了コード5で失敗させる。

    モジュール docstring参照: `SyncService.sync_all` 自体は続行方針のため、
    ジョブとしては成功として記録される。終了コードへの反映は CLI のこの箇所が
    唯一の責務を持つ。
    """
    results = result.get("results")
    if not results:
        return
    failed = [r for r in results if r.get("error")]
    if not failed:
        return
    raise AppError(
        code=ErrorCode.CONFLICT,
        message=f"{len(failed)}/{len(results)} バッチの同期に失敗しました。",
        details={"failed_batch_ids": [r["batch_id"] for r in failed]},
        exit_code=ExitCode.CONFLICT,
    )


@sync_app.command("source")
def sync_source(
    ctx: typer.Context,
    source_id: Annotated[str, typer.Argument(help="ソースID(esa/web)。")],
    category: Annotated[
        list[str] | None,
        typer.Option(
            "--category", help="同期するカテゴリパス(esa のみ、複数指定可)。web では無視。"
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="ローカル編集との競合を取得元優先で上書きする(esa/web)。"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="ファイルにも DB にも書き込まない(esa/web)。"),
    ] = False,
    prune_orphans: Annotated[
        bool,
        typer.Option(
            "--prune-orphans",
            help="カテゴリ移動で取り残された旧パスのファイルを削除する(esa のみ)。",
        ),
    ] = False,
) -> None:
    """1つのソースを同期する(esa はカテゴリ単位、web は接続設定1件分)。"""
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
            categories=list(category) if category else None,
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
    batch_id: Annotated[str, typer.Argument(help="バッチ ID または名前(esa/web)。")],
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    prune_orphans: Annotated[bool, typer.Option("--prune-orphans")] = False,
) -> None:
    """バッチに登録された全対象を、バッチ種別に応じて同期する。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        resolved_id = resolve_batch(BatchService(conn), batch_id)["id"]
        result = run_sync_inline(
            conn,
            root_dir=cli_ctx.settings.root_dir,
            docs_dir=cli_ctx.settings.docs_dir,
            reports_dir=cli_ctx.settings.reports_dir,
            missing_threshold=cli_ctx.settings.missing_threshold,
            target="batch",
            target_id=resolved_id,
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
    """有効な全バッチ(esa/web)を同期する。

    1バッチの失敗で残りを止めない(`SyncService.sync_all` docstring参照)。
    1件でも失敗があれば、結果提示の後に終了コード5(部分成功)で終わる。
    """
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
        _fail_if_partial(result)
    finally:
        conn.close()


__all__ = ["sync_app"]
