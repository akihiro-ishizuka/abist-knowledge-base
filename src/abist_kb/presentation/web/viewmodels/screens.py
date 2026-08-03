"""9画面(設計書 §7.1)の view-model。

各関数は `ServiceContainer` だけを受け取り、`application/` の戻り値
(dict/`Job`/`AppError`)を JSON 互換の dict へ組み立てて返す。Rich/Web 固有の
表示(色、罫線、HTML)はここに置かない — Web は `presentation/web/pages/*.py`
が、将来の Textual TUI は同じ関数をそのまま呼んで自分の描画コードへ渡す。

**チャット・可視化は M7 のサービスが無いためスタブ**(`available: False`)。
配線(ルーティング・画面・view-model の形)は本物であり、バックエンドだけが
未実装。品質(監査)も同じ理由でスタブ(監査4種は Task 7.2)。
"""

from __future__ import annotations

import shutil
import sqlite3
from typing import Any

from abist_kb.domain.errors import AppError
from abist_kb.domain.job import JobState

from .container import ServiceContainer
from .markdown_render import render_markdown_safe
from .serialize import error_to_dict, event_to_dict, job_to_dict
from .tokens import job_state_token


def _err(exc: AppError) -> dict[str, Any]:
    return {"error": error_to_dict(exc)}


# -- 1. ダッシュボード -------------------------------------------------------


def dashboard(container: ServiceContainer) -> dict[str, Any]:
    """文書数、同期状態、索引鮮度、直近ジョブ、警告(§7.1 画面1)。"""
    counts = container.documents.count_by_source()
    total_documents = sum(counts.values())

    index_status = container.index.status()
    index_corpora = index_status.get("corpora", {})

    recent_jobs = [job_to_dict(job) for job in container.jobs.list()[:10]]
    warnings: list[dict[str, Any]] = []
    for job in recent_jobs:
        # PARTIAL/FAILED/INTERRUPTED はダッシュボードで緑塗りにしない
        # (design/plans/M6-M10-remaining.md 共通申し送り: 一部失敗は PARTIAL)。
        if job["state"] in (
            str(JobState.PARTIAL),
            str(JobState.FAILED),
            str(JobState.INTERRUPTED),
        ):
            warnings.append(
                {
                    "job_id": job["id"],
                    "kind": job["kind"],
                    "state": job["state"],
                    "state_token": job["state_token"],
                }
            )

    return {
        "document_count": total_documents,
        "document_count_by_source": counts,
        "index_status": index_status,
        "index_corpora": index_corpora,
        "recent_jobs": recent_jobs,
        "warnings": warnings,
    }


# -- 2. ソース／バッチ -------------------------------------------------------


def sources_list(container: ServiceContainer) -> dict[str, Any]:
    return {"sources": container.sources.list()}


def batches_list(container: ServiceContainer) -> dict[str, Any]:
    return {"batches": container.batches.list()}


def batch_run(container: ServiceContainer, batch_id: str) -> dict[str, Any]:
    """バッチを実行する(§10.2: `docs-write` を全プロセス横断で直列化する)。"""
    try:
        result = container.batches.run(batch_id, owner_id=container.owner_id)
    except AppError as exc:
        return _err(exc)
    return {"job": result}


def source_test_connection(container: ServiceContainer, source_id: str) -> dict[str, Any]:
    try:
        return container.sources.test_connection(source_id)
    except AppError as exc:
        return _err(exc)


# -- 3. ジョブ ---------------------------------------------------------------


def jobs_list(container: ServiceContainer, *, state: str | None = None) -> dict[str, Any]:
    state_filter = JobState(state) if state else None
    jobs = container.jobs.list(state=state_filter)
    return {"jobs": [job_to_dict(job) for job in jobs]}


def job_detail(container: ServiceContainer, job_id: str) -> dict[str, Any]:
    try:
        job = container.jobs.get(job_id)
        history = container.jobs.history(job_id)
    except AppError as exc:
        return _err(exc)
    return {"job": job_to_dict(job), "history": [event_to_dict(evt) for evt in history]}


def job_cancel(container: ServiceContainer, job_id: str) -> dict[str, Any]:
    try:
        container.jobs.cancel(job_id)
    except AppError as exc:
        return _err(exc)
    return job_detail(container, job_id)


