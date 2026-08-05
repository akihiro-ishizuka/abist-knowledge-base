"""`api` コマンド群: スタンドアロン FastAPI `/api/v1` サーバー。

MCP-only UI cutover Phase 2a。NiceGUI 非依存で `create_api_app()` を
uvicorn 上に起動する。安全契約は `ui web` と同じ
(既定 `127.0.0.1`、非ループバックはアクセストークン必須)。
"""

from __future__ import annotations

import os
from typing import Annotated

import typer

from abist_kb.presentation.cli.context import AppTyper, get_context

api_app = AppTyper(help="スタンドアロン HTTP API の起動。", no_args_is_help=True)

ACCESS_TOKEN_ENV = "ABIST_KB_ACCESS_TOKEN"


@api_app.command("serve")
def api_serve(
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
    """スタンドアロンの FastAPI `/api/v1` サーバーを起動する。"""
    import uvicorn

    from abist_kb.presentation.api.app import create_api_app
    from abist_kb.presentation.common.container import ServiceContainer

    cli_ctx = get_context(ctx)
    token = access_token or os.environ.get(ACCESS_TOKEN_ENV)
    container = ServiceContainer(cli_ctx.settings, check_same_thread=False)
    app = create_api_app(container, bind_host=host, access_token=token)
    cli_ctx.presenter.info(f"API サーバーを起動します: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = ["ACCESS_TOKEN_ENV", "api_app"]
