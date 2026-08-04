"""`mcp serve` コマンド(M5 task-1-brief Step4)。

**stdout 純度が最優先の制約**: `main_callback` は全サブコマンドの実行前に
`sys.stdout` を束縛した `Presenter` を構築するが、この Presenter の
stdout 書込メソッドは `mcp serve` の中では一切呼ばない。ここで使うのは
自前で stderr へ束縛し直した Presenter だけである — 起動メッセージも
エラー提示もすべて stderr へ出す(stdout は JSON-RPC の唯一の経路)。
"""

from __future__ import annotations

import sys
from typing import Annotated

import anyio
import typer

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.presentation.cli.context import AppTyper, get_context
from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.presenter import Presenter
from abist_kb.presentation.mcp.server_core import SERVER_NAMES, build_server, run_http, run_stdio

_ALL = "all"
_VALID_SERVERS = (*SERVER_NAMES, _ALL)

mcp_app = AppTyper(help="MCP サーバー(kb-download/kb-search/kb-visualize)の起動。")


def _stderr_presenter() -> Presenter:
    """stdout に触れない、stderr 専用の Presenter を新たに作る(共有 Presenter は使わない)。"""
    return Presenter(OutputMode.PLAIN, stdout=sys.stderr, stderr=sys.stderr)


async def _serve_stdio(
    server_name: str,
    docs_dir,
    work_index_path,
    reference_index_path,
    app_db_path,
    root_dir,
    reports_dir,
    missing_threshold,
) -> None:  # type: ignore[no-untyped-def]
    server = build_server(
        server_name,
        docs_dir=docs_dir,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
        app_db_path=app_db_path,
        root_dir=root_dir,
        reports_dir=reports_dir,
        missing_threshold=missing_threshold,
    )
    await run_stdio(server)


async def _serve_http(
    server_name: str,
    docs_dir,
    work_index_path,
    reference_index_path,
    app_db_path,
    root_dir,
    reports_dir,
    missing_threshold,
    port: int,  # type: ignore[no-untyped-def]
) -> None:
    server = build_server(
        server_name,
        docs_dir=docs_dir,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
        app_db_path=app_db_path,
        root_dir=root_dir,
        reports_dir=reports_dir,
        missing_threshold=missing_threshold,
    )
    await run_http(server, port=port)


@mcp_app.command("serve")
def serve(
    ctx: typer.Context,
    server: Annotated[
        str,
        typer.Argument(help=f"起動するサーバー: {', '.join(_VALID_SERVERS)}。"),
    ],
    transport: Annotated[
        str, typer.Option("--transport", help="stdio(既定) または http。")
    ] = "stdio",
    port: Annotated[
        int, typer.Option("--port", help="--transport http のときの待受ポート。")
    ] = 8765,
) -> None:
    """MCP サーバーを起動する(既定は stdio。`--transport http` で明示起動のみ)。"""
    cli_ctx = get_context(ctx)
    stderr_presenter = _stderr_presenter()

    if server not in _VALID_SERVERS:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"未知のサーバーです: {server!r}(有効値: {', '.join(_VALID_SERVERS)})",
        )
    if transport not in ("stdio", "http"):
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"未知のトランスポートです: {transport!r}(有効値: stdio, http)",
        )
    settings = cli_ctx.settings
    stderr_presenter.info(f"[{server}] MCP server starting ({transport})")

    try:
        if transport == "http":
            anyio.run(
                _serve_http,
                server,
                settings.docs_dir,
                settings.work_index_path,
                settings.reference_index_path,
                settings.app_db_path,
                settings.root_dir,
                settings.reports_dir,
                settings.missing_threshold,
                port,
            )
        else:
            anyio.run(
                _serve_stdio,
                server,
                settings.docs_dir,
                settings.work_index_path,
                settings.reference_index_path,
                settings.app_db_path,
                settings.root_dir,
                settings.reports_dir,
                settings.missing_threshold,
            )
    except NotImplementedError as exc:
        raise AppError(code=ErrorCode.INVALID_INPUT, message=str(exc)) from exc


__all__ = ["mcp_app", "serve"]
