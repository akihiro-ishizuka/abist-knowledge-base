"""`IndexService`/`SearchService`(task-4-brief Step1)。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from abist_kb.application.index_service import IndexService, run_index_inline
from abist_kb.application.search_service import SearchService
from abist_kb.domain.errors import AppError
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema


def _write(docs_dir: Path, path: str, content: str) -> None:
    full = docs_dir / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


@pytest.fixture
def paths(tmp_root: Path) -> dict[str, Path]:
    return {
        "docs_dir": tmp_root / "docs",
        "app_db_path": tmp_root / "app.sqlite",
        "work_index_path": tmp_root / "work-index.sqlite",
        "reference_index_path": tmp_root / "reference-index.sqlite",
    }


@pytest.fixture
def index_service(paths: dict[str, Path]) -> IndexService:
    return IndexService(
        docs_dir=paths["docs_dir"],
        app_db_path=paths["app_db_path"],
        work_index_path=paths["work_index_path"],
        reference_index_path=paths["reference_index_path"],
    )


def _seed_work_document(paths: dict[str, Path]) -> None:
    _write(
        paths["docs_dir"],
        "a.md",
        "# CATIA起動\n\nCATIAの起動時間を短縮する手順について説明します。\n",
    )
    conn = connect(paths["app_db_path"])
    try:
        ensure_app_schema(conn)
        DocumentRepository(conn).upsert(
            {
                "path": "a.md",
                "title": "CATIA起動",
                "source": "esa",
                "document_type": "memo",
                "status": "active",
                "post_number": 1,
            }
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# IndexService.build/status
# ---------------------------------------------------------------------------


def test_build_inserts_work_documents_from_app_db(
    index_service: IndexService, paths: dict[str, Path]
) -> None:
    _seed_work_document(paths)
    result = index_service.build("work")
    assert result["summary"]["documents_added"] == 1
    assert result["disk_only_count"] == 0

    status = index_service.status()
    assert status["corpora"]["work"]["available"] is True
    assert status["corpora"]["work"]["documents"] == 1
    assert status["corpora"]["work"]["chunks"] >= 1


def test_build_reports_disk_only_paths_not_in_documents_table(
    index_service: IndexService, paths: dict[str, Path]
) -> None:
    """`documents` に無いが `docs/` に実在するファイルを黙って落とさない。"""
    _write(paths["docs_dir"], "orphan.md", "# 孤立\n\n本文。\n")
    conn = connect(paths["app_db_path"])
    try:
        ensure_app_schema(conn)
    finally:
        conn.close()

    result = index_service.build("work")
    assert result["disk_only_count"] == 1
    assert "orphan.md" in result["disk_only_paths_sample"]


def test_status_reports_unavailable_corpus_before_build(index_service: IndexService) -> None:
    status = index_service.status()
    assert status["corpora"]["work"]["available"] is False
    assert status["corpora"]["reference"]["available"] is False
    assert status["default_corpus"] == "work"


def test_build_rejects_unknown_corpus(index_service: IndexService) -> None:
    with pytest.raises(AppError):
        index_service.build("bogus")


def test_run_index_inline_runs_under_corpus_write_lease(
    index_service: IndexService, paths: dict[str, Path]
) -> None:
    _seed_work_document(paths)
    conn = connect(paths["app_db_path"])
    try:
        ensure_app_schema(conn)
        result = run_index_inline(index_service, conn, action="build", corpus="work")
    finally:
        conn.close()
    assert result["state"] == "succeeded"
    assert result["result"]["summary"]["documents_added"] == 1


# ---------------------------------------------------------------------------
# SearchService.search
# ---------------------------------------------------------------------------


class _FakeProvider:
    """決定的なベクトルを返す埋め込みプロバイダ(モデルロード不要)。"""

    def __init__(self, model_identity: str) -> None:
        self.model_identity = model_identity
        self.closed = False

    def embed_batch(self, texts):
        return [np.ones(4, dtype="<f4") for _ in texts]

    def close(self) -> None:
        self.closed = True


def _search_service(paths: dict[str, Path]) -> SearchService:
    return SearchService(
        docs_dir=paths["docs_dir"],
        work_index_path=paths["work_index_path"],
        reference_index_path=paths["reference_index_path"],
        embedding_provider_factory=_FakeProvider,
    )


def test_search_raises_when_corpus_not_indexed(paths: dict[str, Path]) -> None:
    service = _search_service(paths)
    with pytest.raises(AppError):
        service.search("CATIA")


def test_search_matches_mcp_search_kb_fixture_shape(
    index_service: IndexService, paths: dict[str, Path]
) -> None:
    _seed_work_document(paths)
    index_service.build("work")
    service = _search_service(paths)

    result = service.search("CATIA")

    assert result["ok"] is True
    assert result["count"] >= 1
    result_keys = {
        "path",
        "title",
        "heading_path",
        "start_line",
        "end_line",
        "snippet",
        "url",
        "status",
        "source",
        "document_type",
        "post_number",
        "document_hash",
        "indexed_at",
        "index_stale",
        "score",
        "matched_by",
        "boosts",
        "other_paths",
        "chunk_id",
    }
    assert result_keys.issubset(result["results"][0].keys())
    diagnostics_keys = {
        "query",
        "terms",
        "tokenizer",
        "keyword_candidates",
        "short_term_candidates",
        "vector_candidates",
        "identifier_only_query",
        "vector_available",
        "elapsed_ms",
    }
    assert diagnostics_keys.issubset(result["diagnostics"].keys())
    assert "index_stale: true" in result["note"]


def test_search_note_mentions_missing_embeddings_when_vector_unavailable(
    index_service: IndexService, paths: dict[str, Path]
) -> None:
    """埋め込み未生成の索引では note に全文検索のみである旨を追記する。

    fixture: zero_hit_query.json
    """
    _seed_work_document(paths)
    index_service.build("work")  # embed していないので embedding_model が meta に無い
    service = _search_service(paths)

    result = service.search("CATIA")

    assert result["diagnostics"]["vector_available"] is False
    assert "埋め込みが未生成" in result["note"]


def test_search_identifier_only_query_skips_vector_search(
    index_service: IndexService, paths: dict[str, Path]
) -> None:
    _seed_work_document(paths)
    index_service.build("work")
    service = _search_service(paths)

    result = service.search("shrink_clamp_bellow_overlap_mm")

    assert result["diagnostics"]["identifier_only_query"] is True
    assert result["diagnostics"]["vector_candidates"] == 0


def test_search_applies_filters(index_service: IndexService, paths: dict[str, Path]) -> None:
    _seed_work_document(paths)
    index_service.build("work")
    service = _search_service(paths)

    result = service.search("CATIA", source="web")
    assert result["count"] == 0


def test_search_zero_hit_returns_empty_results_not_error(
    index_service: IndexService, paths: dict[str, Path]
) -> None:
    _seed_work_document(paths)
    index_service.build("work")
    service = _search_service(paths)

    result = service.search("PySide6", path_prefix="this-path-prefix-does-not-exist/")
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["results"] == []
