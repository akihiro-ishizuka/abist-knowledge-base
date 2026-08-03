"""MCP stdio サーバーの基盤(M5 task-1-brief Step2)。

**stdout 純度が最優先の制約**: このモジュールを import しただけで stdout に
1バイトも出てはならない(`tests/mcp/test_server_purity.py` が保証する)。
ログ・診断メッセージはすべて stderr へ出す。stdio транспорト は JSON-RPC の
唯一の経路であり、stdout に他の何かが混ざると壊れる。

既定は stdio トランスポート。Streamable HTTP は `run_http()` を明示的に
呼んだ場合のみ起動する(task-1-brief: 「Streamable HTTP は明示起動のみ」)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from abist_kb.presentation.mcp.kb_search import KbSearchTools
from abist_kb.presentation.mcp.kb_search import list_tools as kb_search_list_tools
from abist_kb.presentation.mcp.payloads import error_result

SERVER_NAMES: tuple[str, ...] = ("kb-download", "kb-search", "kb-visualize")

_SERVER_VERSION = "1.0.0"


def build_kb_search_server(
    *, docs_dir: Path, work_index_path: Path, reference_index_path: Path
) -> Server[Any, Any]:
    """kb-search サーバー(search_kb/get_document/get_chunk/index_status)を組み立てる。"""
    server: Server[Any, Any] = Server("kb-search", version=_SERVER_VERSION)
    tools = KbSearchTools(
        docs_dir=docs_dir,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return kb_search_list_tools()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        handler = {
            "search_kb": tools.search_kb,
            "get_document": tools.get_document,
            "get_chunk": tools.get_chunk,
            "index_status": tools.index_status,
        }.get(name)
        if handler is None:
            return error_result(f"未知のツールです: {name}")
        return handler(arguments)

    return server


def build_server(
    name: str,
    *,
    docs_dir: Path,
    work_index_path: Path,
    reference_index_path: Path,
) -> Server[Any, Any]:
    """サーバー名から `Server` を組み立てる。kb-download/kb-visualize は後続マイルストーン。"""
    if name == "kb-search":
        return build_kb_search_server(
            docs_dir=docs_dir,
            work_index_path=work_index_path,
            reference_index_path=reference_index_path,
        )
    raise NotImplementedError(
        f"サーバー '{name}' は M5 task-1/2 の範囲外です(kb-search のみ実装済み)。"
    )


async def run_stdio(server: Server[Any, Any]) -> None:
    """stdio トランスポートでサーバーを起動する(標準入出力のみを JSON-RPC に使う)。

    起動メッセージ・ログは一切 stdout へ書かない。`stdio_server()` 自体が
    `sys.stdin`/`sys.stdout` をバイナリでラップして使うため、Python の
    `print()` 等をこの経路の前後で誤って呼ばない限り stdout は汚れない。
    """
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=server.name,
                server_version=_SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


async def run_http(server: Server[Any, Any], *, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Streamable HTTP トランスポートでサーバーを起動する(明示要求時のみ呼ばれる)。"""
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    session_manager = StreamableHTTPSessionManager(app=server)

    async def handle(scope: Any, receive: Any, send: Any) -> None:
        await session_manager.handle_request(scope, receive, send)

    starlette_app = Starlette(routes=[Mount("/mcp", app=handle)])
    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()


__all__ = [
    "SERVER_NAMES",
    "build_kb_search_server",
    "build_server",
    "run_http",
    "run_stdio",
]
