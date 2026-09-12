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

from abist_kb.config import Settings, load_settings
from abist_kb.infrastructure.visualization.artifact_store import visualizations_dir
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
from abist_kb.presentation.mcp.kb_video import KbVideoTools
from abist_kb.presentation.mcp.kb_video import handlers_for as kb_video_handlers
from abist_kb.presentation.mcp.kb_video import list_tools as kb_video_list_tools
from abist_kb.presentation.mcp.kb_video import (
    validate_arguments as validate_kb_video_arguments,
)
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


def _resolve_settings(
    *,
    settings: Settings | None,
    root_dir: Path | None,
) -> Settings:
    """CLI でロード済みの Settings を優先し、無ければ `load_settings(root=...)`。

    `Settings(...)` をパスだけ渡して作り直すと、カレントの `.env` を読んでしまい
    `--root` 配下の OpenAI/esa/Git 資格情報を取り違える。
    """
    if settings is not None:
        return settings
    loaded = load_settings(root=root_dir if root_dir is not None else Path.cwd())
    loaded.ensure_directories()
    return loaded


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
    """kb-visualize サーバー(scene_kinds/deps/render + カタログ2ツール)を組み立てる。

    `render` リソースリースの直列化(`CONCURRENT_RENDER` 互換)には SQLite 接続が
    必要なため、`app_db_path` を kb-download と同じ `app.sqlite` に向ける。出力は
    `reports_dir` 未指定時 `<repo_root>/reports` の `visualizations/` 配下。
    """
    from abist_kb.infrastructure.db.schema import open_app_db

    server: Server[Any, Any] = Server("kb-visualize", version=_SERVER_VERSION)
    conn: sqlite3.Connection = open_app_db(app_db_path)
    resolved_root = repo_root if repo_root is not None else Path.cwd()
    resolved_reports = visualizations_dir(
        reports_dir if reports_dir is not None else (resolved_root / "reports")
    )
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
            "list_visualizations": tools.list_visualizations,
            "get_visualization": tools.get_visualization,
        }.get(name)
        if handler is None:
            return error_result(f"未知のツールです: {name}")
        return handler(arguments)

    return server


def _settings_for_admin(
    *,
    settings: Settings | None,
    root_dir: Path | None,
    docs_dir: Path | None = None,
    work_index_path: Path | None = None,
    reference_index_path: Path | None = None,
    app_db_path: Path | None = None,
    reports_dir: Path | None = None,
) -> Settings:
    """資格情報は `settings` / `load_settings(root=...)` から取り、パスのみ上書き可。"""
    resolved = _resolve_settings(settings=settings, root_dir=root_dir)
    updates: dict[str, Any] = {}
    if root_dir is not None:
        updates["root_dir"] = root_dir
    if docs_dir is not None:
        updates["docs_dir"] = docs_dir
    if work_index_path is not None:
        updates["work_index_path"] = work_index_path
    if reference_index_path is not None:
        updates["reference_index_path"] = reference_index_path
    if app_db_path is not None:
        updates["app_db_path"] = app_db_path
    if reports_dir is not None:
        updates["reports_dir"] = reports_dir
    if updates:
        resolved = resolved.model_copy(update=updates)
    resolved.ensure_directories()
    return resolved


def build_kb_admin_server(
    *,
    docs_dir: Path | None = None,
    work_index_path: Path | None = None,
    reference_index_path: Path | None = None,
    app_db_path: Path | None = None,
    root_dir: Path | None = None,
    reports_dir: Path | None = None,
    settings: Settings | None = None,
) -> Server[Any, Any]:
    """kb-admin サーバー(管理操作ツール群)を組み立てる。"""
    from abist_kb.presentation.common.container import ServiceContainer

    server: Server[Any, Any] = Server(KB_ADMIN_SERVER_NAME, version=_SERVER_VERSION)
    resolved = _settings_for_admin(
        settings=settings,
        root_dir=root_dir,
        docs_dir=docs_dir,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
        app_db_path=app_db_path,
        reports_dir=reports_dir,
    )
    container = ServiceContainer(resolved)
    tools = KbAdminTools(container)
    handlers = kb_admin_handlers(tools)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return kb_admin_list_tools()

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        validation_error = validate_kb_video_arguments(name, arguments)
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


