"""クエリ語抽出と統合検索(`design/plans/M4-index-search.md` Task3)。

旧 `test/search-engine.test.js` のケースを移植する(M1 では fixture 化されていない
ため、実行で確認しながら書いた)。チューニング値(重み・ブースト量・しきい値)は
旧版の実測に基づく既定値であり、テストはその値を固定するためにある
(`tests/fixtures/eval/baseline.json` の Recall@5 ベースラインとの比較を壊さないため)。
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from abist_kb.domain.frontmatter import hash_body
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.search.index_schema import create_index_schema
from abist_kb.infrastructure.search.indexer import IndexBuilder
from abist_kb.infrastructure.search.query_terms import (
    extract_query_terms,
    identifier_terms,
    is_pure_identifier_query,
)
from abist_kb.infrastructure.search.search_engine import (
    FusedEntry,
    apply_boosts,
    check_index_stale,
    deduplicate,
    fuse_rrf,
    keyword_search,
    make_snippet,
    search,
    short_term_search,
    vector_search,
)

# ---------------------------------------------------------------------------
# クエリの語分割(日本語は空白で区切られない)
# ---------------------------------------------------------------------------


def test_extracts_content_words_from_japanese_question():
    terms = extract_query_terms("CATIAの起動時間を短縮するにはどうすればよいか")
    assert "CATIA" in terms
    assert "起動時間" in terms
    assert "短縮" in terms
    assert "どう" not in terms, "疑問表現を語として残してはいけない"


def test_keeps_identifiers_intact():
    assert "shrink_clamp_bellow_overlap_mm" in extract_query_terms("shrink_clamp_bellow_overlap_mm")
    assert "Selection.Search" in extract_query_terms("Selection.Search の文法")
    assert "#398" in extract_query_terms("#398 の対応状況")


def test_extracts_katakana_and_dimension_notation():
    terms = extract_query_terms("コンボリュートチューブ φ9 の製品仕様")
    assert any("コンボリュート" in t for t in terms)
    assert "φ9" in terms


def test_full_width_digits_are_not_treated_as_ascii_digits():
    """M2 で全角数字が ASCII と同じ扱いを受けた罠の再発防止。`[0-9]` を使うこと。"""
    assert extract_query_terms("#398") == ["#398"]
    assert extract_query_terms("#３９８") != ["#３９８"]


def test_identifies_terms_eligible_for_identifier_boost():
    ids = identifier_terms("#398 と PySide6 と 起動時間")
    assert "#398" in ids
    assert "PySide6" in ids
    assert "起動時間" not in ids, "日本語を識別子扱いしてはいけない"


@pytest.mark.parametrize(
    "query", ["#398", "PySide6", "shrink_clamp_bellow_overlap_mm", "Selection.Search"]
)
def test_pure_identifier_queries_are_detected(query):
    assert is_pure_identifier_query(query) is True


@pytest.mark.parametrize(
    "query", ["蛇腹の要件", "CATIAの起動時間を短縮する", "PySide6 の採用理由", ""]
)
def test_non_identifier_queries_are_not_misdetected(query):
    assert is_pure_identifier_query(query) is False


# ---------------------------------------------------------------------------
# RRF
# ---------------------------------------------------------------------------


def test_top_of_both_rankings_becomes_top_overall():
    a = [{"chunk_id": 1}, {"chunk_id": 2}, {"chunk_id": 3}]
    b = [{"chunk_id": 3}, {"chunk_id": 1}, {"chunk_id": 9}]

    fused = fuse_rrf([("keyword", a), ("vector", b)], k=60)
    assert fused[0].row["chunk_id"] == 1, "両方で上位のものが最上位でない"
    assert sorted(fused[0].sources) == ["keyword", "vector"]
    assert len(fused) == 4


def test_results_present_in_only_one_ranking_survive():
    a = [{"chunk_id": 1}]
    fused = fuse_rrf([("keyword", a)], k=60)
    assert len(fused) == 1


# ---------------------------------------------------------------------------
# ブースト
# ---------------------------------------------------------------------------


def test_title_exact_match_and_active_status_are_boosted():
    entries = [
        FusedEntry(
            row={"title": "別の記事", "text": "本文", "heading_path": "", "status": "archived"},
            score=0.1,
        ),
        FusedEntry(
            row={"title": "Unbend処理", "text": "本文", "heading_path": "", "status": "active"},
            score=0.1,
        ),
    ]
    boosted = apply_boosts(entries, "Unbend処理")
    assert boosted[0].row["title"] == "Unbend処理"
    assert "title_exact" in boosted[0].boosts
    assert "active" in boosted[0].boosts


def test_identifier_present_in_body_is_boosted():
    entries = [
        FusedEntry(
            row={"title": "a", "text": "無関係", "heading_path": "", "status": "active"}, score=0.1
        ),
        FusedEntry(
            row={
                "title": "b",
                "text": "shrink_clamp_bellow_overlap_mm を使う",
                "heading_path": "",
                "status": "active",
            },
            score=0.1,
        ),
    ]
    boosted = apply_boosts(entries, "shrink_clamp_bellow_overlap_mm")
    assert boosted[0].row["title"] == "b"
    assert any(b.startswith("identifier:") for b in boosted[0].boosts)


def test_identifier_boost_dominates_rrf_score_range():
    """RRF スコアは最大でも約0.016(k=60, index=0: 1/61)。0.08 のブーストが
    支配的であることを実測で確認する(brief の非対称性の主張そのもの)。
    """
    entries = [
        FusedEntry(
            row={"title": "x", "text": "PySide6 を使う", "heading_path": "", "status": None},
            score=1 / 61,
        )
    ]
    boosted = apply_boosts(entries, "PySide6")
    assert boosted[0].score > 0.08


# ---------------------------------------------------------------------------
# 重複排除
# ---------------------------------------------------------------------------


def test_identical_content_chunks_collapse_to_one():
    entries = [
        FusedEntry(
            row={"chunk_id": 1, "path": "a/x.md", "content_hash": "h1", "post_number": 1}, score=3
        ),
        FusedEntry(
            row={"chunk_id": 2, "path": "b/x.md", "content_hash": "h1", "post_number": 1}, score=2
        ),
    ]
    result = deduplicate(entries, {"max_chunks_per_document": 2})
    assert len(result) == 1
    assert result[0].other_paths == ["b/x.md"], "畳んだ別パスを報告していない"


def test_same_article_different_paths_collapse():
    entries = [
        FusedEntry(
            row={
                "chunk_id": 1,
                "path": "チーム内定例/x.md",
                "content_hash": "h1",
                "post_number": 100,
            },
            score=3,
        ),
        FusedEntry(
            row={
                "chunk_id": 2,
                "path": "蛇腹形状の自動設計/x.md",
                "content_hash": "h2",
                "post_number": 100,
            },
            score=2,
        ),
        FusedEntry(
            row={
                "chunk_id": 3,
                "path": "チーム内定例/x.md",
                "content_hash": "h3",
                "post_number": 100,
            },
            score=1,
        ),
    ]
    result = deduplicate(entries, {"max_chunks_per_document": 2})
    assert len(result) == 2, "同一記事から上限を超えて返している"
    assert all(r.row["post_number"] == 100 for r in result)


def test_chunks_per_document_are_capped():
    entries = [
        FusedEntry(
            row={"chunk_id": i, "path": "a/x.md", "content_hash": f"h{i}", "post_number": 5},
            score=10 - i,
        )
        for i in (1, 2, 3, 4)
    ]
    assert len(deduplicate(entries, {"max_chunks_per_document": 2})) == 2
    assert len(deduplicate(entries, {"max_chunks_per_document": 1})) == 1


def test_documents_without_post_number_group_by_canonical_path():
    entries = [
        FusedEntry(
            row={
                "chunk_id": 1,
                "path": "a/x.md",
                "content_hash": "h1",
                "post_number": None,
                "canonical_path": "a/x.md",
            },
            score=2,
        ),
        FusedEntry(
            row={
                "chunk_id": 2,
                "path": "a/y.md",
                "content_hash": "h2",
                "post_number": None,
                "canonical_path": "a/y.md",
            },
            score=1,
        ),
    ]
    result = deduplicate(entries, {"max_chunks_per_document": 1})
    assert len(result) == 2, "別文書まで畳んでしまった"


# ---------------------------------------------------------------------------
# スニペット・鮮度
# ---------------------------------------------------------------------------


def test_snippet_extracts_around_the_query_term():
    text = "あ" * 300 + "目的の語がここにある" + "い" * 300
    snippet = make_snippet(text, "目的の語")
    assert "目的の語" in snippet, "検索語が含まれていない"
    assert len(snippet) < 300


def test_index_stale_when_file_changes_after_indexing(tmp_root: Path):
    dir_ = tmp_root / "stale"
    dir_.mkdir()
    content = '---\ntitle: "x"\n---\n\n本文\n'
    (dir_ / "a.md").write_text(content, encoding="utf-8")
    indexed_hash = hash_body(content)

    assert check_index_stale(dir_, "a.md", indexed_hash)[0] is False

    (dir_ / "a.md").write_text(content + "追記\n", encoding="utf-8")
    assert check_index_stale(dir_, "a.md", indexed_hash)[0] is True

    assert check_index_stale(dir_, "無い.md", indexed_hash)[0] is True, (
        "読めない場合も stale にする"
    )


# ---------------------------------------------------------------------------
# 索引を作って実際に検索する
# ---------------------------------------------------------------------------


def _build_test_index(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    conn = connect(tmp_root / "kb.sqlite")
    create_index_schema(conn, tokenizers=["trigram"])

    docs = [
        {
            "row": {
                "path": "batchA/蛇腹/Unbend処理.md",
                "post_number": 1,
                "title": "Unbend処理",
                "source": "esa",
                "document_type": "knowledge",
                "status": "active",
                "url": None,
                "category": None,
            },
            "content": (
                '---\ntitle: "Unbend処理"\npost_number: 1\n---\n\n'
                "# Unbend処理\n\n蛇腹の展開は sheetmetal で行う。\n"
            ),
        },
        {
            "row": {
                "path": "batchB/蛇腹/Unbend処理.md",
                "post_number": 1,
                "title": "Unbend処理",
                "source": "esa",
                "document_type": "knowledge",
                "status": "active",
                "url": None,
                "category": None,
            },
            "content": (
                '---\ntitle: "Unbend処理"\npost_number: 1\n---\n\n'
                "# Unbend処理\n\n蛇腹の展開は sheetmetal で行う。\n"
            ),
        },
        {
            "row": {
                "path": "batchA/メモ/起動.md",
                "post_number": 2,
                "title": "CATIAの起動時間",
                "source": "esa",
                "document_type": "memo",
                "status": "archived",
                "url": None,
                "category": None,
            },
            "content": (
                '---\ntitle: "CATIAの起動時間"\npost_number: 2\n---\n\n'
                "# 起動時間\n\nキャッシュを消すと速くなる。\n"
            ),
        },
    ]

    for doc in docs:
        full = docs_dir / doc["row"]["path"]
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(doc["content"], encoding="utf-8")

    rows = [doc["row"] for doc in docs]
    IndexBuilder(conn, tokenizers=["trigram"]).build(rows, docs_dir)
    return conn, docs_dir


def test_full_text_search_matches_three_or_more_char_japanese_query(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        rows = keyword_search(conn, "sheetmetal で展開する", {"tokenizer": "trigram"})
        assert len(rows) > 0, "日本語クエリでヒットしていない"
        assert any(r["title"] == "Unbend処理" for r in rows)
    finally:
        conn.close()


def test_trigram_alone_cannot_find_two_char_japanese_terms(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        # 「蛇腹」は2文字なので trigram の索引対象にならず MATCH では拾えない
        rows = keyword_search(conn, "蛇腹", {"tokenizer": "trigram"})
        assert rows == [], "この前提が変わったら補助検索の要否を見直すこと"
    finally:
        conn.close()


def test_short_term_search_finds_two_char_japanese_terms(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        rows = short_term_search(conn, "蛇腹", {"tokenizer": "trigram"})
        assert len(rows) > 0, "2文字語を補助検索で拾えていない"
        assert any(r["title"] == "Unbend処理" for r in rows)
    finally:
        conn.close()


def test_hybrid_search_answers_even_for_two_char_only_query(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        result = search(conn, "蛇腹", options={"tokenizer": "trigram"})
        assert len(result.results) > 0, "2文字語のクエリで0件になっている"
        assert result.diagnostics["short_term_candidates"] > 0
    finally:
        conn.close()


def test_ascii_only_query_does_not_trigger_short_term_search(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        assert short_term_search(conn, "sheetmetal", {"tokenizer": "trigram"}) == []
    finally:
        conn.close()


def test_duplicate_paths_of_same_article_collapse_to_one(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        result = search(conn, "sheetmetal", options={"tokenizer": "trigram", "limit": 10})
        posts = [r["post_number"] for r in result.results]
        assert len(set(posts)) == len(posts), "同一記事が複数返っている"
        unbend = next(r for r in result.results if r["post_number"] == 1)
        assert len(unbend["other_paths"]) >= 1, "畳んだ別パスを報告していない"
    finally:
        conn.close()


def test_filters_are_applied(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        memo_only = search(conn, "起動", options={"tokenizer": "trigram", "document_type": "memo"})
        assert all(r["document_type"] == "memo" for r in memo_only.results)

        path_only = search(conn, "蛇腹", options={"tokenizer": "trigram", "path_prefix": "batchB"})
        assert all(r["path"].startswith("batchB") for r in path_only.results)
    finally:
        conn.close()


def test_results_carry_provenance_fields(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        result = search(conn, "sheetmetal", options={"tokenizer": "trigram"})
        r = result.results[0]
        for key in [
            "path",
            "heading_path",
            "start_line",
            "end_line",
            "snippet",
            "status",
            "source",
            "document_hash",
            "indexed_at",
            "index_stale",
        ]:
            assert key in r, f"{key} が結果に含まれていない"
        assert r["start_line"] >= 1
        assert r["end_line"] >= r["start_line"]
    finally:
        conn.close()


def test_works_with_full_text_search_alone_without_embeddings(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        result = search(conn, "sheetmetal", options={"tokenizer": "trigram"})
        assert result.diagnostics["vector_available"] is False
        assert len(result.results) > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 識別子だけのクエリの扱い
# ---------------------------------------------------------------------------


def test_identifier_only_queries_skip_vector_search(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        # 埋め込みが無い索引なので query_vector を渡しても使われないことだけ確認する
        identifier = search(
            conn, "sheetmetal", query_vector=np.zeros(384), options={"tokenizer": "trigram"}
        )
        assert identifier.diagnostics["identifier_only_query"] is True
        assert identifier.diagnostics["vector_candidates"] == 0

        natural = search(conn, "蛇腹の展開について", options={"tokenizer": "trigram"})
        assert natural.diagnostics["identifier_only_query"] is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ベクトル検索
# ---------------------------------------------------------------------------


def _insert_embedding(conn, chunk_id: int, vector: list[float]) -> None:
    blob = struct.pack(f"<{len(vector)}f", *vector)
    conn.execute(
        "INSERT INTO embeddings (chunk_id, vector, model, dimensions, input_hash, created_at) "
        "VALUES (?, ?, 'test-model', ?, 'h', '2026-01-01T00:00:00Z')",
        (chunk_id, blob, len(vector)),
    )


def test_vector_search_ranks_by_dot_product_and_skips_when_no_embeddings(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        # 埋め込みが無い状態では空を返す(全文検索だけで動く前提)
        assert vector_search(conn, [1.0, 0.0], {"tokenizer": "trigram"}) == []

        chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks ORDER BY id").fetchall()]
        assert len(chunk_ids) >= 2
        _insert_embedding(conn, chunk_ids[0], [1.0, 0.0])
        _insert_embedding(conn, chunk_ids[1], [0.0, 1.0])
        conn.commit()

        rows = vector_search(conn, [1.0, 0.0], {"tokenizer": "trigram"})
        assert len(rows) == 2
        assert rows[0]["chunk_id"] == chunk_ids[0]
        assert rows[0]["similarity"] > rows[1]["similarity"]

        # 次元が合わなければ空を返す
        assert vector_search(conn, [1.0, 0.0, 0.0], {"tokenizer": "trigram"}) == []

        # query_vector が None なら空を返す
        assert vector_search(conn, None, {"tokenizer": "trigram"}) == []
    finally:
        conn.close()


def test_vector_matrix_cache_rebuilds_when_embedding_count_changes(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks ORDER BY id").fetchall()]
        _insert_embedding(conn, chunk_ids[0], [1.0, 0.0])
        conn.commit()
        assert len(vector_search(conn, [1.0, 0.0], {"tokenizer": "trigram"})) == 1

        _insert_embedding(conn, chunk_ids[1], [0.0, 1.0])
        conn.commit()
        # embeddings の件数が変わったのでキャッシュが破棄され再構築される
        assert len(vector_search(conn, [1.0, 0.0], {"tokenizer": "trigram"})) == 2
    finally:
        conn.close()


def test_vector_search_applies_filters(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks ORDER BY id").fetchall()]
        for chunk_id in chunk_ids:
            _insert_embedding(conn, chunk_id, [1.0, 0.0])
        conn.commit()

        rows = vector_search(conn, [1.0, 0.0], {"tokenizer": "trigram", "status": "archived"})
        assert len(rows) > 0
        assert all(r["status"] == "archived" for r in rows)
    finally:
        conn.close()


def test_hybrid_search_uses_vector_ranking_when_embeddings_available(tmp_root: Path):
    conn, _docs_dir = _build_test_index(tmp_root)
    try:
        chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks ORDER BY id").fetchall()]
        for chunk_id in chunk_ids:
            _insert_embedding(conn, chunk_id, [1.0, 0.0])
        conn.commit()

        # 自然文クエリ(識別子だけではない)なのでベクトル検索が使われる
        result = search(
            conn, "蛇腹の展開について", query_vector=[1.0, 0.0], options={"tokenizer": "trigram"}
        )
        assert result.diagnostics["vector_available"] is True
        assert result.diagnostics["vector_candidates"] > 0
        assert any("vector" in r["matched_by"] for r in result.results)
    finally:
        conn.close()
