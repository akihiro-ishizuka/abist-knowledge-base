"""`ui` コマンド群(設計書 §7.1, §7.3): `ui web|desktop|tui`。

Web/デスクトップは同じ NiceGUI 画面を使い、デスクトップは `native=True` で
起動する。既定バインドは `127.0.0.1`(§7.1)。`--host` で非ループバックへ公開
する場合は `--access-token`(または環境変数)を必須にする。
"""

from __future__ import annotations

import os
from typing import Annotated

import typer

from abist_kb.presentation.cli.context import AppTyper, get_context

ui_app = AppTyper(help="Web/デスクトップ/TUI 画面の起動。", no_args_is_help=True)

ACCESS_TOKEN_ENV = "ABIST_KB_ACCESS_TOKEN"


@ui_app.command("web")
def ui_web(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", help="バインドホスト(既定 127.0.0.1)。")] = (
        "127.0.0.1"
    ),
    port: Annotated[int, typer.Option("--port", help="待受ポート。")] = 8080,
    access_token: Annotated[
        str | None,
        typer.Option(
            "--access-token",
            help=(f"非ループバック公開時に必須(未指定なら環境変数 {ACCESS_TOKEN_ENV} を使う)。"),
        ),
    ] = None,
) -> None:
    """NiceGUI Web サーバーを起動する(ブラウザモード)。"""
    from nicegui import ui as nicegui_ui

    from abist_kb.presentation.web.app import build_web_app
    from abist_kb.presentation.web.viewmodels.container import ServiceContainer

    cli_ctx = get_context(ctx)
    token = access_token or os.environ.get(ACCESS_TOKEN_ENV)
    container = ServiceContainer(cli_ctx.settings, check_same_thread=False)
    build_web_app(container, bind_host=host, access_token=token)
    cli_ctx.presenter.info(f"Web サーバーを起動します: http://{host}:{port}")
    nicegui_ui.run(host=host, port=port, reload=False, show=False, title="ABIST Knowledge Base")


@ui_app.command("desktop")
def ui_desktop(ctx: typer.Context) -> None:
    """同じ画面をデスクトップアプリ(NiceGUI native mode)として起動する。"""
    from nicegui import ui as nicegui_ui

    from abist_kb.presentation.web.app import build_web_app
    from abist_kb.presentation.web.viewmodels.container import ServiceContainer

    cli_ctx = get_context(ctx)
    container = ServiceContainer(cli_ctx.settings, check_same_thread=False)
    # デスクトップは常にループバックへバインドする(§7.1: 常に127.0.0.1相当)。
    build_web_app(container, bind_host="127.0.0.1", access_token=None)
    nicegui_ui.run(native=True, reload=False, title="ABIST Knowledge Base")


@ui_app.command("tui")
def ui_tui(ctx: typer.Context) -> None:
    """Textual TUI を起動する(§7.1 Task 6.3)。"""
    from abist_kb.presentation.tui.app import run_tui
    from abist_kb.presentation.web.viewmodels.container import ServiceContainer

    cli_ctx = get_context(ctx)
    container = ServiceContainer(cli_ctx.settings)
    try:
        run_tui(container)
    finally:
        container.close()


__all__ = ["ui_app"]
