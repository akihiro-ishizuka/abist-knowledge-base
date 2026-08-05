"""MCP stdio サーバーの基盤(M5 task-1-brief Step2)。

**stdout 純度が最優先の制約**: このモジュールを import しただけで stdout に
1バイトも出てはならない(`tests/mcp/test_server_purity.py` が保証する)。
ログ・診断メッセージはすべて stderr へ出す。stdio транспорト は JSON-RPC の
唯一の経路であり、stdout に他の何かが混ざると壊れる。

既定は stdio トランスポート。Streamable HTTP は `run_http()` を明示的に
呼んだ場合のみ起動する(task-1-brief: 「Streamable HTTP は明示起動のみ」)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from abist_kb.presentation.mcp.jobs_tools import JobTools
from abist_kb.presentation.mcp.jobs_tools import list_tools as jobs_list_tools
from abist_kb.presentation.mcp.jobs_tools import validate_arguments as validate_jobs_arguments
from abist_kb.presentation.mcp.kb_admin import KbAdminTools
from abist_kb.presentation.mcp.kb_admin import handlers_for as kb_admin_handlers
from abist_kb.presentation.mcp.kb_admin import list_tools as kb_admin_list_tools
from abist_kb.presentation.mcp.kb_admin import validate_arguments as validate_kb_admin_arguments
from abist_kb.presentation.mcp.kb_download import KbDownloadTools
from abist_kb.presentation.mcp.kb_download import list_tools as kb_download_list_tools
from abist_kb.presentation.mcp.kb_download import (
    validate_arguments as validate_kb_download_arguments,
)
from abist_kb.presentation.mcp.kb_search import KbSearchTools
from abist_kb.presentation.mcp.kb_search import list_tools as kb_search_list_tools
from abist_kb.presentation.mcp.kb_visualize import KbVisualizeTools
from abist_kb.presentation.mcp.kb_visualize import list_tools as kb_visualize_list_tools
from abist_kb.presentation.mcp.kb_visualize import (
    validate_arguments as validate_kb_visualize_arguments,
)
from abist_kb.presentation.mcp.payloads import error_result

SERVER_NAMES: tuple[str, ...] = ("kb-download", "kb-search", "kb-visualize")

#: `all` / `kb-admin` は `SERVER_NAMES`(互換3)には含めない。既定の公式構成は
#: 互換3のまま残し、管理操作用の `kb-admin` と結合用の `all` を別キーで追加する。
ALL_SERVER_NAME = "all"
KB_ADMIN_SERVER_NAME = "kb-admin"

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


def build_kb_download_server(
    *,
    app_db_path: Path,
    root_dir: Path | None = None,
    docs_dir: Path | None = None,
    reports_dir: Path | None = None,
    missing_threshold: int | None = None,
) -> Server[Any, Any]:
    """kb-download サーバー(8ツール、M5 task-3b: ブロッキング実処理まで実装)を組み立てる。

    SQLite 接続はこの `Server` インスタンスの寿命の間開いたままにする
    (`app.sqlite` は読み取り・書き込み両方に使う)。`root_dir`/`docs_dir`/
    `reports_dir` はブロッキング実処理(`SyncService` 経由の esa/web/git 同期)が
    ファイルを読み書きする先。未指定時は `KbDownloadTools` の既定
    (`root_dir=cwd`、`docs_dir=root_dir/docs`、`reports_dir=root_dir/reports`)
    を使う。
    """
    from abist_kb.infrastructure.db.schema import open_app_db
    from abist_kb.infrastructure.sources.esa import DEFAULT_MISSING_THRESHOLD

    server: Server[Any, Any] = Server("kb-download", version=_SERVER_VERSION)
    conn: sqlite3.Connection = open_app_db(app_db_path)
    tools = KbDownloadTools(
        conn,
        root_dir=root_dir,
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        missing_threshold=(
            missing_threshold if missing_threshold is not None else DEFAULT_MISSING_THRESHOLD
        ),
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return kb_download_list_tools()

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        validation_error = validate_kb_download_arguments(name, arguments)
        if validation_error is not None:
            return validation_error
        handler = {
            "list_batches": tools.list_batches,
            "run_batch": tools.run_batch,
            "add_web_batch": tools.add_web_batch,
            "download_esa_post": tools.download_esa_post,
            "download_esa_category": tools.download_esa_category,
            "download_esa_search": tools.download_esa_search,
            "download_web": tools.download_web,
            "download_git": tools.download_git,
        }.get(name)
        if handler is None:
            return error_result(f"未知のツールです: {name}")
        return handler(arguments)

    return server


def build_kb_visualize_server(
    *,
    app_db_path: Path,
    docs_dir: Path,
    repo_root: Path | None = None,
    reports_dir: Path | None = None,
) -> Server[Any, Any]:
    """kb-visualize サーバー(list_scene_kinds/check_visualize_deps/render_scene)を組み立てる。

    `render` リソースリースの直列化(`CONCURRENT_RENDER` 互換)には SQLite 接続が
    必要なため、`app_db_path` を kb-download と同じ `app.sqlite` に向ける。出力は
    `reports_dir` 未指定時 `<repo_root>/reports` の `visualizations/` 配下。
    """
    from abist_kb.infrastructure.db.schema import open_app_db

    server: Server[Any, Any] = Server("kb-visualize", version=_SERVER_VERSION)
    conn: sqlite3.Connection = open_app_db(app_db_path)
    resolved_root = repo_root if repo_root is not None else Path.cwd()
    resolved_reports = (
        reports_dir if reports_dir is not None else (resolved_root / "reports")
    ) / "visualizations"
    tools = KbVisualizeTools(
        conn, docs_dir=docs_dir, reports_dir=resolved_reports, repo_root=resolved_root
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return kb_visualize_list_tools()

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        validation_error = validate_kb_visualize_arguments(name, arguments)
        if validation_error is not None:
            return validation_error
        handler = {
            "list_scene_kinds": tools.list_scene_kinds,
            "check_visualize_deps": tools.check_visualize_deps,
            "render_scene": tools.render_scene,
        }.get(name)
        if handler is None:
            return error_result(f"未知のツールです: {name}")
        return handler(arguments)

    return server


def build_kb_admin_server(
    *,
    docs_dir: Path,
    work_index_path: Path,
    reference_index_path: Path,
    app_db_path: Path,
    root_dir: Path | None = None,
    reports_dir: Path | None = None,
) -> Server[Any, Any]:
    """kb-admin サーバー(管理操作ツール群)を組み立てる。"""
    from abist_kb.config import Settings
    from abist_kb.presentation.common.container import ServiceContainer

    server: Server[Any, Any] = Server(KB_ADMIN_SERVER_NAME, version=_SERVER_VERSION)
    resolved_root = root_dir if root_dir is not None else Path.cwd()
    resolved_reports = reports_dir if reports_dir is not None else (resolved_root / "reports")
    settings = Settings(
        root_dir=resolved_root,
        docs_dir=docs_dir,
        reports_dir=resolved_reports,
        app_db_path=app_db_path,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
    )
    settings.ensure_directories()
    container = ServiceContainer(settings)
    tools = KbAdminTools(container)
    handlers = kb_admin_handlers(tools)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return kb_admin_list_tools()

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        validation_error = validate_kb_admin_arguments(name, arguments)
        if validation_error is not None:
            return validation_error
        handler = handlers.get(name)
        if handler is None:
            return error_result(f"未知のツールです: {name}")
        return handler(arguments)

    return server


def build_all_server(
    *,
    docs_dir: Path,
    work_index_path: Path,
    reference_index_path: Path,
    app_db_path: Path,
    root_dir: Path | None = None,
    reports_dir: Path | None = None,
    missing_threshold: int | None = None,
) -> Server[Any, Any]:
    """`all` サーバー: 互換3(kb-search/kb-download/kb-visualize) + jobs 拡張 +
    kb-admin を同一プロセスで公開する。互換3の list_tools / スキーマ / 応答は
    変更しない(additions only)。
    """
    from abist_kb.config import Settings
    from abist_kb.infrastructure.db.schema import open_app_db
    from abist_kb.infrastructure.sources.esa import DEFAULT_MISSING_THRESHOLD
    from abist_kb.presentation.common.container import ServiceContainer

    server: Server[Any, Any] = Server(ALL_SERVER_NAME, version=_SERVER_VERSION)
    conn: sqlite3.Connection = open_app_db(app_db_path)

    resolved_root_dir = root_dir if root_dir is not None else Path.cwd()
    resolved_reports_dir = (
        reports_dir if reports_dir is not None else (resolved_root_dir / "reports")
    )

    search_tools = KbSearchTools(
        docs_dir=docs_dir,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
    )
    download_tools = KbDownloadTools(
        conn,
        root_dir=root_dir,
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        missing_threshold=(
            missing_threshold if missing_threshold is not None else DEFAULT_MISSING_THRESHOLD
        ),
    )
    visualize_tools = KbVisualizeTools(
        conn,
        docs_dir=docs_dir,
        reports_dir=resolved_reports_dir / "visualizations",
        repo_root=resolved_root_dir,
    )
    job_tools = JobTools(
        conn,
        docs_dir=docs_dir,
        app_db_path=app_db_path,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
        reports_dir=resolved_reports_dir,
        repo_root=resolved_root_dir,
    )
    admin_settings = Settings(
        root_dir=resolved_root_dir,
        docs_dir=docs_dir,
        reports_dir=resolved_reports_dir,
        app_db_path=app_db_path,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
    )
    admin_settings.ensure_directories()
    admin_tools = KbAdminTools(ServiceContainer(admin_settings))

    handlers: dict[str, Any] = {
        "search_kb": search_tools.search_kb,
        "get_document": search_tools.get_document,
        "get_chunk": search_tools.get_chunk,
        "index_status": search_tools.index_status,
        "list_batches": download_tools.list_batches,
        "run_batch": download_tools.run_batch,
        "add_web_batch": download_tools.add_web_batch,
        "download_esa_post": download_tools.download_esa_post,
        "download_esa_category": download_tools.download_esa_category,
        "download_esa_search": download_tools.download_esa_search,
        "download_web": download_tools.download_web,
        "download_git": download_tools.download_git,
        "list_scene_kinds": visualize_tools.list_scene_kinds,
        "check_visualize_deps": visualize_tools.check_visualize_deps,
        "render_scene": visualize_tools.render_scene,
        "start_run_batch": job_tools.start_run_batch,
        "start_download_esa_post": job_tools.start_download_esa_post,
        "start_download_esa_category": job_tools.start_download_esa_category,
        "start_download_esa_search": job_tools.start_download_esa_search,
        "start_download_web": job_tools.start_download_web,
        "start_download_git": job_tools.start_download_git,
        "start_render_scene": job_tools.start_render_scene,
        "job_status": job_tools.job_status,
        "cancel_job": job_tools.cancel_job,
        "get_batch": job_tools.get_batch,
        "list_corpora": job_tools.list_corpora,
        "system_status": job_tools.system_status,
        **kb_admin_handlers(admin_tools),
    }

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            *kb_search_list_tools(),
            *kb_download_list_tools(),
            *kb_visualize_list_tools(),
            *jobs_list_tools(),
            *kb_admin_list_tools(),
        ]

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        validation_error = validate_kb_download_arguments(name, arguments)
        if validation_error is not None:
            return validation_error
        validation_error = validate_kb_visualize_arguments(name, arguments)
        if validation_error is not None:
            return validation_error
        validation_error = validate_jobs_arguments(name, arguments)
        if validation_error is not None:
            return validation_error
        validation_error = validate_kb_admin_arguments(name, arguments)
        if validation_error is not None:
            return validation_error
        handler = handlers.get(name)
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
    app_db_path: Path | None = None,
    root_dir: Path | None = None,
    reports_dir: Path | None = None,
    missing_threshold: int | None = None,
) -> Server[Any, Any]:
    """サーバー名から `Server` を組み立てる。"""
    if name == "kb-search":
        return build_kb_search_server(
            docs_dir=docs_dir,
            work_index_path=work_index_path,
            reference_index_path=reference_index_path,
        )
    if name == "kb-download":
        if app_db_path is None:
            raise ValueError("kb-download サーバーには app_db_path が必要です")
        return build_kb_download_server(
            app_db_path=app_db_path,
            root_dir=root_dir,
            docs_dir=docs_dir,
            reports_dir=reports_dir,
            missing_threshold=missing_threshold,
        )
    if name == "kb-visualize":
        if app_db_path is None:
            raise ValueError("kb-visualize サーバーには app_db_path が必要です")
        return build_kb_visualize_server(
            app_db_path=app_db_path,
            docs_dir=docs_dir,
            repo_root=root_dir,
            reports_dir=reports_dir,
        )
    if name == KB_ADMIN_SERVER_NAME:
        if app_db_path is None:
            raise ValueError("kb-admin サーバーには app_db_path が必要です")
        return build_kb_admin_server(
            docs_dir=docs_dir,
            work_index_path=work_index_path,
            reference_index_path=reference_index_path,
            app_db_path=app_db_path,
            root_dir=root_dir,
            reports_dir=reports_dir,
        )
    if name == ALL_SERVER_NAME:
        if app_db_path is None:
            raise ValueError("'all' サーバーには app_db_path が必要です")
        return build_all_server(
            docs_dir=docs_dir,
            work_index_path=work_index_path,
            reference_index_path=reference_index_path,
            app_db_path=app_db_path,
            root_dir=root_dir,
            reports_dir=reports_dir,
            missing_threshold=missing_threshold,
        )
    raise NotImplementedError(
        f"未知のサーバー名です: '{name}'"
        f"(kb-search/kb-download/kb-visualize/kb-admin/all のみ実装済み)。"
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
    "ALL_SERVER_NAME",
    "KB_ADMIN_SERVER_NAME",
    "SERVER_NAMES",
    "build_all_server",
    "build_kb_admin_server",
    "build_kb_download_server",
    "build_kb_search_server",
    "build_kb_visualize_server",
    "build_server",
    "run_http",
    "run_stdio",
]