def job_retry(container: ServiceContainer, job_id: str) -> dict[str, Any]:
    try:
        job = container.jobs.retry(job_id)
    except AppError as exc:
        return _err(exc)
    return {"job": job_to_dict(job)}


# -- 4. 文書 -----------------------------------------------------------------


def documents_list(
    container: ServiceContainer,
    *,
    source: str | None = None,
    sync_status: str | None = None,
    status: str | None = None,
    path_prefix: str | None = None,
) -> dict[str, Any]:
    docs = container.documents.list(
        source=source, sync_status=sync_status, status=status, path_prefix=path_prefix
    )
    return {"documents": docs}


def document_detail(container: ServiceContainer, path: str) -> dict[str, Any]:
    try:
        record = container.documents.get(path)
    except AppError as exc:
        return _err(exc)

    file_path = container.settings.docs_dir / path
    body_html: str | None = None
    body_missing = False
    if file_path.is_file():
        try:
            text = file_path.read_text(encoding="utf-8")
            body_html = render_markdown_safe(text)
        except OSError:
            body_missing = True
    else:
        body_missing = True

    return {"document": record, "body_html": body_html, "body_missing": body_missing}


# -- 5. 検索 -----------------------------------------------------------------


def search(
    container: ServiceContainer,
    query: str,
    *,
    corpus: str = "work",
    source: str | None = None,
    document_type: str | None = None,
    status: str | None = None,
    path_prefix: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    try:
        return container.search.search(
            query,
            corpus=corpus,
            source=source,
            document_type=document_type,
            status=status,
            path_prefix=path_prefix,
            limit=limit,
        )
    except AppError as exc:
        return _err(exc)


# -- 6. チャット(M7 でサービスが到着するまでスタブ) ---------------------------


def chat_stub(_container: ServiceContainer) -> dict[str, Any]:
    """`ChatService` は M7(Task 7.1)で追加される。配線だけ先に切る。"""
    return {
        "available": False,
        "reason": "ChatService は M7 で実装予定です(design/plans/M6-M10-remaining.md Task 7.1)。",
    }


# -- 7. 可視化(同上) ----------------------------------------------------------


def visualization_stub(_container: ServiceContainer) -> dict[str, Any]:
    """`SceneSpec`/`render_scene` は M7(Task 7.3/7.4)で追加される。"""
    return {
        "available": False,
        "reason": (
            "SceneSpec 検証・Manim レンダリングは M7 で実装予定です"
            "(design/plans/M6-M10-remaining.md Task 7.3)。"
        ),
    }


# -- 8. 品質(監査4種は M7 Task 7.2 で追加されるまでスタブ) ---------------------


def quality_stub(_container: ServiceContainer) -> dict[str, Any]:
    return {
        "available": False,
        "reason": (
            "整合性/重複/矛盾/検索評価の監査は M7 で実装予定です"
            "(design/plans/M6-M10-remaining.md Task 7.2)。"
        ),
    }


# -- 9. 設定／診断 -------------------------------------------------------------


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS __fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE __fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def settings_diagnostics(container: ServiceContainer) -> dict[str, Any]:
    """パス、モデル、ffmpeg・Manim・FTS5診断(§7.1 画面9)。"""
    settings = container.settings
    return {
        "paths": {
            "root_dir": str(settings.root_dir),
            "docs_dir": str(settings.docs_dir),
            "reports_dir": str(settings.reports_dir),
            "data_dir": str(settings.data_dir),
            "app_db_path": str(settings.app_db_path),
            "work_index_path": str(settings.work_index_path),
            "reference_index_path": str(settings.reference_index_path),
        },
        "embedding_model": settings.embedding_model,
        "diagnostics": {
            "fts5_available": _fts5_available(container.conn),
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "manim_available": shutil.which("manim") is not None,
        },
    }


__all__ = [
    "batch_run",
    "batches_list",
    "chat_stub",
    "dashboard",
    "document_detail",
    "documents_list",
    "job_cancel",
    "job_detail",
    "job_retry",
    "jobs_list",
    "quality_stub",
    "search",
    "settings_diagnostics",
    "source_test_connection",
    "sources_list",
    "visualization_stub",
    "job_state_token",
]
