"""9画面(設計書 §7.1)の view-model。

各関数は `ServiceContainer` だけを受け取り、`application/` の戻り値
(dict/`Job`/`AppError`)を JSON 互換の dict へ組み立てて返す。Rich/Web 固有の
表示(色、罫線、HTML)はここに置かない — Web は `presentation/web/pages/*.py`
が、将来の Textual TUI は同じ関数をそのまま呼んで自分の描画コードへ渡す。

**可視化は次パス(M7 Task 7.3/7.4)のためスタブのまま**(`available: False`)。
チャット(Task 7.1)と品質監査4種(Task 7.2)は `ChatService`/`application.audit.*`
に配線済み。チャットは `openai_api_key` 未設定時のみ `chat_stub` がスタブへ
フォールバックする。
"""

from __future__ import annotations

import shutil
import sqlite3
from typing import Any

from abist_kb.domain.errors import AppError, ErrorCode
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


def batch_add(
    container: ServiceContainer,
    *,
    name: str,
    type: str,
    output_dir: str | None = None,
    enabled: bool = True,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        batch = container.batches.add(
            name=name,
            type=type,
            output_dir=output_dir,
            enabled=enabled,
            items=items,
        )
    except AppError as exc:
        return _err(exc)
    return {"batch": batch}


def batch_edit(
    container: ServiceContainer, batch_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    try:
        batch = container.batches.edit(batch_id, **fields)
    except AppError as exc:
        return _err(exc)
    return {"batch": batch}


def batch_remove(container: ServiceContainer, batch_id: str, *, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        return _err(
            AppError(
                code=ErrorCode.INVALID_INPUT,
                message="バッチを削除するには確認が必要です。",
            )
        )
    try:
        removed = container.batches.remove(batch_id, confirm=lambda _msg: confirmed)
    except AppError as exc:
        return _err(exc)
    return {"deleted": True} if removed else {"cancelled": True}


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


def source_add(
    container: ServiceContainer,
    *,
    type: str,
    display_name: str,
    connection: dict[str, Any] | None = None,
    output_dir: str,
    enabled: bool = True,
) -> dict[str, Any]:
    try:
        source = container.sources.add(
            type=type,
            display_name=display_name,
            connection=connection,
            output_dir=output_dir,
            enabled=enabled,
        )
    except AppError as exc:
        return _err(exc)
    return {"source": source}


def source_edit(
    container: ServiceContainer, source_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    try:
        source = container.sources.edit(source_id, **fields)
    except AppError as exc:
        return _err(exc)
    return {"source": source}


def source_remove(
    container: ServiceContainer, source_id: str, *, confirmed: bool
) -> dict[str, Any]:
    if not confirmed:
        return _err(
            AppError(
                code=ErrorCode.INVALID_INPUT,
                message="ソースを削除するには確認が必要です。",
            )
        )
    try:
        removed = container.sources.remove(source_id, confirm=lambda _msg: confirmed)
    except AppError as exc:
        return _err(exc)
    return {"deleted": True} if removed else {"cancelled": True}


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


def document_update_metadata(
    container: ServiceContainer, path: str, fields: dict[str, Any]
) -> dict[str, Any]:
    allowed_keys = {"status", "document_type"}
    disallowed_keys = set(fields) - allowed_keys
    if disallowed_keys:
        return _err(
            AppError(
                code=ErrorCode.INVALID_INPUT,
                message=(
                    "更新できない文書メタデータが指定されました: "
                    + ", ".join(sorted(disallowed_keys))
                ),
            )
        )
    filtered_fields = {key: value for key, value in fields.items() if key in allowed_keys}
    if not filtered_fields:
        return _err(
            AppError(
                code=ErrorCode.INVALID_INPUT,
                message="更新する文書メタデータを指定してください。",
            )
        )
    try:
        document = container.documents.update_metadata(path, filtered_fields)
    except AppError as exc:
        return _err(exc)
    return {"document": document}


def document_delete(container: ServiceContainer, path: str, *, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        return _err(
            AppError(
                code=ErrorCode.INVALID_INPUT,
                message="文書を削除するには確認が必要です。",
            )
        )
    try:
        deleted = container.documents.delete(path, confirm=lambda _msg: confirmed)
    except AppError as exc:
        return _err(exc)
    return {"deleted": True} if deleted else {"cancelled": True}


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


def chat_stub(container: ServiceContainer) -> dict[str, Any]:
    """`ChatService`(§7.1)。`openai_api_key` 未設定時のみスタブ表示にフォールバックする。"""
    if container.chat is not None:
        return {"available": True}
    return {
        "available": False,
        "reason": "OPENAI_API_KEY(または settings.toml の openai_api_key)が未設定です。",
    }


def chat_start(container: ServiceContainer, *, title: str | None = None) -> dict[str, Any]:
    chat = container.chat
    if chat is None:
        return _err(
            AppError(
                code=ErrorCode.CONFIG_ERROR,
                message="ChatService が利用できません(openai_api_key 未設定)。",
            )
        )
    return {"conversation_id": chat.start_conversation(title=title)}


def chat_ask(container: ServiceContainer, *, conversation_id: str, question: str) -> dict[str, Any]:
    """1問1答。**引用検証に失敗した引用は黙って落とさず `citation_warnings` に含める。**"""
    chat = container.chat
    if chat is None:
        return _err(
            AppError(
                code=ErrorCode.CONFIG_ERROR,
                message="ChatService が利用できません(openai_api_key 未設定)。",
            )
        )
    try:
        answer = chat.ask(conversation_id, question)
    except AppError as exc:
        return _err(exc)
    return {
        "conversation_id": answer.conversation_id,
        "message_id": answer.message_id,
        "text": answer.text,
        "citations": [
            {
                "path": c.path,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "valid": c.valid,
                "reason": c.reason,
            }
            for c in answer.citations
        ],
        "citation_warnings": list(answer.citation_warnings),
    }


def chat_history(container: ServiceContainer, *, conversation_id: str) -> dict[str, Any]:
    chat = container.chat
    if chat is None:
        return _err(
            AppError(
                code=ErrorCode.CONFIG_ERROR,
                message="ChatService が利用できません(openai_api_key 未設定)。",
            )
        )
    try:
        return {"messages": chat.history(conversation_id)}
    except AppError as exc:
        return _err(exc)


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
    """4種の監査は `quality_run_integrity`/`_duplicates`/`_contradictions`/
    `_backfill_metadata` として実装済み(§7.1 画面8)。この関数自体は
    「画面はあるがまだ何も実行していない」初期状態を返す(available は常に True)。
    """
    return {"available": True}


def quality_run_integrity(
    container: ServiceContainer, *, update_db: bool = False
) -> dict[str, Any]:
    """`audit integrity` と同じ処理(§12: Web からの既定は書き込まない = `update_db=False`)。"""
    from abist_kb.application.audit.verify_integrity import VerifyIntegrityService

    result = VerifyIntegrityService(container.conn, docs_dir=container.settings.docs_dir).run(
        update_db=update_db
    )
    return {"run_id": result.run_id, "totals": result.totals.as_dict(), "findings": result.findings}


def quality_run_duplicates(container: ServiceContainer, *, corpus: str = "work") -> dict[str, Any]:
    """`audit duplicates` と同じ処理。変更は一切行わない。"""
    from abist_kb.application.audit.find_duplicates import FindDuplicatesService
    from abist_kb.infrastructure.db.connection import connect

    index_path = (
        container.settings.work_index_path
        if corpus == "work"
        else container.settings.reference_index_path
    )
    index_conn = connect(index_path, read_only=True) if index_path.is_file() else None
    try:
        result = FindDuplicatesService(container.conn, index_conn=index_conn).run()
    finally:
        if index_conn is not None:
            index_conn.close()
    return {
        "run_id": result.run_id,
        "totals": result.totals(),
        "same_article": result.same_article,
        "identical": result.identical,
        "near": result.near,
    }


def quality_run_contradictions(container: ServiceContainer) -> dict[str, Any]:
    """`audit contradictions` と同じ処理。変更は一切行わない。"""
    from abist_kb.application.audit.check_contradictions import CheckContradictionsService

    result = CheckContradictionsService(container.conn, docs_dir=container.settings.docs_dir).run()
    return {
        "run_id": result.run_id,
        "candidate_pair_count": result.candidate_pair_count,
        "candidates": [
            {"path_a": c.path_a, "path_b": c.path_b, "sources": c.sources, "conflicts": c.conflicts}
            for c in result.candidates
        ],
    }


def quality_run_backfill_metadata(
    container: ServiceContainer, *, apply: bool = False
) -> dict[str, Any]:
    """`audit backfill-metadata` と同じ処理。**既定は dry-run。**

    Web から `apply=True` を呼ぶ経路は §12 の作法(対象提示→確認→`--yes`相当)を
    満たす確認 UI が別途必要なため、このタスクでは dry-run のみを画面から
    実行可能にする(`apply=True` は CLI の `--apply` + 確認プロンプトを使うこと)。
    """
    from abist_kb.application.audit.backfill_metadata import BackfillMetadataService

    if apply:
        return {
            "error": {
                "message": "Web からの --apply 実行は未対応です。CLI の `abist-kb audit "
                "backfill-metadata --apply` を使ってください。"
            }
        }
    result = BackfillMetadataService(container.conn, docs_dir=container.settings.docs_dir).run(
        apply=False
    )
    return {
        "run_id": result.run_id,
        "mode": result.mode,
        "totals": result.totals.as_dict(),
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
    "batch_add",
    "batch_edit",
    "batch_remove",
    "batch_run",
    "batches_list",
    "chat_ask",
    "chat_history",
    "chat_start",
    "chat_stub",
    "dashboard",
    "document_delete",
    "document_detail",
    "document_update_metadata",
    "documents_list",
    "job_cancel",
    "job_detail",
    "job_retry",
    "jobs_list",
    "quality_run_backfill_metadata",
    "quality_run_contradictions",
    "quality_run_duplicates",
    "quality_run_integrity",
    "quality_stub",
    "search",
    "settings_diagnostics",
    "source_add",
    "source_edit",
    "source_remove",
    "source_test_connection",
    "sources_list",
    "visualization_stub",
    "job_state_token",
]
