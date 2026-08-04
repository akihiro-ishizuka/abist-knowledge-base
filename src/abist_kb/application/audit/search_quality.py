"""検索品質評価と tokenizer 比較(設計書 §9.2、task-5-brief Step2〜3)。

`evaluate()` は `tests/fixtures/eval/queries.jsonl` 形式の22クエリを `bm25_raw`
(FTS5のみ)/`hybrid`(統合検索)の両方式で実行し、`infrastructure.search.metrics`
(task-5-brief Step1で1本化した指標)で Recall@5 / MRR / nDCG@10 のマクロ平均を
出す。M4 の受け入れゲート(`tests/fixtures/eval/baseline.json` のマクロ平均との
比較)そのものはこのモジュールでは判定しない(実データが必要なため、判定は
`.superpowers/sdd/M4-index-search/task-4-5-report.md` で行う) — ここは
「同じ計算を本番の索引DB/検索経路に対して実行する」ための土台。

`compare_tokenizers()` は `unicode61` と `trigram` を同一条件(`bm25_raw` 相当)で
比較し、既定トークナイザーを変えるべきかの判断材料を出す(brief Step2後段)。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.application.search_service import (
    ProviderFactory,
    default_provider_factory,
    resolve_query_vector,
)
from abist_kb.infrastructure.search.metrics import (
    fold_to_documents,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)
from abist_kb.infrastructure.search.query_terms import is_pure_identifier_query
from abist_kb.infrastructure.search.search_engine import SearchOptions, keyword_search, search

#: brief 指定の2方式。`vector` 単体・`legacy` は対象外(M1 fixture 採取と同じ範囲)。
METHODS: tuple[str, ...] = ("bm25_raw", "hybrid")

#: 各方式が候補として取る件数(`search_engine.SearchOptions.candidate_limit` 既定値と同じ)。
CANDIDATE_LIMIT = 60

#: `folded` を報告に保持する上限件数(`tests/fixtures/eval/baseline.json` と同じ方針)。
FOLDED_REPORT_LIMIT = 10

#: 前回結果との比較で警告を出す劣化しきい値(brief Step2: 「-0.01 超」)。
DEGRADE_THRESHOLD = 0.01

#: このリポジトリでの22クエリ定義の既定置き場(brief Step3: 「実 docs/ のスナップショット」
#: を評価する際も、クエリ定義そのものは M1 が採取したこの fixture を使う)。
DEFAULT_QUERIES_RELATIVE_PATH = Path("tests/fixtures/eval/queries.jsonl")


def load_queries(path: Path) -> list[dict[str, Any]]:
    """`queries.jsonl` を読み込む(1行1クエリの JSON Lines)。"""
    lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    return [json.loads(line) for line in lines]


def _row(
    *,
    path: str,
    post_number: int | None,
    chunk_id: int,
    score: float,
    start_line: int | None,
    end_line: int | None,
    matched_by: Sequence[str],
) -> dict[str, Any]:
    return {
        "path": path,
        "post_number": post_number,
        "chunk_id": chunk_id,
        "score": score,
        "start_line": start_line,
        "end_line": end_line,
        "matched_by": list(matched_by),
    }


def run_bm25_raw(
    conn: Any,
    query_text: str,
    *,
    tokenizer: str = "trigram",
    candidate_limit: int = CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    """FTS5(BM25)のみの生ランキング(RRF/ブースト/重複排除を経由しない)。"""
    options = SearchOptions(candidate_limit=candidate_limit, tokenizer=tokenizer)
    rows = keyword_search(conn, query_text, options)
    return [
        _row(
            path=r["path"],
            post_number=r["post_number"],
            chunk_id=r["chunk_id"],
            score=r["bm25"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            matched_by=["keyword"],
        )
        for r in rows
    ]


def run_hybrid(
    conn: Any,
    query_text: str,
    *,
    docs_dir: Path,
    embedding_provider_factory: ProviderFactory = default_provider_factory,
    candidate_limit: int = CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    """統合検索(BM25 + 短語LIKE補助 + ベクトル + RRF + ブースト + 重複排除)の生ランキング。

    本番の `SearchService.search` と同じベクトル解決(`resolve_query_vector`)を使う
    ため、評価と本番経路のずれが起きない。`options.limit` を `candidate_limit` に
    広げて、最終的な `fold_to_documents` に十分な候補を渡す。
    """
    identifier_only = is_pure_identifier_query(query_text)
    query_vector = resolve_query_vector(
        conn,
        query_text,
        identifier_only=identifier_only,
        embedding_provider_factory=embedding_provider_factory,
    )
    options = SearchOptions(limit=candidate_limit)
    result = search(conn, query_text, query_vector=query_vector, docs_dir=docs_dir, options=options)
    return [
        _row(
            path=r["path"],
            post_number=r["post_number"],
            chunk_id=r["chunk_id"],
            score=r["score"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            matched_by=r["matched_by"],
        )
        for r in result.results
    ]


def _score_query(raw: list[dict[str, Any]], relevant: list[dict[str, Any]]) -> dict[str, Any]:
    folded = fold_to_documents(raw)
    return {
        "raw": raw,
        "folded": folded[:FOLDED_REPORT_LIMIT],
        "recall5": recall_at_k(folded, relevant, k=5),
        "rr": reciprocal_rank(folded, relevant),
        "ndcg10": ndcg_at_k(folded, relevant, k=10),
        "_zero_hit": not folded,
    }


def evaluate(
    conn: Any,
    queries: Sequence[dict[str, Any]],
    *,
    docs_dir: Path,
    methods: Sequence[str] = METHODS,
    embedding_provider_factory: ProviderFactory = default_provider_factory,
) -> dict[str, Any]:
    """`queries` を `methods` の各方式で実行し、per-query結果とマクロ平均を返す。"""
    per_query: list[dict[str, Any]] = []
    accum: dict[str, dict[str, list[float] | int]] = {
        m: {"recall5": [], "rr": [], "ndcg10": [], "zero_hit": 0} for m in methods
    }

    for q in queries:
        relevant = q["relevant"]
        results: dict[str, Any] = {}
        for method in methods:
            if method == "bm25_raw":
                raw = run_bm25_raw(conn, q["query"])
            elif method == "hybrid":
                raw = run_hybrid(
                    conn,
                    q["query"],
                    docs_dir=docs_dir,
                    embedding_provider_factory=embedding_provider_factory,
                )
            else:
                raise ValueError(f"未知の評価方式です: {method!r}")

            scored = _score_query(raw, relevant)
            zero_hit = scored.pop("_zero_hit")
            accum[method]["recall5"].append(scored["recall5"])  # type: ignore[union-attr]
            accum[method]["rr"].append(scored["rr"])  # type: ignore[union-attr]
            accum[method]["ndcg10"].append(scored["ndcg10"])  # type: ignore[union-attr]
            if zero_hit:
                accum[method]["zero_hit"] += 1  # type: ignore[operator]
            results[method] = scored

        per_query.append(
            {"id": q["id"], "query": q["query"], "type": q.get("type"), "results": results}
        )

    n = len(queries)
    macro: dict[str, Any] = {}
    for method in methods:
        values = accum[method]
        recalls = values["recall5"]
        rrs = values["rr"]
        ndcgs = values["ndcg10"]
        macro[method] = {
            "recall5": (sum(recalls) / n) if n else 0.0,  # type: ignore[operator]
            "mrr": (sum(rrs) / n) if n else 0.0,  # type: ignore[operator]
            "ndcg10": (sum(ndcgs) / n) if n else 0.0,  # type: ignore[operator]
            "zero_hit_queries": values["zero_hit"],
        }

    return {
        "schema": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "query_count": n,
        "methods": list(methods),
        "macro": macro,
        "per_query": per_query,
    }


def compare_with_previous(
    report: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    threshold: float = DEGRADE_THRESHOLD,
) -> list[dict[str, Any]]:
    """前回の `macro` と比較し、`recall5`/`mrr`/`ndcg10` が `threshold` 超の悪化を警告する。"""
    if previous is None:
        return []
    warnings: list[dict[str, Any]] = []
    for method, current_metrics in report["macro"].items():
        previous_metrics = previous.get("macro", {}).get(method)
        if previous_metrics is None:
            continue
        for metric in ("recall5", "mrr", "ndcg10"):
            delta = current_metrics[metric] - previous_metrics[metric]
            if delta < -threshold:
                warnings.append(
                    {
                        "method": method,
                        "metric": metric,
                        "previous": previous_metrics[metric],
                        "current": current_metrics[metric],
                        "delta": delta,
                    }
                )
    return warnings


def find_latest_report(reports_dir: Path) -> Path | None:
    """`reports/eval/eval-*.json` のうち最新のものを返す(無ければ `None`)。"""
    eval_dir = reports_dir / "eval"
    if not eval_dir.is_dir():
        return None
    candidates = sorted(eval_dir.glob("eval-*.json"))
    return candidates[-1] if candidates else None


def _sanitize_stamp(value: str) -> str:
    return re.sub(r"[:.]", "-", value)


def write_report(report: dict[str, Any], *, reports_dir: Path) -> Path:
    """`reports/eval/eval-<timestamp>.json` へ書き出す。"""
    eval_dir = reports_dir / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    stamp = _sanitize_stamp(report["generated_at"])
    file_path = eval_dir / f"eval-{stamp}.json"
    file_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return file_path


def compare_tokenizers(
    conn: Any,
    queries: Sequence[dict[str, Any]],
    *,
    tokenizers: Sequence[str] = ("unicode61", "trigram"),
) -> dict[str, Any]:
    """`unicode61`/`trigram` を同条件(`bm25_raw` 相当)で比較する(brief Step2後段)。"""
    macro: dict[str, Any] = {}
    for tokenizer in tokenizers:
        recalls: list[float] = []
        rrs: list[float] = []
        ndcgs: list[float] = []
        zero_hit = 0
        for q in queries:
            raw = run_bm25_raw(conn, q["query"], tokenizer=tokenizer)
            folded = fold_to_documents(raw)
            recalls.append(recall_at_k(folded, q["relevant"], k=5))
            rrs.append(reciprocal_rank(folded, q["relevant"]))
            ndcgs.append(ndcg_at_k(folded, q["relevant"], k=10))
            if not folded:
                zero_hit += 1
        n = len(queries)
        macro[tokenizer] = {
            "recall5": (sum(recalls) / n) if n else 0.0,
            "mrr": (sum(rrs) / n) if n else 0.0,
            "ndcg10": (sum(ndcgs) / n) if n else 0.0,
            "zero_hit_queries": zero_hit,
        }
    return {"schema": 1, "query_count": len(queries), "tokenizers": macro}


__all__ = [
    "CANDIDATE_LIMIT",
    "DEFAULT_QUERIES_RELATIVE_PATH",
    "DEGRADE_THRESHOLD",
    "FOLDED_REPORT_LIMIT",
    "METHODS",
    "compare_tokenizers",
    "compare_with_previous",
    "evaluate",
    "find_latest_report",
    "load_queries",
    "run_bm25_raw",
    "run_hybrid",
    "write_report",
]
