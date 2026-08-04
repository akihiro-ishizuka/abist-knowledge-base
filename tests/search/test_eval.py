"""`application.audit.search_quality`(task-5-brief Step2)。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from abist_kb.application.audit.search_quality import (
    compare_tokenizers,
    compare_with_previous,
    evaluate,
    find_latest_report,
    load_queries,
    write_report,
)
from abist_kb.application.index_service import IndexService
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema


def _write(docs_dir: Path, path: str, content: str) -> None:
    full = docs_dir / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


class _FakeProvider:
    def __init__(self, model_identity: str) -> None:
        self.model_identity = model_identity

    def embed_batch(self, texts):
        return [np.ones(4, dtype="<f4") for _ in texts]

    def close(self) -> None:
        pass


@pytest.fixture
def paths(tmp_root: Path) -> dict[str, Path]:
    return {
        "docs_dir": tmp_root / "docs",
        "app_db_path": tmp_root / "app.sqlite",
        "work_index_path": tmp_root / "work-index.sqlite",
        "reference_index_path": tmp_root / "reference-index.sqlite",
        "reports_dir": tmp_root / "reports",
    }


@pytest.fixture
def indexed_work(paths: dict[str, Path]) -> dict[str, Path]:
    _write(
        paths["docs_dir"],
        "catia.md",
        "# CATIA起動\n\nCATIAの起動時間を短縮する手順について説明します。\n",
    )
    _write(
        paths["docs_dir"],
        "unrelated.md",
        "# 別件\n\nこれは無関係な文書です。\n",
    )
    conn = connect(paths["app_db_path"])
    try:
        ensure_app_schema(conn)
        repo = DocumentRepository(conn)
        repo.upsert(
            {
                "path": "catia.md",
                "title": "CATIA起動",
                "source": "esa",
                "document_type": "memo",
                "status": "active",
                "post_number": 1,
            }
        )
        repo.upsert(
            {
                "path": "unrelated.md",
                "title": "別件",
                "source": "esa",
                "document_type": "memo",
                "status": "active",
                "post_number": 2,
            }
        )
    finally:
        conn.close()

    service = IndexService(
        docs_dir=paths["docs_dir"],
        app_db_path=paths["app_db_path"],
        work_index_path=paths["work_index_path"],
        reference_index_path=paths["reference_index_path"],
    )
    service.build("work")
    return paths


_QUERIES = [
    {
        "id": "q1",
        "query": "CATIA",
        "type": "identifier",
        "relevant": [{"post_number": 1, "path": "catia.md", "title": "CATIA起動"}],
    }
]


def test_evaluate_scores_hybrid_and_bm25_raw(indexed_work: dict[str, Path]) -> None:
    conn = connect(indexed_work["work_index_path"], read_only=True)
    try:
        report = evaluate(
            conn,
            _QUERIES,
            docs_dir=indexed_work["docs_dir"],
            embedding_provider_factory=_FakeProvider,
        )
    finally:
        conn.close()

    assert report["query_count"] == 1
    assert set(report["methods"]) == {"bm25_raw", "hybrid"}
    for method in ("bm25_raw", "hybrid"):
        macro = report["macro"][method]
        assert macro["recall5"] == pytest.approx(1.0)
        assert 0.0 <= macro["mrr"] <= 1.0
        assert 0.0 <= macro["ndcg10"] <= 1.0
    per_query = report["per_query"][0]
    for method in ("bm25_raw", "hybrid"):
        folded = per_query["results"][method]["folded"]
        keys = [row.get("post_number") for row in folded]
        assert len(keys) == len(set(keys)), "folded は文書単位で重複してはいけない"


def test_evaluate_zero_hit_when_no_match(indexed_work: dict[str, Path]) -> None:
    queries = [
        {
            "id": "q2",
            "query": "存在しない語句xyz",
            "type": "natural",
            "relevant": [{"post_number": 999, "path": "nope.md"}],
        }
    ]
    conn = connect(indexed_work["work_index_path"], read_only=True)
    try:
        report = evaluate(
            conn,
            queries,
            docs_dir=indexed_work["docs_dir"],
            embedding_provider_factory=_FakeProvider,
        )
    finally:
        conn.close()
    for method in ("bm25_raw", "hybrid"):
        assert report["macro"][method]["recall5"] == 0.0


def test_write_report_and_find_latest_report(paths: dict[str, Path]) -> None:
    report = {
        "schema": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "query_count": 1,
        "methods": ["bm25_raw"],
        "macro": {"bm25_raw": {"recall5": 1.0, "mrr": 1.0, "ndcg10": 1.0, "zero_hit_queries": 0}},
        "per_query": [],
    }
    path = write_report(report, reports_dir=paths["reports_dir"])
    assert path.is_file()
    assert path.name == "eval-2026-01-01T00-00-00+00-00.json"
    assert find_latest_report(paths["reports_dir"]) == path


def test_find_latest_report_returns_none_when_absent(paths: dict[str, Path]) -> None:
    assert find_latest_report(paths["reports_dir"]) is None


def test_compare_with_previous_flags_regression_beyond_threshold() -> None:
    current = {"macro": {"hybrid": {"recall5": 0.90, "mrr": 0.8, "ndcg10": 0.8}}}
    previous = {"macro": {"hybrid": {"recall5": 0.95, "mrr": 0.8, "ndcg10": 0.8}}}
    warnings = compare_with_previous(current, previous)
    assert len(warnings) == 1
    assert warnings[0]["method"] == "hybrid"
    assert warnings[0]["metric"] == "recall5"


def test_compare_with_previous_ignores_small_deltas() -> None:
    current = {"macro": {"hybrid": {"recall5": 0.945, "mrr": 0.8, "ndcg10": 0.8}}}
    previous = {"macro": {"hybrid": {"recall5": 0.9545, "mrr": 0.8, "ndcg10": 0.8}}}
    assert compare_with_previous(current, previous) == []


def test_compare_with_previous_none_baseline_returns_no_warnings() -> None:
    current = {"macro": {"hybrid": {"recall5": 0.5, "mrr": 0.5, "ndcg10": 0.5}}}
    assert compare_with_previous(current, None) == []


def test_compare_tokenizers_reports_both_tokenizers(indexed_work: dict[str, Path]) -> None:
    conn = connect(indexed_work["work_index_path"], read_only=True)
    try:
        result = compare_tokenizers(conn, _QUERIES)
    finally:
        conn.close()
    assert set(result["tokenizers"].keys()) == {"unicode61", "trigram"}
    for metrics in result["tokenizers"].values():
        assert 0.0 <= metrics["recall5"] <= 1.0


def test_load_queries_parses_jsonl(tmp_root: Path) -> None:
    path = tmp_root / "q.jsonl"
    path.write_text(
        '{"id": "q1", "query": "a", "relevant": []}\n{"id": "q2", "query": "b", "relevant": []}\n',
        encoding="utf-8",
    )
    queries = load_queries(path)
    assert [q["id"] for q in queries] == ["q1", "q2"]
