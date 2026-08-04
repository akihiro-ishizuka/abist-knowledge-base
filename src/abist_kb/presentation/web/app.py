"""NiceGUI アプリの組み立て(設計書 §7.1, §10.1)。

Web/デスクトップは同じ9画面をここで登録する。長時間稼働エントリポイントとして
起動時に `WorkerSupervisor` を開始する(§10.1: 「Web、デスクトップ、TUI、MCPの
各長時間稼働エントリポイントは起動時にこれを開始し」)。FastAPI `/api/v1`
(`presentation/api/app.py`)を同じ ASGI アプリへマウントし、SSE も同一プロセスで
提供する。
"""

from __future__ import annotations

import threading
from typing import Any

from nicegui import app as nicegui_app
from nicegui import ui

from abist_kb.presentation.api.app import register_api_routes
from abist_kb.presentation.web.pages import (
    chat,
    dashboard,
    documents,
    jobs,
    quality,
    search,
    settings_page,
    sources_batches,
    visualization,
)
from abist_kb.presentation.web.viewmodels.container import ServiceContainer


def register_pages(container: ServiceContainer) -> None:
    """`@ui.page` ルートを登録する(9画面 + ジョブ/文書の詳細ルート)。"""

    @ui.page("/")
    def _dashboard() -> None:
        dashboard.render(container)

    @ui.page("/sources")
    def _sources() -> None:
        sources_batches.render(container)

    @ui.page("/jobs")
    def _jobs_list() -> None:
        jobs.render_list(container)

    @ui.page("/jobs/{job_id}")
    def _jobs_detail(job_id: str) -> None:
        jobs.render_detail(container, job_id)

    @ui.page("/documents")
    def _documents_list() -> None:
        documents.render_list(container)

    @ui.page("/documents/{path:path}")
    def _documents_detail(path: str) -> None:
        documents.render_detail(container, path)

    @ui.page("/search")
    def _search() -> None:
        search.render(container)

    @ui.page("/chat")
    def _chat() -> None:
        chat.render(container)

    @ui.page("/visualization")
    def _visualization() -> None:
        visualization.render(container)

    @ui.page("/quality")
    def _quality() -> None:
        quality.render(container)

    @ui.page("/settings")
    def _settings() -> None:
        settings_page.render(container)


def build_web_app(
    container: ServiceContainer,
    *,
    bind_host: str = "127.0.0.1",
    access_token: str | None = None,
    start_worker: bool = True,
) -> Any:
    """NiceGUI アプリを組み立て、`/api/v1` をマウントし、9画面を登録する。

    `start_worker=True`(既定)で `WorkerSupervisor` をバックグラウンドスレッドで
    起動する(§10.1)。テストでは `start_worker=False` にしてリーダー選出の
    スレッドを増やさないようにできる。
    """
    # NiceGUI の `app`(`nicegui.app`)自体が FastAPI インスタンスであるため、
    # サブアプリを `mount()` せずルートを直接足す(`/api/v1/...` がそのまま公開される)。
    register_api_routes(nicegui_app, container, bind_host=bind_host, access_token=access_token)
    register_pages(container)

    if start_worker:
        supervisor = container.build_worker_supervisor()
        thread = threading.Thread(
            target=supervisor.run_forever, daemon=True, name="worker-supervisor"
        )
        thread.start()

    return nicegui_app


__all__ = ["build_web_app", "register_pages"]
