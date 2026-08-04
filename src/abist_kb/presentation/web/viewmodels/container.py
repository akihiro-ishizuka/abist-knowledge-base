"""`ServiceContainer`: Web/デスクトップ/TUI が共有するアプリケーションサービスの束(§5, §8)。

CLI が各コマンド内で毎回 `open_app_db` + 個別サービス構築を行っているのと同じ配線を
1箇所へ集約する。Web(NiceGUI/FastAPI)と将来の Textual TUI はどちらもこの
`ServiceContainer` を経由してのみ `application/` へアクセスし、CLI・MCP と同じ
状態・件数・エラーコードを表示する(design/system-design.md §5, §7.1)。

**ここに UI 固有の概念(色、記号、HTML)を混ぜない。** view-model 層
(`viewmodels/*.py`)はこの Container が返す辞書/`Job`/`AppError` をそのまま
JSON 互換の値として返し、色・記号の割当は `presentation/web/theme.py`
(Web)や将来の TUI 側テーマモジュールの責務とする。
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from abist_kb.application.batch_service import (
    BUILTIN_BATCH_HANDLERS,
    BUILTIN_BATCH_RESOURCES,
    BatchService,
)
from abist_kb.application.chat_service import ChatService
from abist_kb.application.document_service import DocumentService
from abist_kb.application.index_service import IndexService, run_index_inline
from abist_kb.application.job_service import JobService
from abist_kb.application.search_service import SearchService
from abist_kb.application.source_service import SourceService
from abist_kb.application.sync_service import run_sync_inline
from abist_kb.config import Settings
from abist_kb.infrastructure.ai.chat_provider import OpenAIChatProvider
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.infrastructure.jobs import events as events_mod
from abist_kb.infrastructure.jobs.supervisor import JobHandler, WorkerSupervisor

#: Web の常駐ワーカー(`WorkerSupervisor`)がキューから消費できるジョブ種別。
#: `noop`(基盤の疎通確認) + `batch`(バッチ実行)。sync/index-build/index-embed は
#: CLI と同じく操作の都度 `run_sync_inline`/`run_index_inline` がその場で専用の
#: `JobService` を組み立てて `docs-write`/`corpus-write:<corpus>` リースを取るため
#: (`application/sync_service.py::run_sync_inline`, `application/index_service.py::
#: run_index_inline` を参照)、常駐ワーカーの静的ハンドラ表には含めない。CLI の
#: `worker run`(`presentation/cli/worker_cmd.py`)も同じ理由で `noop` のみを
#: 常駐ハンドラとして登録しており、ここではそれに `batch` を加える。
WEB_WORKER_HANDLERS: dict[str, JobHandler] = dict(BUILTIN_BATCH_HANDLERS)
WEB_WORKER_RESOURCES: dict[str, Any] = dict(BUILTIN_BATCH_RESOURCES)


def _noop_handler(run: Any) -> None:
    run.emit(phase="noop", current=1, total=1, message="ノーオペレーション完了")


WEB_WORKER_HANDLERS["noop"] = _noop_handler


class ServiceContainer:
    """1つの `app.sqlite` 接続と全アプリケーションサービスをまとめる。

    プロセス生存期間中は1つの Container を使い回す想定(NiceGUI/FastAPI の
    起動時に1つ作り、リクエストハンドラ間で共有する)。将来の Textual TUI も
    同じクラスを import して同じ画面状態を再現できる。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        owner_id: str | None = None,
        check_same_thread: bool = True,
    ) -> None:
        """`check_same_thread=False` は ASGI 層(FastAPI/NiceGUI)専用。

        Starlette の `TestClient`/実運用の ASGI サーバーは、この接続を作った
        スレッドとは別スレッドでリクエストを処理しうる
        (`infrastructure/db/connection.py::connect` の docstring 参照)。
        Web(`presentation/web/app.py`)はこの値を `False` で渡し、代わりに
        `presentation/api/app.py` の `asyncio.Lock` で全リクエストのDBアクセスを
        直列化することで、単一接続への同時アクセスを防ぐ。CLI/TUI/テストのような
        単一スレッド利用では既定の `True`(スレッド越境を誤って許してしまうバグを
        検出できる状態)のままにする。
        """
        self.settings = settings
        self.owner_id = owner_id or str(uuid.uuid4())
        self.conn: sqlite3.Connection = open_app_db(
            settings.app_db_path, check_same_thread=check_same_thread
        )
        self.event_bus = events_mod.EventBus()

        self.sources = SourceService(self.conn)
        self.batches = BatchService(self.conn)
        self.documents = DocumentService(self.conn)
        self.search = SearchService(
            docs_dir=settings.docs_dir,
            work_index_path=settings.work_index_path,
            reference_index_path=settings.reference_index_path,
        )
        self.index = IndexService(
            docs_dir=settings.docs_dir,
            app_db_path=settings.app_db_path,
            work_index_path=settings.work_index_path,
            reference_index_path=settings.reference_index_path,
        )
        # 参照専用: `jobs` 画面の一覧・詳細・履歴・キャンセル・再試行はジョブ種別に
        # 依存しない(`JobRepository`/`job_events` はどのハンドラが書いたジョブでも
        # 同じ形で読める)。インライン実行(`run_sync_inline`/`run_index_inline`/
        # `BatchService.run`)はそれぞれ専用の一時的な `JobService` を都度組み立てる
        # ため、この `jobs` には handlers を登録しない(投入しても実行されない
        # ジョブを作らないため)。
        self.jobs = JobService(self.conn, owner_id=self.owner_id, event_bus=self.event_bus)
        self._chat: ChatService | None = None

    @property
    def chat(self) -> ChatService | None:
        """`ChatService`(§7.1)。`openai_api_key` が未設定なら `None`(画面はスタブ表示)。

        `SearchService` を根拠取得に、`OpenAIChatProvider` をベンダー実装として使う
        (`ChatProvider` 境界のおかげで差し替え可能。実 API へは `openai_api_key`
        が設定されているときのみ到達する)。
        """
        if self._chat is not None:
            return self._chat
        if not self.settings.openai_api_key:
            return None
        provider = OpenAIChatProvider(
            model=self.settings.chat_model, api_key=self.settings.openai_api_key
        )
        self._chat = ChatService(
            self.conn,
            search_service=self.search,
            provider=provider,
            docs_dir=self.settings.docs_dir,
        )
        return self._chat

    def run_sync(
        self,
        *,
        target: str,
        target_id: str | None = None,
        categories: list[str] | None = None,
        force: bool = False,
        dry_run: bool = False,
        prune_orphans: bool = False,
    ) -> dict[str, Any]:
        return run_sync_inline(
            self.conn,
            root_dir=self.settings.root_dir,
            docs_dir=self.settings.docs_dir,
            reports_dir=self.settings.reports_dir,
            missing_threshold=self.settings.missing_threshold,
            target=target,
            target_id=target_id,
            categories=categories,
            force=force,
            dry_run=dry_run,
            prune_orphans=prune_orphans,
            owner_id=self.owner_id,
        )

    def run_index(self, *, action: str, corpus: str) -> dict[str, Any]:
        return run_index_inline(
            self.index, self.conn, action=action, corpus=corpus, owner_id=self.owner_id
        )

    def build_worker_supervisor(self) -> WorkerSupervisor:
        """長時間稼働エントリポイント(Web)用の `WorkerSupervisor`(§10.1)。

        Web は他の常駐エントリポイント(デスクトップ/TUI/MCP)と同じく起動時に
        これを開始し、リーダー選出に参加する。専用接続を使う
        (`WorkerSupervisor` はバックグラウンドスレッドで heartbeat 更新するため、
        リクエスト処理用の `self.conn` と共有しない)。
        """
        supervisor_conn = open_app_db(self.settings.app_db_path)
        return WorkerSupervisor(
            supervisor_conn,
            owner_id=self.owner_id,
            handlers=WEB_WORKER_HANDLERS,
            resource_for_kind=WEB_WORKER_RESOURCES,
        )

    def close(self) -> None:
        self.conn.close()


def build_container(root_dir: Path | None = None, **overrides: Any) -> ServiceContainer:
    """`Settings` を組み立てて `ServiceContainer` を返す(Web/TUI 共通の入口)。"""
    settings = (
        Settings(root_dir=root_dir, **overrides) if root_dir is not None else Settings(**overrides)
    )
    settings.ensure_directories()
    return ServiceContainer(settings)


__all__ = ["ServiceContainer", "build_container"]
