"""config コマンド群(設計書 §7): `show` / `path` / `validate`。"""

from __future__ import annotations

import typer

from abist_kb.presentation.cli.context import get_context

config_app = typer.Typer(help="設定の確認・検証。", no_args_is_help=True)


@config_app.command("path")
def config_path(ctx: typer.Context) -> None:
    """有効な設定ファイルのパスを表示する(存在有無は問わない)。"""
    cli_ctx = get_context(ctx)
    path = cli_ctx.settings.config_file
    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result({"config_file": str(path)})
        return
    cli_ctx.presenter.line(str(path))


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """秘密情報を伏せた設定値を表示する(`Settings.redacted_dict()` 経由)。"""
    cli_ctx = get_context(ctx)
    payload = cli_ctx.settings.redacted_dict()
    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(payload)
        return
    rows = [[key, str(value)] for key, value in sorted(payload.items())]
    cli_ctx.presenter.table("設定", ["キー", "値"], rows)


@config_app.command("validate")
def config_validate(ctx: typer.Context) -> None:
    """設定値を検証する。

    設定の読み込みと検証そのものは全コマンド共通のコールバック
    (`app.main_callback`)が毎回実行しており、不正な設定はここへ到達する前に
    `AppError` として処理済み(終了コード3)である。したがってこのコマンド本体へ
    到達した時点で検証は成功しており、成功を提示するだけでよい。
    """
    cli_ctx = get_context(ctx)
    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result({"ok": True})
        return
    cli_ctx.presenter.success("設定は正常です。")


__all__ = ["config_app"]
