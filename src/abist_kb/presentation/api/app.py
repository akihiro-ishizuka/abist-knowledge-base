"""FastAPI `/api/v1` 層(設計書 §7.1, §10.3): NiceGUI 内蔵アプリへ統合する。

CLI/MCP と同じ `ServiceContainer`(`presentation/web/viewmodels/container.py`)
の上に薄い HTTP 層を被せるだけで、業務ロジックはここに一切書かない。
ジョブ進捗は `ProgressEvent` バス(`infrastructure/jobs/events.py`)を購読する
SSE で配信する。

既定バインドは呼び出し側(`presentation/cli/ui_cmd.py`)が `127.0.0.1` を渡す。
`0.0.0.0` 等の非ループバック公開時はアクセストークンを必須にする
(`presentation/api/auth.py::require_token_when_exposed`)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from abist_kb.domain.errors import AppError, ErrorCode, http_status_for
from abist_kb.domain.job import TERMINAL_STATES
from abist_kb.presentation.api.auth import AccessTokenMiddleware, require_token_when_exposed
from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer
from abist_kb.presentation.web.viewmodels.serialize import error_to_dict, event_to_dict

T = TypeVar("T")


class BatchCreateRequest(BaseModel):
    name: str
    type: str
    output_dir: str | None = None
    enabled: bool = True
    items: list[dict[str, Any]] | None = None


class BatchEditRequest(BaseModel):
    name: str | None = None
    type: str | None = None
    output_dir: str | None = None
    enabled: bool | None = None
    items: list[dict[str, Any]] | None = None


class SourceCreateRequest(BaseModel):
    type: str
    display_name: str
    connection: dict[str, Any] | None = None
    output_dir: str
    enabled: bool = True


class SourceEditRequest(BaseModel):
    type: str | None = None
    display_name: str | None = None
    connection: dict[str, Any] | None = None
    output_dir: str | None = None
    enabled: bool | None = None


class DocumentMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    document_type: str | None = None


class ChatStartRequest(BaseModel):
    title: str | None = None


class ChatAskRequest(BaseModel):
    conversation_id: str
    question: str


class QualityIntegrityRequest(BaseModel):
    update_db: bool = False


class QualityDuplicatesRequest(BaseModel):
    corpus: str = "work"


class QualityContradictionsRequest(BaseModel):
    pass


class QualityBackfillMetadataRequest(BaseModel):
    apply: bool = False


class VisualizationValidateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene_spec: dict[str, Any]


class VisualizationRenderRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene_spec: dict[str, Any]
    slug: str | None = None


def _status_for(err: AppError) -> int:
    return http_status_for(err.code)


def _sse_line(payload: dict[str, Any]) -> str:
    import json

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def register_api_routes(
    app: FastAPI,
    container: ServiceContainer,
    *,
    bind_host: str = "127.0.0.1",
    access_token: str | None = None,
    enforce_token_requirement: bool = True,
) -> FastAPI:
    """`/api/v1` ルートを既存の FastAPI アプリへ登録する。

    Web(`presentation/web/app.py`)は NiceGUI 自身の FastAPI インスタンスへ
    これを直接呼ぶ(サブアプリを `mount()` するとパスプレフィックスがずれるため、
    同一アプリへルートを足す方式にしている)。単体テスト・スタンドアロン用途は
    `create_api_app()` が新規 `FastAPI()` を作ってこれを呼ぶ。

    `enforce_token_requirement=True`(既定)では、非ループバック `bind_host` で
    `access_token` 未設定だと起動時に `ValueError` になる(フェイルセーフ)。
    テストでインメモリ DB を使い `bind_host="0.0.0.0"` を試したい場合など、
    意図的にトークン検証だけ迂回したいときは `False` を渡す。
    """
    if enforce_token_requirement:
        require_token_when_exposed(bind_host=bind_host, access_token=access_token)

    app.state.container = container
    # `ServiceContainer.conn` は1つの sqlite3 接続をプロセス寿命ぶん共有する
    # (§10.2 の `resource_leases` と同じ「1プロセス内は直列化」という前提を
    # HTTP リクエスト間にも適用する)。ASGI サーバーは並行リクエストを単一の
    # イベントループスレッド上で処理するため真の並列アクセスにはならないが、
    # `await` をまたぐ処理(SSE のポーリングループ)が挟まると別リクエストの
    # DB アクセスと論理的に交互実行されうる。`transaction()`(`BEGIN IMMEDIATE`)
    # の途中に他リクエストの SELECT が割り込まないよう、DB へ触れる区間はこの
    # ロックで直列化する(`presentation/web/viewmodels/container.py` の
    # `check_same_thread=False` の docstring と対になる対策)。
    app.state.db_lock = asyncio.Lock()
    app.add_middleware(AccessTokenMiddleware, bind_host=bind_host, access_token=access_token)

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(error_to_dict(exc), status_code=_status_for(exc))

    def get_container(request: Request) -> ServiceContainer:
        return request.app.state.container  # type: ignore[no-any-return]

    async def run_locked(request: Request, fn: Callable[[], T]) -> T | JSONResponse:
        """`fn()` を DB ロック配下で実行し、view-model の `{"error": ...}` 規約を
        HTTP ステータスへ変換する。

        `screens.py` の各関数は `AppError` を re-raise せず `{"error":
        error_to_dict(exc)}` へ変換して返す(NiceGUI の画面側がそのまま
        エラーメッセージを描画できるようにするため)。そのため FastAPI の
        `AppError` 例外ハンドラだけでは NOT_FOUND 等を検知できず、ここで
        戻り値の形を見て `_status_for` 相当のステータスへ変換する。
        """
        async with request.app.state.db_lock:
            result = fn()
        if isinstance(result, dict) and set(result.keys()) == {"error"}:
            error_payload = result["error"]
            try:
                code = ErrorCode(error_payload.get("code"))
            except ValueError:
                code = ErrorCode.FAILURE
            return JSONResponse(error_payload, status_code=http_status_for(code))
        return result

    router_prefix = "/api/v1"

    @app.get(f"{router_prefix}/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get(f"{router_prefix}/dashboard")
    async def get_dashboard(request: Request) -> dict[str, Any]:
        return await run_locked(request, lambda: screens.dashboard(get_container(request)))

    @app.get(f"{router_prefix}/sources")
    async def list_sources(request: Request) -> dict[str, Any]:
        return await run_locked(request, lambda: screens.sources_list(get_container(request)))

    @app.post(f"{router_prefix}/sources")
    async def add_source(body: SourceCreateRequest, request: Request) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.source_add(get_container(request), **body.model_dump()),
        )

    @app.patch(f"{router_prefix}/sources/{{source_id}}")
    async def edit_source(
        source_id: str, body: SourceEditRequest, request: Request
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.source_edit(
                get_container(request),
                source_id,
                body.model_dump(exclude_unset=True),
            ),
        )

    @app.delete(f"{router_prefix}/sources/{{source_id}}")
    async def remove_source(
        source_id: str, request: Request, confirmed: bool = False
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.source_remove(get_container(request), source_id, confirmed=confirmed),
        )

    @app.post(f"{router_prefix}/sources/{{source_id}}/test")
    async def test_source(source_id: str, request: Request) -> dict[str, Any]:
        return await run_locked(
            request, lambda: screens.source_test_connection(get_container(request), source_id)
        )

    @app.get(f"{router_prefix}/batches")
    async def list_batches(request: Request) -> dict[str, Any]:
        return await run_locked(request, lambda: screens.batches_list(get_container(request)))

    @app.post(f"{router_prefix}/batches")
    async def add_batch(body: BatchCreateRequest, request: Request) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.batch_add(get_container(request), **body.model_dump()),
        )

    @app.patch(f"{router_prefix}/batches/{{batch_id}}")
    async def edit_batch(batch_id: str, body: BatchEditRequest, request: Request) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.batch_edit(
                get_container(request),
                batch_id,
                body.model_dump(exclude_unset=True),
            ),
        )

    @app.delete(f"{router_prefix}/batches/{{batch_id}}")
    async def remove_batch(
        batch_id: str, request: Request, confirmed: bool = False
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.batch_remove(get_container(request), batch_id, confirmed=confirmed),
        )

    @app.post(f"{router_prefix}/batches/{{batch_id}}/run")
    async def run_batch(batch_id: str, request: Request) -> dict[str, Any]:
        return await run_locked(
            request, lambda: screens.batch_run(get_container(request), batch_id)
        )

    @app.get(f"{router_prefix}/jobs")
    async def list_jobs(request: Request, state: str | None = None) -> dict[str, Any]:
        return await run_locked(
            request, lambda: screens.jobs_list(get_container(request), state=state)
        )

    @app.get(f"{router_prefix}/jobs/{{job_id}}")
    async def get_job(job_id: str, request: Request) -> dict[str, Any]:
        return await run_locked(request, lambda: screens.job_detail(get_container(request), job_id))

    @app.post(f"{router_prefix}/jobs/{{job_id}}/cancel")
    async def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        return await run_locked(request, lambda: screens.job_cancel(get_container(request), job_id))

    @app.post(f"{router_prefix}/jobs/{{job_id}}/retry")
    async def retry_job(job_id: str, request: Request) -> dict[str, Any]:
        return await run_locked(request, lambda: screens.job_retry(get_container(request), job_id))

    @app.get(f"{router_prefix}/jobs/{{job_id}}/events")
    async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
        """SSE: `ProgressEvent` バス(§10.3)を購読し、既存履歴 → ライブ更新の順で流す。"""
        cont = get_container(request)
        lock: asyncio.Lock = request.app.state.db_lock
        async with lock:
            cont.jobs.get(job_id)  # NOT_FOUND を先に出す(AppError -> exception handler)

        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_event(event: Any) -> None:
            if event.job_id == job_id:
                loop.call_soon_threadsafe(queue.put_nowait, event)

        unsubscribe = cont.event_bus.subscribe(on_event)

        async def generator() -> Any:
            try:
                async with lock:
                    history = cont.jobs.history(job_id)
                for evt in history:
                    yield _sse_line(event_to_dict(evt))
                async with lock:
                    job = cont.jobs.get(job_id)
                if job.state in TERMINAL_STATES:
                    return
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield _sse_line(event_to_dict(evt))
                    async with lock:
                        job = cont.jobs.get(job_id)
                    if job.state in TERMINAL_STATES:
                        break
            finally:
                unsubscribe()

        return StreamingResponse(generator(), media_type="text/event-stream")

    @app.get(f"{router_prefix}/documents")
    async def list_documents(
        request: Request,
        source: str | None = None,
        sync_status: str | None = None,
        status: str | None = None,
        path_prefix: str | None = None,
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.documents_list(
                get_container(request),
                source=source,
                sync_status=sync_status,
                status=status,
                path_prefix=path_prefix,
            ),
        )

    @app.get(f"{router_prefix}/documents/{{path:path}}")
    async def get_document(path: str, request: Request) -> dict[str, Any]:
        return await run_locked(
            request, lambda: screens.document_detail(get_container(request), path)
        )

    @app.patch(f"{router_prefix}/documents/{{path:path}}")
    async def update_document_metadata(
        path: str, body: DocumentMetadataRequest, request: Request
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.document_update_metadata(
                get_container(request),
                path,
                body.model_dump(exclude_unset=True),
            ),
        )

    @app.delete(f"{router_prefix}/documents/{{path:path}}")
    async def delete_document(
        path: str, request: Request, confirmed: bool = False
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.document_delete(get_container(request), path, confirmed=confirmed),
        )

    @app.get(f"{router_prefix}/search")
    async def search(
        request: Request,
        q: str,
        corpus: str = "work",
        source: str | None = None,
        document_type: str | None = None,
        status: str | None = None,
        path_prefix: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.search(
                get_container(request),
                q,
                corpus=corpus,
                source=source,
                document_type=document_type,
                status=status,
                path_prefix=path_prefix,
                limit=limit,
            ),
        )

    @app.get(f"{router_prefix}/chat")
    async def chat(request: Request) -> dict[str, Any]:
        return await run_locked(request, lambda: screens.chat_stub(get_container(request)))

    @app.post(f"{router_prefix}/chat/start")
    async def start_chat(body: ChatStartRequest, request: Request) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.chat_start(get_container(request), title=body.title),
        )

    @app.post(f"{router_prefix}/chat/ask")
    async def ask_chat(body: ChatAskRequest, request: Request) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.chat_ask(
                get_container(request),
                conversation_id=body.conversation_id,
                question=body.question,
            ),
        )

    @app.get(f"{router_prefix}/chat/{{conversation_id}}/history")
    async def chat_history(conversation_id: str, request: Request) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.chat_history(
                get_container(request),
                conversation_id=conversation_id,
            ),
        )

    @app.get(f"{router_prefix}/visualization/deps")
    async def visualization_deps(request: Request) -> dict[str, Any]:
        return await run_locked(
            request, lambda: screens.visualization_deps(get_container(request))
        )

    @app.post(f"{router_prefix}/visualization/validate")
    async def visualization_validate(
        body: VisualizationValidateRequest, request: Request
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.visualization_validate(get_container(request), body.scene_spec),
        )

    @app.post(f"{router_prefix}/visualization/render")
    async def visualization_render(
        body: VisualizationRenderRequest, request: Request
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.visualization_submit_render(
                get_container(request), body.scene_spec, slug=body.slug
            ),
        )

    @app.get(f"{router_prefix}/quality")
    async def quality(request: Request) -> dict[str, Any]:
        return await run_locked(request, lambda: screens.quality_stub(get_container(request)))

    @app.post(f"{router_prefix}/quality/integrity")
    async def run_quality_integrity(
        body: QualityIntegrityRequest, request: Request
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.quality_run_integrity(
                get_container(request),
                update_db=body.update_db,
            ),
        )

    @app.post(f"{router_prefix}/quality/duplicates")
    async def run_quality_duplicates(
        body: QualityDuplicatesRequest, request: Request
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.quality_run_duplicates(
                get_container(request),
                corpus=body.corpus,
            ),
        )

    @app.post(f"{router_prefix}/quality/contradictions")
    async def run_quality_contradictions(
        _body: QualityContradictionsRequest, request: Request
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.quality_run_contradictions(get_container(request)),
        )

    @app.post(f"{router_prefix}/quality/backfill-metadata")
    async def run_quality_backfill_metadata(
        body: QualityBackfillMetadataRequest, request: Request
    ) -> dict[str, Any]:
        return await run_locked(
            request,
            lambda: screens.quality_run_backfill_metadata(
                get_container(request),
                apply=body.apply,
            ),
        )

    @app.get(f"{router_prefix}/settings/diagnostics")
    async def diagnostics(request: Request) -> dict[str, Any]:
        return await run_locked(
            request, lambda: screens.settings_diagnostics(get_container(request))
        )

    return app


def create_api_app(
    container: ServiceContainer,
    *,
    bind_host: str = "127.0.0.1",
    access_token: str | None = None,
    enforce_token_requirement: bool = True,
) -> FastAPI:
    """スタンドアロンの `/api/v1` FastAPI アプリ(テスト・独立起動用)。"""
    app = FastAPI(title="ABIST Knowledge Base API", version="1.0.0")
    return register_api_routes(
        app,
        container,
        bind_host=bind_host,
        access_token=access_token,
        enforce_token_requirement=enforce_token_requirement,
    )


__all__ = ["create_api_app", "register_api_routes"]
