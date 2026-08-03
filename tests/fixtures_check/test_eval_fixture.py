"""M1 Task 4 で採取した検索評価ベースライン fixture の健全性検証。

`capture-eval.mjs` は旧リポジトリの `eval/queries.jsonl`(22クエリ)を
バイト保持でコピーし、`tools/eval-search.js` / `tools/lib/search-engine.js` /
`tools/lib/indexer.js` の実装(サンドボックス化した索引DBコピー上で実行)を
そのまま使って、`bm25_raw`(FTS5のみ)と `hybrid`(統合検索: BM25 + 短語LIKE補助 +
ベクトル + RRF + ブースト + 重複排除)の2方式で Recall@5 / MRR(=RR平均) /
nDCG@10 のベースラインを採取したもの。M4(Python版検索の移植)の受け入れ基準
「Recall@5 が本ベースラインと0.01以内」はこの fixture の `macro` 値が根拠になる。

ここでのテスト対象は Python 実装ではなく、採取された fixture 自体。

検証する性質:
  - `tests/fixtures/eval/queries.jsonl` に22クエリすべてが揃い、各クエリに
    空でない `relevant` 集合があること
  - `tests/fixtures/eval/baseline.json` に `bm25_raw` / `hybrid` の両方式が
    22クエリすべてに存在すること
  - 方式ごとのマクロ平均(Recall@5 / MRR / nDCG@10)が [0, 1] に収まること
  - 各クエリ・各方式のランキング(raw / folded)が、その方式の並び順規約
    (hybrid はスコア降順、bm25_raw は SQLite `bm25()` の慣習どおり昇順
    ["小さい(より負)ほど一致度が高い"])に沿って単調であること。
    ただし実データに同点(tie)が複数存在する(例: bm25_raw の raw リストで
    408件)ため、「厳密な狭義順序」ではなく同点を許す「広義単調」で検証する
    (同点は BM25 スコアが偶然一致する複数チャンクという実際の性質であり、
    バグではない)。
  - `queries.jsonl` の SHA-256 が `baseline.json` に記録された値と一致すること
  - fixture 全体(`queries.jsonl` と `baseline.json`)に本物の `mask_secrets`
    を適用しても変化が無いこと(秘密情報が混入していないことの確認)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from abist_kb.domain.redaction import mask_secrets

FIXTURES_EVAL_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval"
QUERIES_PATH = FIXTURES_EVAL_DIR / "queries.jsonl"
BASELINE_PATH = FIXTURES_EVAL_DIR / "baseline.json"

EXPECTED_QUERY_COUNT = 22
EXPECTED_METHODS = {"bm25_raw", "hybrid"}

# 方式ごとの「良い一致ほど先頭に来る」並び順。
# hybrid: RRF+ブースト後のスコアは大きいほど良い(降順)。
# bm25_raw: SQLite FTS5 の bm25() は値が小さい(より負)ほど一致度が高く、
#           元の SQL も `ORDER BY bm25`(昇順)で並べている。
METHOD_SORT_DIRECTION = {"hybrid": "desc", "bm25_raw": "asc"}


def _load_queries() -> list[dict[str, Any]]:
    lines = [line for line in QUERIES_PATH.read_text(encoding="utf-8").split("\n") if line.strip()]
    return [json.loads(line) for line in lines]


def _load_baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_fixture_files_exist() -> None:
    assert QUERIES_PATH.is_file(), f"queries.jsonl が見つからない: {QUERIES_PATH}"
    assert BASELINE_PATH.is_file(), f"baseline.json が見つからない: {BASELINE_PATH}"


def test_queries_jsonl_has_22_queries_with_nonempty_relevant() -> None:
    queries = _load_queries()
    assert len(queries) == EXPECTED_QUERY_COUNT, f"クエリ数が22ではない: {len(queries)}"

    ids = [q["id"] for q in queries]
    assert len(set(ids)) == EXPECTED_QUERY_COUNT, f"クエリIDに重複がある: {ids}"
    assert ids == sorted(ids), "クエリIDが昇順でない(旧リポジトリの原本と食い違う可能性)"

    for q in queries:
        assert isinstance(q.get("query"), str) and q["query"], f"{q['id']}: query が空"
        assert q.get("type") in {"identifier", "natural"}, (
            f"{q['id']}: type が想定外: {q.get('type')}"
        )
        relevant = q.get("relevant")
        assert isinstance(relevant, list) and len(relevant) > 0, f"{q['id']}: relevant が空"
        for r in relevant:
            has_key = r.get("post_number") is not None or r.get("path")
            assert has_key, f"{q['id']}: relevant の各要素は post_number か path が必要: {r}"


def test_queries_jsonl_sha256_matches_recorded_value() -> None:
    baseline = _load_baseline()
    actual_sha256 = hashlib.sha256(QUERIES_PATH.read_bytes()).hexdigest()
    assert actual_sha256 == baseline["queries_sha256"], (
        "queries.jsonl の実際のSHA-256が baseline.json に記録された値と食い違う"
        "(コピー後にファイルが変更された可能性)"
    )


def test_baseline_schema_and_query_count() -> None:
    baseline = _load_baseline()
    assert baseline["schema"] == 1
    assert isinstance(baseline["source"], str) and baseline["source"]
    assert baseline["query_count"] == EXPECTED_QUERY_COUNT
    assert set(baseline["methods"]) == EXPECTED_METHODS
    assert len(baseline["per_query"]) == EXPECTED_QUERY_COUNT


def test_both_methods_present_for_every_query() -> None:
    baseline = _load_baseline()
    queries = _load_queries()
    query_ids = {q["id"] for q in queries}

    per_query_ids = {pq["id"] for pq in baseline["per_query"]}
    assert per_query_ids == query_ids, (
        "baseline.json の per_query が queries.jsonl の全クエリを網羅していない"
    )

    for pq in baseline["per_query"]:
        results = pq["results"]
        assert set(results.keys()) == EXPECTED_METHODS, (
            f"{pq['id']}: 方式が bm25_raw/hybrid の2つ揃っていない"
        )
        for method in EXPECTED_METHODS:
            m = results[method]
            for key in ("raw", "folded", "recall5", "rr", "ndcg10"):
                assert key in m, f"{pq['id']}/{method}: フィールド {key!r} が無い"
            assert isinstance(m["raw"], list)
            assert isinstance(m["folded"], list)
            assert len(m["folded"]) <= 10, f"{pq['id']}/{method}: folded が10件を超えている"


@pytest.mark.parametrize("method", sorted(EXPECTED_METHODS))
def test_macro_metrics_are_in_unit_range(method: str) -> None:
    baseline = _load_baseline()
    macro = baseline["macro"][method]
    for key in ("recall5", "mrr", "ndcg10"):
        value = macro[key]
        assert 0.0 <= value <= 1.0, f"macro[{method}][{key}] が [0,1] の範囲外: {value}"
    assert isinstance(macro["zero_hit_queries"], int)
    assert 0 <= macro["zero_hit_queries"] <= EXPECTED_QUERY_COUNT


@pytest.mark.parametrize("method", sorted(EXPECTED_METHODS))
def test_per_query_metrics_are_in_unit_range(method: str) -> None:
    baseline = _load_baseline()
    for pq in baseline["per_query"]:
        m = pq["results"][method]
        for key in ("recall5", "rr", "ndcg10"):
            value = m[key]
            assert 0.0 <= value <= 1.0, f"{pq['id']}/{method}[{key}] が [0,1] の範囲外: {value}"


def _is_monotonic(scores: list[float], direction: str) -> bool:
    """同点(tie)は許容した広義単調性を確認する。"""
    for i in range(1, len(scores)):
        if direction == "desc":
            if scores[i] > scores[i - 1]:
                return False
        else:
            if scores[i] < scores[i - 1]:
                return False
    return True


@pytest.mark.parametrize("method", sorted(EXPECTED_METHODS))
def test_scores_are_monotonically_ordered_per_ranking(method: str) -> None:
    baseline = _load_baseline()
    direction = METHOD_SORT_DIRECTION[method]
    for pq in baseline["per_query"]:
        m = pq["results"][method]
        for kind in ("raw", "folded"):
            scores = [row["score"] for row in m[kind]]
            ok = _is_monotonic(scores, direction)
            assert ok, f"{pq['id']}/{method}/{kind}: 並び順({direction})が崩れている: {scores}"


@pytest.mark.parametrize("method", sorted(EXPECTED_METHODS))
def test_folded_results_have_no_duplicate_documents(method: str) -> None:
    """foldToDocuments は post_number(無ければ path)単位で重複を落とすはず。"""
    baseline = _load_baseline()
    for pq in baseline["per_query"]:
        folded = pq["results"][method]["folded"]
        keys = [
            f"post:{row['post_number']}" if row["post_number"] else f"path:{row['path']}"
            for row in folded
        ]
        assert len(keys) == len(set(keys)), (
            f"{pq['id']}/{method}: folded リストに同一文書が重複している"
        )


def test_result_rows_have_required_fields() -> None:
    baseline = _load_baseline()
    required = {"path", "post_number", "chunk_id", "score", "start_line", "end_line", "matched_by"}
    for pq in baseline["per_query"]:
        for method in EXPECTED_METHODS:
            for kind in ("raw", "folded"):
                for row in pq["results"][method][kind]:
                    missing = required - set(row.keys())
                    assert not missing, f"{pq['id']}/{method}/{kind}: 必須フィールド不足: {missing}"
                    assert row["start_line"] <= row["end_line"]
                    assert isinstance(row["matched_by"], list) and len(row["matched_by"]) > 0


def test_hybrid_zero_hit_queries_do_not_exceed_bm25_raw() -> None:
    """統合検索は短語LIKE補助・ベクトルを併用するため、0件クエリは bm25_raw 以下のはず。"""
    baseline = _load_baseline()
    assert (
        baseline["macro"]["hybrid"]["zero_hit_queries"]
        <= baseline["macro"]["bm25_raw"]["zero_hit_queries"]
    )


# ===========================================================================
# 秘密情報スキャン(Task 2/3 と同じ方式: 本物の mask_secrets を全ファイルへ適用)
# ===========================================================================


def test_no_secrets_leak_into_eval_fixtures() -> None:
    checked = 0
    for path in [QUERIES_PATH, BASELINE_PATH]:
        text = path.read_text(encoding="utf-8")
        masked = mask_secrets(text)
        assert masked == text, (
            f"{path}: mask_secrets が反応する内容が残っている(秘密情報漏洩の疑い)"
        )
        checked += 1
    assert checked == 2
