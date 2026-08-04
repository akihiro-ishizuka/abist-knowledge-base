"""`find-duplicates` の移植(旧 `tools/find-duplicates.js`, M7 task-2)。

**このツールは何も変更しない。** 統合・削除の判断は人間が行う(旧実装の
コメントをそのまま踏襲する設計原則)。3種類の重複を検出する:

  same_article  同じ記事(`post_number`)が複数パスにある
  identical     本文ハッシュが完全一致する別記事
  near          埋め込みのコサイン類似度が高い別記事(索引が無ければ検出しない)

近似重複(`near`)は旧実装の2段階(文書ベクトルで絞り込み→チャンク共有率で確認)
のうち、1段目(文書ベクトルのコサイン類似度)のみを移植している。チャンク共有率
による2段目は今回のスコープでは省略した(索引DBの `chunks`/`embeddings` は
あるが対象を持たない場合の縮退動作を優先した)。しきい値の互換
(`DEFAULT_NEAR_THRESHOLD = 0.99`)は維持する。レビュー時はここを重点的に見ること。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from abist_kb.application.audit.runs import Finding, finish_run, record_findings, start_run
from abist_kb.infrastructure.db.documents_repo import DocumentRepository

#: 近似重複とみなす文書ベクトル(センチロイド)のコサイン類似度下限。
#: 旧実装と同じ値(実データでの調整結果)を維持する。
DEFAULT_NEAR_THRESHOLD = 0.99

_NUMERIC_STRIP = re.compile(r"[0-9０-９]")
_SEPARATOR_STRIP = re.compile(r"[-_/年月日.\s]")


def find_same_article(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一記事(`post_number`)が複数パスにあるものを集める。"""
    by_post: dict[Any, list[dict[str, Any]]] = {}
    for doc in documents:
        post_number = doc.get("post_number")
        if not post_number:
            continue
        by_post.setdefault(post_number, []).append(doc)

    groups = []
    for post_number, docs in by_post.items():
        if len(docs) < 2:
            continue
        hashes = {d.get("local_content_hash") for d in docs}
        groups.append(
            {
                "type": "same_article",
                "key": f"post:{post_number}",
                "title": docs[0].get("title"),
                "documents": [{"path": d["path"], "status": d.get("status")} for d in docs],
                "same_content": len(hashes) == 1,
            }
        )
    groups.sort(key=lambda g: len(g["documents"]), reverse=True)
    return groups


