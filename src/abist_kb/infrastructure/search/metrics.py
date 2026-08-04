"""検索評価指標の単一実装(task-5-brief Step1)。

旧版はこれを3ファイル(`tools/eval-search.js`・`tools/compare-tokenizers.js`他)に
重複させていた。`recall_at_k`/`reciprocal_rank`/`ndcg_at_k`/`fold_to_documents` を
ここへ1本化する。

**畳んでからスコアリングする(brief Step1の核心)。** `fold_to_documents` は
チャンク単位の結果を `post:<n>`(無ければ `path:<p>`)でグループ化し、最初に
現れた(=最もスコアが良い)チャンクだけを残して文書単位へ畳む。指標は必ず
畳んだ後のリストに対して計算すること — 畳まずに計算すると、hybrid の
「1文書あたり最大2チャンク」という重複排除の仕様により同一文書が2回
カウントされ、nDCG が1を超えてしまう(brief に明記された既知の落とし穴)。

`tests/fixtures/eval/baseline.json` の `per_query[*].results[*].{recall5,rr,
ndcg10}` はここで定義する式で計算されたものであり、`tests/search/
test_metrics.py` はこの実装を fixture の値と突き合わせて検証する。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def document_key(row: Mapping[str, Any]) -> str:
    """`post:<post_number>`(無ければ `path:<path>`)の文書識別キー。

    `search_engine.deduplicate` の `document_key` と同じ規約(brief Step1)。
    """
    post_number = row.get("post_number")
    if post_number:
        return f"post:{post_number}"
    return f"path:{row.get('path')}"


def fold_to_documents(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """チャンク単位の結果を文書単位へ畳む(先頭に現れたチャンクを代表とする)。

    呼び出し元は事前に `rows` をスコア順(良い順)へ並べておくこと——ここでは
    並べ替えを行わず、「最初に見つかったもの」をそのまま代表として残す。
    """
    seen: set[str] = set()
    folded: list[Mapping[str, Any]] = []
    for row in rows:
        key = document_key(row)
        if key in seen:
            continue
        seen.add(key)
        folded.append(row)
    return folded


def _relevant_keys(relevant: Iterable[Mapping[str, Any]]) -> set[str]:
    return {document_key(item) for item in relevant}


def recall_at_k(
    folded: Sequence[Mapping[str, Any]], relevant: Iterable[Mapping[str, Any]], k: int = 5
) -> float:
    """`folded`(畳み済み、スコア順)の上位 `k` 件のうち、`relevant` を何割拾えたか。

    `relevant` が複数件の文書からなる場合、部分的に拾えた分だけ按分する
    (2件中1件しか拾えなければ0.5)。`relevant` が空なら未定義のため0.0とする。
    """
    keys = _relevant_keys(relevant)
    if not keys:
        return 0.0
    top_keys = {document_key(row) for row in folded[:k]}
    return len(top_keys & keys) / len(keys)


def reciprocal_rank(
    folded: Sequence[Mapping[str, Any]], relevant: Iterable[Mapping[str, Any]]
) -> float:
    """最初に relevant が現れた順位の逆数(MRR の1クエリ分)。無ければ0.0。"""
    keys = _relevant_keys(relevant)
    for index, row in enumerate(folded, start=1):
        if document_key(row) in keys:
            return 1.0 / index
    return 0.0


def ndcg_at_k(
    folded: Sequence[Mapping[str, Any]], relevant: Iterable[Mapping[str, Any]], k: int = 10
) -> float:
    """二値関連度(relevant=1/それ以外=0)の nDCG@k。"""
    keys = _relevant_keys(relevant)
    if not keys:
        return 0.0
    dcg = sum(
        1.0 / math.log2(index + 1)
        for index, row in enumerate(folded[:k], start=1)
        if document_key(row) in keys
    )
    ideal_hits = min(len(keys), k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


__all__ = [
    "document_key",
    "fold_to_documents",
    "ndcg_at_k",
    "recall_at_k",
    "reciprocal_rank",
]
