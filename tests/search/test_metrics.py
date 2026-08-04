"""`infrastructure.search.metrics`(task-5-brief Step1)。

`tests/fixtures/eval/baseline.json` の `per_query[*].results[*].{recall5,rr,
ndcg10}` は旧 `tools/eval-search.js` が計算した値であり、ここで定義する式が
その値を独立に再現できることをもって実装の正しさを検証する
(fixture 自体は `tests/fixtures_check/test_eval_fixture.py` が別途健全性を検証済み)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.infrastructure.search.metrics import (
    document_key,
    fold_to_documents,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)

FIXTURES_EVAL_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval"
BASELINE_PATH = FIXTURES_EVAL_DIR / "baseline.json"
QUERIES_PATH = FIXTURES_EVAL_DIR / "queries.jsonl"


def _load_queries() -> dict[str, dict]:
    lines = [line for line in QUERIES_PATH.read_text(encoding="utf-8").split("\n") if line.strip()]
    return {json.loads(line)["id"]: json.loads(line) for line in lines}


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 単体
# ---------------------------------------------------------------------------


def test_document_key_prefers_post_number_over_path():
    assert document_key({"post_number": 42, "path": "a.md"}) == "post:42"
    assert document_key({"post_number": None, "path": "a.md"}) == "path:a.md"


def test_fold_to_documents_keeps_first_occurrence_and_drops_duplicates():
    rows = [
        {"post_number": 1, "path": "a.md", "chunk_id": 1},
        {"post_number": 1, "path": "a.md", "chunk_id": 2},
        {"post_number": 2, "path": "b.md", "chunk_id": 3},
    ]
    folded = fold_to_documents(rows)
    assert [r["chunk_id"] for r in folded] == [1, 3]


def test_recall_at_k_gives_partial_credit_for_multiple_relevant():
    folded = [{"post_number": 1, "path": "a.md"}, {"post_number": 3, "path": "c.md"}]
    relevant = [{"post_number": 1}, {"post_number": 2}]
    assert recall_at_k(folded, relevant, k=5) == pytest.approx(0.5)


def test_recall_at_k_empty_relevant_is_zero():
    assert recall_at_k([{"post_number": 1}], [], k=5) == 0.0


def test_reciprocal_rank_zero_when_not_found():
    assert reciprocal_rank([{"post_number": 9}], [{"post_number": 1}]) == 0.0


def test_reciprocal_rank_uses_first_matching_rank():
    folded = [{"post_number": 9}, {"post_number": 1}]
    assert reciprocal_rank(folded, [{"post_number": 1}]) == pytest.approx(0.5)


def test_ndcg_at_k_is_one_when_ideal_order():
    folded = [{"post_number": 1}, {"post_number": 2}, {"post_number": 9}]
    relevant = [{"post_number": 1}, {"post_number": 2}]
    assert ndcg_at_k(folded, relevant, k=10) == pytest.approx(1.0)


def test_ndcg_at_k_folding_prevents_score_above_one():
    """畳まずに2チャンク/文書を渡すと nDCG が1を超える、というbriefの警告を再現する。"""
    unfolded = [
        {"post_number": 1, "chunk_id": 1},
        {"post_number": 1, "chunk_id": 2},
        {"post_number": 2, "chunk_id": 3},
    ]
    relevant = [{"post_number": 1}]
    # 畳まずに計算すると、順位1と2の両方が同じ文書のヒットとしてカウントされうる
    # 実装(二値関連度でも重複した文書キーを弾かない単純な実装)を想定してもなお、
    # 畳んだ場合は必ず [0,1] に収まることを確認する。
    folded = fold_to_documents(unfolded)
    assert ndcg_at_k(folded, relevant, k=10) <= 1.0 + 1e-9


# ---------------------------------------------------------------------------
# baseline.json との突き合わせ(独立再現)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["bm25_raw", "hybrid"])
def test_metrics_reproduce_recorded_per_query_values(method: str) -> None:
    baseline = _load_baseline()
    queries = _load_queries()

    for pq in baseline["per_query"]:
        query = queries[pq["id"]]
        relevant = query["relevant"]
        recorded = pq["results"][method]
        folded = recorded["folded"]

        assert recall_at_k(folded, relevant, k=5) == pytest.approx(recorded["recall5"], abs=1e-9)
        assert reciprocal_rank(folded, relevant) == pytest.approx(recorded["rr"], abs=1e-9)
        assert ndcg_at_k(folded, relevant, k=10) == pytest.approx(recorded["ndcg10"], abs=1e-9)


@pytest.mark.parametrize("method", ["bm25_raw", "hybrid"])
def test_folding_raw_reproduces_recorded_folded_list(method: str) -> None:
    """`fold_to_documents(raw)` が記録済みの `folded` と同じ文書順を再現する。

    fixture の `folded` は上位10件に切り詰めて記録されている
    (`tests/fixtures_check/test_eval_fixture.py::test_both_methods_present_for_every_query`
    の `len(folded) <= 10` 制約参照)ため、こちらも同じ件数で比較する。
    """
    baseline = _load_baseline()
    for pq in baseline["per_query"]:
        recorded = pq["results"][method]
        refolded = fold_to_documents(recorded["raw"])
        limit = len(recorded["folded"])
        assert [document_key(r) for r in refolded[:limit]] == [
            document_key(r) for r in recorded["folded"]
        ]


@pytest.mark.parametrize("method", ["bm25_raw", "hybrid"])
def test_macro_average_reproduces_recorded_macro(method: str) -> None:
    baseline = _load_baseline()
    queries = _load_queries()
    recalls = []
    rrs = []
    ndcgs = []
    zero_hit = 0
    for pq in baseline["per_query"]:
        query = queries[pq["id"]]
        folded = pq["results"][method]["folded"]
        recalls.append(recall_at_k(folded, query["relevant"], k=5))
        rrs.append(reciprocal_rank(folded, query["relevant"]))
        ndcgs.append(ndcg_at_k(folded, query["relevant"], k=10))
        if not folded:
            zero_hit += 1

    macro = baseline["macro"][method]
    n = len(baseline["per_query"])
    assert sum(recalls) / n == pytest.approx(macro["recall5"], abs=1e-9)
    assert sum(rrs) / n == pytest.approx(macro["mrr"], abs=1e-9)
    assert sum(ndcgs) / n == pytest.approx(macro["ndcg10"], abs=1e-9)
    assert zero_hit == macro["zero_hit_queries"]