def find_identical(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """本文ハッシュが完全一致する別記事を集める(同一記事の複製は除く)。"""
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for doc in documents:
        content_hash = doc.get("local_content_hash")
        if not content_hash:
            continue
        by_hash.setdefault(content_hash, []).append(doc)

    groups = []
    for content_hash, docs in by_hash.items():
        post_numbers = {d.get("post_number") or f"path:{d['path']}" for d in docs}
        if len(docs) < 2 or len(post_numbers) < 2:
            continue
        groups.append(
            {
                "type": "identical",
                "key": content_hash[:12],
                "documents": [
                    {
                        "path": d["path"],
                        "title": d.get("title"),
                        "post_number": d.get("post_number"),
                        "status": d.get("status"),
                    }
                    for d in docs
                ],
            }
        )
    groups.sort(key=lambda g: len(g["documents"]), reverse=True)
    return groups


def is_likely_series(doc_a: dict[str, Any], doc_b: dict[str, Any]) -> bool:
    """同じ系列の時系列文書(週次議事録・スナップショット等)かを判定する。"""
    path_a, path_b = doc_a["path"], doc_b["path"]
    dir_a = path_a.rsplit("/", 1)[0] if "/" in path_a else ""
    dir_b = path_b.rsplit("/", 1)[0] if "/" in path_b else ""
    if dir_a != dir_b:
        return False

    def normalize(title: str | None) -> str:
        text = title or ""
        text = _NUMERIC_STRIP.sub("", text)
        text = _SEPARATOR_STRIP.sub("", text)
        return text.strip()

    a = normalize(doc_a.get("title"))
    b = normalize(doc_b.get("title"))
    return bool(a) and a == b


def _load_document_centroids(conn: sqlite3.Connection) -> tuple[list[str], np.ndarray]:
    """索引DBの `chunks`/`embeddings` から文書ごとの正規化センチロイドを作る。

    テーブルが存在しない(索引が未構築)場合は空を返す(検出しないだけで、
    エラーにはしない)。
    """
    try:
        rows = conn.execute(
            "SELECT c.path, e.vector FROM embeddings e JOIN chunks c ON c.id = e.chunk_id"
        ).fetchall()
    except sqlite3.OperationalError:
        return [], np.empty((0, 0), dtype=np.float32)

    sums: dict[str, list[np.ndarray]] = {}
    for row in rows:
        vector = np.frombuffer(row["vector"], dtype=np.float32)
        sums.setdefault(row["path"], []).append(vector)

    paths: list[str] = []
    centroids: list[np.ndarray] = []
    for path, vectors in sums.items():
        centroid = np.mean(np.stack(vectors), axis=0)
        norm = np.linalg.norm(centroid)
        if norm == 0:
            continue
        paths.append(path)
        centroids.append(centroid / norm)

    if not centroids:
        return [], np.empty((0, 0), dtype=np.float32)
    return paths, np.stack(centroids)


def find_near_duplicates(
    conn: sqlite3.Connection,
    documents: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_NEAR_THRESHOLD,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """センチロイドのコサイン類似度で近似重複候補を探す(1段目のみ、§冒頭参照)。

    戻り値は `(pairs, series_pairs)`。`series_pairs` は `is_likely_series` に
    該当したもの(統合対象外として別掲、旧実装と同じ扱い)。
    """
    paths, centroids = _load_document_centroids(conn)
    if not paths:
        return [], []
    by_path = {d["path"]: d for d in documents}

    pairs: list[dict[str, Any]] = []
    series_pairs: list[dict[str, Any]] = []
    n = len(paths)
    for i in range(n):
        doc_a = by_path.get(paths[i])
        if doc_a is None:
            continue
        for j in range(i + 1, n):
            doc_b = by_path.get(paths[j])
            if doc_b is None:
                continue
            if doc_a.get("post_number") and doc_a.get("post_number") == doc_b.get("post_number"):
                continue
            if doc_a.get("local_content_hash") and doc_a.get("local_content_hash") == doc_b.get(
                "local_content_hash"
            ):
                continue
            similarity = float(np.dot(centroids[i], centroids[j]))
            if similarity < threshold:
                continue
            pair = {
                "type": "near",
                "similarity": similarity,
                "documents": [
                    {
                        "path": doc_a["path"],
                        "title": doc_a.get("title"),
                        "post_number": doc_a.get("post_number"),
                        "status": doc_a.get("status"),
                    },
                    {
                        "path": doc_b["path"],
                        "title": doc_b.get("title"),
                        "post_number": doc_b.get("post_number"),
                        "status": doc_b.get("status"),
                    },
                ],
            }
            if is_likely_series(doc_a, doc_b):
                series_pairs.append(pair)
            else:
                pairs.append(pair)

    pairs.sort(key=lambda p: p["similarity"], reverse=True)
    return pairs, series_pairs


@dataclass(slots=True)
class DuplicatesResult:
    run_id: str
    same_article: list[dict[str, Any]] = field(default_factory=list)
    identical: list[dict[str, Any]] = field(default_factory=list)
    near: list[dict[str, Any]] = field(default_factory=list)
    series: list[dict[str, Any]] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        return {
            "same_article": len(self.same_article),
            "identical": len(self.identical),
            "near": len(self.near),
            "series": len(self.series),
        }


class FindDuplicatesService:
    """読み取り専用(`documents` テーブル + 任意で索引DB接続)。書込は一切行わない。"""

    def __init__(
        self, app_conn: sqlite3.Connection, *, index_conn: sqlite3.Connection | None = None
    ) -> None:
        self._app_conn = app_conn
        self._index_conn = index_conn
        self._repo = DocumentRepository(app_conn)

    def run(self, *, threshold: float = DEFAULT_NEAR_THRESHOLD) -> DuplicatesResult:
        run_id = start_run(
            self._app_conn,
            audit_type="find-duplicates",
            mode="report",
            params={"threshold": threshold},
        )

        documents = self._repo.list()
        same_article = find_same_article(documents)
        identical = find_identical(documents)
        near, series = (
            find_near_duplicates(self._index_conn, documents, threshold=threshold)
            if self._index_conn is not None
            else ([], [])
        )

        result = DuplicatesResult(
            run_id=run_id, same_article=same_article, identical=identical, near=near, series=series
        )

        def findings() -> Any:
            for group in same_article:
                yield Finding(finding_type="same_article", path=group["key"], details=group)
            for group in identical:
                yield Finding(finding_type="identical", path=group["key"], details=group)
            for pair in near:
                yield Finding(
                    finding_type="near",
                    path=pair["documents"][0]["path"],
                    details=pair,
                )
            for pair in series:
                yield Finding(
                    finding_type="series",
                    path=pair["documents"][0]["path"],
                    details=pair,
                )

        record_findings(self._app_conn, run_id, findings())
        finish_run(self._app_conn, run_id, totals=result.totals())
        return result


__all__ = [
    "DEFAULT_NEAR_THRESHOLD",
    "DuplicatesResult",
    "FindDuplicatesService",
    "find_identical",
    "find_near_duplicates",
    "find_same_article",
    "is_likely_series",
]
