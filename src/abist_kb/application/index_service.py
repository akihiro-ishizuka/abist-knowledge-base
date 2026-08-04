"""索引の構築・埋め込み生成・状態確認(設計書 §9.2、task-4-brief Step1)。

`IndexService.build(corpus)`/`.embed(corpus)` はどちらも `corpus-write:<corpus>`
リソースリースの下で永続ジョブとして実行する(`infrastructure.jobs` 経由)。
索引DB(`work-index.sqlite`/`reference-index.sqlite`)への書込ループは
`infrastructure.search.indexer.IndexBuilder.build`/
`infrastructure.ai.embedding_provider.generate_embeddings` に委譲しており、
どちらも内部で `check_lease()` を呼ぶ(brief: 「ループでは check_lease() を呼ぶ」)。

`.status()` は読み取り専用で、ジョブ/リースを経由しない。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.job import ResourceKind
from abist_kb.infrastructure.ai.embedding_provider import (
    LocalEmbeddingProvider,
    generate_embeddings,
)
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.jobs.supervisor import JobRunContext
from abist_kb.infrastructure.search.corpus import select_reference_targets, select_work_targets
from abist_kb.infrastructure.search.index_schema import (
    DEFAULT_TOKENIZERS,
    create_index_schema,
    fts_table_name,
)
from abist_kb.infrastructure.search.indexer import IndexBuilder

#: `index_status`(MCP `index_status` fixture)に合わせた表示ラベル。
CORPUS_LABELS: dict[str, str] = {
    "work": "実務資料",
    "reference": "CATIA原本（B32doc）",
}

CORPORA: tuple[str, ...] = ("work", "reference")


def _require_corpus(corpus: str) -> None:
    if corpus not in CORPORA:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"未知のコーパスです: {corpus!r}(有効値: {', '.join(CORPORA)})",
            exit_code=ExitCode.INVALID_INPUT,
        )


class IndexService:
    """work/reference の2索引DBに対する構築・埋め込み生成・状態確認。"""

    def __init__(
        self,
        *,
        docs_dir: Path,
        app_db_path: Path,
        work_index_path: Path,
        reference_index_path: Path,
    ) -> None:
        self._docs_dir = docs_dir
        self._app_db_path = app_db_path
        self._index_paths: dict[str, Path] = {
            "work": work_index_path,
            "reference": reference_index_path,
        }

    def index_path(self, corpus: str) -> Path:
        _require_corpus(corpus)
        return self._index_paths[corpus]

    # -- 構築・埋め込み生成(ジョブハンドラ内部から呼ばれる純粋な処理) -------------

    def build(
        self,
        corpus: str,
        *,
        emit: Callable[..., None] | None = None,
        check_lease: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """`corpus` を再走査し、索引DBへ差分反映する。呼び出し元がリースを保持すること。"""
        _require_corpus(corpus)
        index_path = self._index_paths[corpus]
        conn = connect(index_path)
        try:
            create_index_schema(conn, DEFAULT_TOKENIZERS)
            if corpus == "work":
                app_conn = connect(self._app_db_path, read_only=True)
                try:
                    documents = DocumentRepository(app_conn).list()
                finally:
                    app_conn.close()
                selection = select_work_targets(documents, self._docs_dir)
                rows = selection.targets
                disk_only_count = selection.disk_only_count
                disk_only_paths = list(selection.disk_only_paths)
            else:
                selection = select_reference_targets(self._docs_dir)
                rows = selection.targets
                disk_only_count = 0
                disk_only_paths = []

            builder = IndexBuilder(conn, tokenizers=DEFAULT_TOKENIZERS)
            summary = builder.build(rows, self._docs_dir, emit=emit, check_lease=check_lease)
        finally:
            conn.close()

        return {
            "corpus": corpus,
            "summary": asdict(summary),
            "disk_only_count": disk_only_count,
            # 全件保持すると B32doc 除く1,317件のような大きな一覧が結果に混じるため、
            # 呼び出し側(CLI/MCP)への提示は先頭100件に留める。件数自体は
            # disk_only_count で必ず可視化する(corpus.py モジュール docstring の
            # 「黙って落とさない」契約はここで満たす)。
            "disk_only_paths_sample": disk_only_paths[:100],
        }

    def embed(
        self,
        corpus: str,
        *,
        emit: Callable[..., None] | None = None,
        check_lease: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """`corpus` の埋め込みを差分生成する(`reference` は常に0件、設計どおり)。"""
        _require_corpus(corpus)
        index_path = self._index_paths[corpus]
        conn = connect(index_path)
        try:
            create_index_schema(conn, DEFAULT_TOKENIZERS)
            with LocalEmbeddingProvider() as provider:
                summary = generate_embeddings(
                    conn, provider, corpus=corpus, emit=emit, check_lease=check_lease
                )
        finally:
            conn.close()
        return {"corpus": corpus, "summary": asdict(summary)}

    # -- 状態確認(読み取り専用、ジョブ不要) ------------------------------------

    def status(self) -> dict[str, Any]:
        """2コーパスの索引状態(MCP `index_status` の情報に対応、キーはスネークケース)。"""
        corpora: dict[str, Any] = {}
        for corpus, path in self._index_paths.items():
            corpora[corpus] = self._corpus_status(corpus, path)
        return {"default_corpus": "work", "corpora": corpora}

    def _corpus_status(self, corpus: str, path: Path) -> dict[str, Any]:
        label = CORPUS_LABELS[corpus]
        if not path.is_file():
            return {
                "available": False,
                "label": label,
                "documents": 0,
                "chunks": 0,
                "embedded_chunks": 0,
                "tokenizers": [],
                "last_indexed_at": None,
                "chunk_options": {},
                "embedding_model": None,
                "index_size_mb": 0.0,
                "vector_search_available": False,
            }

        conn = connect(path, read_only=True)
        try:
            documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            embedded_chunks = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            meta = {
                row["key"]: row["value"]
                for row in conn.execute("SELECT key, value FROM meta").fetchall()
            }
        finally:
            conn.close()

        tokenizers_raw = meta.get("tokenizers")
        tokenizers = json.loads(tokenizers_raw) if tokenizers_raw else list(DEFAULT_TOKENIZERS)
        chunk_options_raw = meta.get("chunk_options")
        chunk_options = json.loads(chunk_options_raw) if chunk_options_raw else {}
        index_size_mb = round(path.stat().st_size / (1024 * 1024), 1)

        return {
            "available": True,
            "label": label,
            "documents": documents,
            "chunks": chunks,
            "embedded_chunks": embedded_chunks,
            "tokenizers": tokenizers,
            "last_indexed_at": meta.get("last_indexed_at"),
            "chunk_options": chunk_options,
            "embedding_model": meta.get("embedding_model"),
            "index_size_mb": index_size_mb,
            "vector_search_available": embedded_chunks > 0,
        }


# --------------------------------------------------------------------------
# ジョブ基盤への配線(`corpus-write:<corpus>` リースの下で実行する、brief Step1)
# --------------------------------------------------------------------------


def make_index_job_handler(*, service: IndexService, outbox: dict[str, Any]) -> Any:
    """`JobService(handlers={"index-build": ..., "index-embed": ...})` へ渡すハンドラ。

    `params` は `{"action": "build"|"embed", "corpus": "work"|"reference"}`。
    """

    def handler(run: JobRunContext) -> None:
        params = run.job.params
        action = params["action"]
        corpus = params["corpus"]
        if action == "build":
            outbox["result"] = service.build(corpus, emit=run.emit, check_lease=run.check_lease)
        elif action == "embed":
            outbox["result"] = service.embed(corpus, emit=run.emit, check_lease=run.check_lease)
        else:
            raise AppError(code=ErrorCode.INVALID_INPUT, message=f"未知の索引操作です: {action}")

    return handler


def run_index_inline(
    service: IndexService,
    conn: Any,
    *,
    action: str,
    corpus: str,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """CLI 既定の索引操作実行: `corpus-write:<corpus>` リースの下でジョブ経由に実行する。"""
    # ローカル import: sync_service.run_sync_inline と同じ理由(循環回避)。
    from abist_kb.application.job_service import JobService

    _require_corpus(corpus)
    outbox: dict[str, Any] = {}
    handler = make_index_job_handler(service=service, outbox=outbox)
    kind = f"index-{action}"
    job_service = JobService(
        conn,
        owner_id=owner_id or str(uuid.uuid4()),
        handlers={kind: handler},
        resource_for_kind={kind: (ResourceKind.CORPUS_WRITE, corpus)},
    )
    job = job_service.run_inline(kind, {"action": action, "corpus": corpus})
    return {
        "id": job.id,
        "kind": job.kind,
        "state": str(job.state),
        "params": job.params,
        "error": job.error,
        **outbox,
    }


__all__ = [
    "CORPORA",
    "CORPUS_LABELS",
    "IndexService",
    "fts_table_name",
    "make_index_job_handler",
    "run_index_inline",
]
