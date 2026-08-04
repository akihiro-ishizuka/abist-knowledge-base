"""`migrate verify` の search_quality 条件(§11.4)を実データ相当の索引DBへ実接続する。

M4 で構築済みの `application.audit.search_quality.evaluate()`(Recall@5等の
計測ロジック)と `tests/fixtures/eval/baseline.json` 形式のcaptured baselineを
`verify.measure_search_quality()`/`verify_migration()` へ実際に配線し、
呼び出し側がbaseline/クエリ/索引DBを渡した場合は「未計測」のfail-closedへ
落ちずに実測することを確認する。
"""

from __future__ import annotations

from pathlib import Path

from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.search.index_schema import DEFAULT_TOKENIZERS, create_index_schema
from abist_kb.infrastructure.search.indexer import IndexBuilder
from abist_kb.migration.manifest import Manifest, StepRecord
from abist_kb.migration.verify import measure_search_quality, verify_migration

_ROW = {
    "path": "a.md",
    "post_number": 1,
    "title": "タイトル",
    "source": "esa",
    "document_type": "article",
    "status": "active",
    "url": None,
    "category": None,
}


def _build_index(tmp_path: Path) -> Path:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# 見出し\n\n蛇腹パターンの検証手順。\n", encoding="utf-8")
    index_path = tmp_path / "work-index.sqlite"
    conn = connect(index_path)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        IndexBuilder(conn).build([_ROW], docs_dir)
    finally:
        conn.close()
    return index_path


def _baseline_and_queries() -> tuple[dict, list[dict]]:
    queries = [
        {"id": "q1", "query": "蛇腹パターン", "relevant": [{"path": "a.md", "post_number": 1}]}
    ]
    baseline = {
        "macro": {"bm25_raw": {"recall5": 1.0}},
        "per_query": [
            {
                "id": "q1",
                "results": {
                    "bm25_raw": {
                        "folded": [
                            {"path": "a.md", "start_line": 1, "end_line": 3},
                        ]
                    }
                },
            }
        ],
    }
    return baseline, queries


def test_measure_search_quality_computes_real_recall_and_citation_agreement(
    tmp_path: Path,
) -> None:
    index_path = _build_index(tmp_path)
    baseline, queries = _baseline_and_queries()
    conn = connect(index_path, read_only=True)
    try:
        recall_before, recall_after, citation_agreement = measure_search_quality(
            conn,
            tmp_path / "docs",
            baseline=baseline,
            queries=queries,
            method="bm25_raw",
        )
    finally:
        conn.close()

    assert recall_before == 1.0
    assert recall_after == 1.0
    assert citation_agreement == 1.0


def test_verify_migration_wires_search_quality_when_inputs_supplied(
    old_repo: Path, tmp_path: Path
) -> None:
    index_path = _build_index(tmp_path)
    baseline, queries = _baseline_and_queries()
    conn = connect(index_path, read_only=True)

    to_root = tmp_path / "new-repo"
    to_root.mkdir()
    manifest = Manifest(from_root=str(old_repo), to_root=str(to_root))
    manifest.record_step(
        StepRecord(name="copy_docs", status="completed", input_hash="x", counts={"copied": 0})
    )
    try:
        result = verify_migration(
            manifest,
            old_repo,
            to_root,
            search_quality_conn=conn,
            search_quality_docs_dir=tmp_path / "docs",
            search_quality_baseline=baseline,
            search_quality_queries=queries,
            search_quality_method="bm25_raw",
        )
    finally:
        conn.close()

    by_name = {c.name: c for c in result.conditions}
    assert by_name["search_quality"].passed
    assert "未計測" not in by_name["search_quality"].detail


def test_verify_migration_still_fails_closed_without_search_quality_inputs(
    old_repo: Path, tmp_path: Path
) -> None:
    to_root = tmp_path / "new-repo"
    to_root.mkdir()
    manifest = Manifest(from_root=str(old_repo), to_root=str(to_root))
    result = verify_migration(manifest, old_repo, to_root)
    by_name = {c.name: c for c in result.conditions}
    assert not by_name["search_quality"].passed