def build_all_server(
    *,
    docs_dir: Path | None = None,
    work_index_path: Path | None = None,
    reference_index_path: Path | None = None,
    app_db_path: Path | None = None,
    root_dir: Path | None = None,
    reports_dir: Path | None = None,
    missing_threshold: int | None = None,
    settings: Settings | None = None,
) -> Server[Any, Any]:
    """`all` サーバー: 互換3(kb-search/kb-download/kb-visualize) + jobs 拡張 +
    kb-admin を同一プロセスで公開する。互換3の list_tools / スキーマ / 応答は
    変更しない(additions only)。
    """
    from abist_kb.infrastructure.db.schema import open_app_db
    from abist_kb.infrastructure.sources.esa import DEFAULT_MISSING_THRESHOLD
    from abist_kb.presentation.common.container import ServiceContainer

    server: Server[Any, Any] = Server(ALL_SERVER_NAME, version=_SERVER_VERSION)
    resolved = _settings_for_admin(
        settings=settings,
        root_dir=root_dir,
        docs_dir=docs_dir,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
        app_db_path=app_db_path,
        reports_dir=reports_dir,
    )
    conn: sqlite3.Connection = open_app_db(resolved.app_db_path)  # type: ignore[arg-type]
    threshold = missing_threshold if missing_threshold is not None else resolved.missing_threshold
    if threshold is None:
        threshold = DEFAULT_MISSING_THRESHOLD

    search_tools = KbSearchTools(
        docs_dir=resolved.docs_dir,  # type: ignore[arg-type]
        work_index_path=resolved.work_index_path,  # type: ignore[arg-type]
        reference_index_path=resolved.reference_index_path,  # type: ignore[arg-type]
    )
    download_tools = KbDownloadTools(
        conn,
        root_dir=resolved.root_dir,
        docs_dir=resolved.docs_dir,
        reports_dir=resolved.reports_dir,
        missing_threshold=threshold,
    )
    visualize_tools = KbVisualizeTools(
        conn,
        docs_dir=resolved.docs_dir,  # type: ignore[arg-type]
        reports_dir=visualizations_dir(resolved.reports_dir),
        repo_root=resolved.root_dir,
    )
    job_tools = JobTools(
        conn,
        docs_dir=resolved.docs_dir,  # type: ignore[arg-type]
        app_db_path=resolved.app_db_path,  # type: ignore[arg-type]
        work_index_path=resolved.work_index_path,  # type: ignore[arg-type]
        reference_index_path=resolved.reference_index_path,  # type: ignore[arg-type]
        reports_dir=resolved.reports_dir,  # type: ignore[arg-type]
        repo_root=resolved.root_dir,
    )
    admin_tools = KbAdminTools(ServiceContainer(resolved))
    # 動画は `reports/` のベースをそのまま受け取る（`videos_dir` はこの層より内側で導出）
    video_tools = KbVideoTools(
        conn,
        docs_dir=resolved.docs_dir,  # type: ignore[arg-type]
        reports_dir=resolved.reports_dir,  # type: ignore[arg-type]
        repo_root=resolved.root_dir,
        job_tools=job_tools,
    )

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
        "list_scene_kinds": visualize_tools.list_scene_kinds,
        "check_visualize_deps": visualize_tools.check_visualize_deps,
        "render_scene": visualize_tools.render_scene,
        "list_visualizations": visualize_tools.list_visualizations,
        "get_visualization": visualize_tools.get_visualization,
        "start_run_batch": job_tools.start_run_batch,
        "start_download_esa_post": job_tools.start_download_esa_post,
        "start_download_esa_category": job_tools.start_download_esa_category,
        "start_download_esa_search": job_tools.start_download_esa_search,
        "start_download_web": job_tools.start_download_web,
        "start_render_scene": job_tools.start_render_scene,
        "job_status": job_tools.job_status,
        "cancel_job": job_tools.cancel_job,
        "get_batch": job_tools.get_batch,
        "list_corpora": job_tools.list_corpora,
        "system_status": job_tools.system_status,
        "start_render_video": job_tools.start_render_video,
        **kb_video_handlers(video_tools),
        **kb_admin_handlers(admin_tools),
    }

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            *kb_search_list_tools(),
            *kb_download_list_tools(),
            *kb_visualize_list_tools(),
            *jobs_list_tools(),
            *kb_video_list_tools(),
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
    settings: Settings | None = None,
    docs_dir: Path | None = None,
    work_index_path: Path | None = None,
    reference_index_path: Path | None = None,
    app_db_path: Path | None = None,
    root_dir: Path | None = None,
    reports_dir: Path | None = None,
    missing_threshold: int | None = None,
) -> Server[Any, Any]:
    """サーバー名から `Server` を組み立てる。

    `settings` を渡すと `--root` 配下の `.env` 資格情報をそのまま使う
    (CLI `mcp serve` の推奨経路)。パス引数だけ渡す場合は
    `load_settings(root=root_dir)` でルート配下の `.env` を読む。
    """
    if settings is not None:
        docs_dir = docs_dir or settings.docs_dir
        work_index_path = work_index_path or settings.work_index_path
        reference_index_path = reference_index_path or settings.reference_index_path
        app_db_path = app_db_path or settings.app_db_path
        root_dir = root_dir or settings.root_dir
        reports_dir = reports_dir or settings.reports_dir
        if missing_threshold is None:
            missing_threshold = settings.missing_threshold

    if name == "kb-search":
        if docs_dir is None or work_index_path is None or reference_index_path is None:
            raise ValueError("kb-search サーバーには docs_dir / index paths が必要です")
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
        return build_kb_admin_server(
            docs_dir=docs_dir,
            work_index_path=work_index_path,
            reference_index_path=reference_index_path,
            app_db_path=app_db_path,
            root_dir=root_dir,
            reports_dir=reports_dir,
            settings=settings,
        )
    if name == ALL_SERVER_NAME:
        return build_all_server(
            docs_dir=docs_dir,
            work_index_path=work_index_path,
            reference_index_path=reference_index_path,
            app_db_path=app_db_path,
            root_dir=root_dir,
            reports_dir=reports_dir,
            missing_threshold=missing_threshold,
            settings=settings,
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
