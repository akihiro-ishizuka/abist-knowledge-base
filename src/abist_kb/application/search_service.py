"""統合検索のアプリケーションサービス(設計書 §9.2、task-4-brief Step1)。

`infrastructure.search.search_engine.search` を索引DB接続へ配線し、クエリの
埋め込み(ベクトル検索が使える場合のみ)・コーパス選択・応答の組み立てまでを
行う。**応答形状は M5 の MCP `search_kb`/`get_document` fixture に合わせてある**
(`tests/fixtures/mcp/kb-search/search_kb/*.json` を参照)。fixture 本文は
JS のキャメルケースだが、`search_engine.search()` 自体が既にスネークケースで
統一している(M4 task-3 report の判断を踏襲)ため、ここでもキャメル化はしない
— キャメル化は M5(MCP サーバ)の変換層の仕事として残す。

検索は読み取り専用であり、`corpus-write` リースは取得しない。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.ai.embedding_provider import EmbeddingProvider, LocalEmbeddingProvider
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.search.e5_input import query_input
from abist_kb.infrastructure.search.query_terms import is_pure_identifier_query
from abist_kb.infrastructure.search.search_engine import SearchOptions, search

#: fixture (`tests/fixtures/mcp/kb-search/search_kb/*.json`) の `note` に常に含まれる
#: 文言。行番号は索引時点のスナップショットであり、`index_stale` が立っていれば
#: 信用できないことを毎回明示する。
STALE_NOTE = (
    "index_stale: true の結果は行番号が陳腐化しています。get_document で原文を確認してください。"
)
#: ベクトル検索が使えなかった(埋め込み未生成、または識別子クエリでスキップ)ときに
#: 追記する文言(fixture: zero_hit_query.json / corpus_reference.json)。
EMBEDDING_MISSING_NOTE = " 埋め込みが未生成のため全文検索のみで検索しています。"

CORPORA: tuple[str, ...] = ("work", "reference")

ProviderFactory = Callable[[str], EmbeddingProvider]


def _default_provider_factory(model_identity: str) -> EmbeddingProvider:
    return LocalEmbeddingProvider(model_identity=model_identity)


class SearchService:
    """work/reference インデックスに対する統合検索。"""

    def __init__(
        self,
        *,
        docs_dir: Path,
        work_index_path: Path,
        reference_index_path: Path,
        embedding_provider_factory: ProviderFactory = _default_provider_factory,
    ) -> None:
        self._docs_dir = docs_dir
        self._index_paths: dict[str, Path] = {
            "work": work_index_path,
            "reference": reference_index_path,
        }
        self._embedding_provider_factory = embedding_provider_factory

    def _require_index(self, corpus: str) -> Path:
        if corpus not in CORPORA:
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"未知のコーパスです: {corpus!r}(有効値: {', '.join(CORPORA)})",
                exit_code=ExitCode.INVALID_INPUT,
            )
        path = self._index_paths[corpus]
        if not path.is_file():
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"コーパス '{corpus}' の索引がまだありません: {path}",
                hint="`index build` を先に実行してください。",
                exit_code=ExitCode.INVALID_INPUT,
            )
        return path

    def _resolve_query_vector(
        self, conn: Any, query: str, *, identifier_only: bool
    ) -> list[float] | None:
        if identifier_only:
            return None
        model = conn.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()
        if model is None or not model["value"]:
            return None
        count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        if count == 0:
            return None

        model_identity = model["value"]
        provider = self._embedding_provider_factory(model_identity)
        try:
            text = query_input(query, model_identity)
            vectors = provider.embed_batch([text])
            return list(vectors[0])
        finally:
            close = getattr(provider, "close", None)
            if close is not None:
                close()

    def search(
        self,
        query: str,
        *,
        corpus: str = "work",
        source: str | None = None,
        document_type: str | None = None,
        status: str | None = None,
        path_prefix: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """`query` を実行し、MCP `search_kb` fixture と同じ形の結果を返す。"""
        index_path = self._require_index(corpus)
        conn = connect(index_path, read_only=True)
        try:
            identifier_only = is_pure_identifier_query(query)
            query_vector = self._resolve_query_vector(conn, query, identifier_only=identifier_only)
            options = SearchOptions(
                limit=limit,
                source=source,
                document_type=document_type,
                status=status,
                path_prefix=path_prefix,
            )
            result = search(
                conn,
                query,
                query_vector=query_vector,
                docs_dir=self._docs_dir,
                options=options,
            )
        finally:
            conn.close()

        note = STALE_NOTE
        if not result.diagnostics.get("vector_available"):
            note += EMBEDDING_MISSING_NOTE

        return {
            "ok": True,
            "count": len(result.results),
            "results": result.results,
            "diagnostics": result.diagnostics,
            "note": note,
        }


__all__ = ["CORPORA", "EMBEDDING_MISSING_NOTE", "STALE_NOTE", "SearchService"]
