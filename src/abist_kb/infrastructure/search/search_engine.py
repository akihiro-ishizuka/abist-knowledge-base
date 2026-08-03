"""統合検索(設計書 §9.2、`design/plans/M4-index-search.md` Task3)。

全文検索(FTS5/BM25)とベクトル検索を RRF で統合し、タイトル・識別子の完全一致と
アクティブ状態でブーストする。埋め込みが無い環境でも全文検索だけで動く
(ベクトルは任意)。旧 `tools/lib/search-engine.js` の移植であり、チューニング値
(重み・ブースト量・しきい値)は旧版の実測に基づく既定値をそのまま踏襲する。
**これらの値を「改善」しないこと**——`tests/fixtures/eval/baseline.json` の
Recall@5 ベースライン(0.9545)は旧版のこれらの値で測定されたものであり、
値を変えるとベースラインとの比較が意味を失う。

出典の信頼性:
    結果には必ず path / heading_path / start_line / end_line を含める。
    索引時の本文ハッシュ(`document_hash`)と現在のファイルを `check_index_stale`
    で比べ、食い違えば `index_stale` を立てて「行番号を信用してはいけない」ことを
    呼び出し側に伝える(M7 の可視化パイプラインが行範囲を検証する際の入力になる)。
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from abist_kb.domain.frontmatter import hash_body
from abist_kb.infrastructure.search.index_schema import fts_table_name
from abist_kb.infrastructure.search.query_terms import (
    extract_query_terms,
    identifier_terms,
    is_pure_identifier_query,
)

#: 2文字の漢字/カタカナ語だけを補助検索(LIKE)の対象にする(trigram の穴埋め)。
_SHORT_TERM_RE = re.compile(r"^[一-鿿ァ-ヶー]{2}$")

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SearchOptions:
    """検索の全チューニング値とフィルタ条件。既定値は旧版の実測に基づく。"""

    limit: int = 10
    #: RRF の定数。順位差を緩やかにするための一般的な既定値。
    rrf_k: int = 60
    #: 1文書から返すチャンクの上限(同じ文書で埋め尽くさないため)。
    max_chunks_per_document: int = 2
    #: 候補として各手法から取る件数。
    candidate_limit: int = 60
    #: ブースト量(RRF スコアに加算)。RRF は最大でも約0.016なので、
    #: 0.08 の識別子ブーストが支配的になるのは意図的な非対称。
    title_boost: float = 0.05
    identifier_boost: float = 0.08
    active_boost: float = 0.01
    tokenizer: str = "trigram"
    # -- フィルタ(3つのランキング全てに同じ条件を適用する) ------------------
    source: str | None = None
    document_type: str | None = None
    status: str | None = None
    path_prefix: str | None = None


DEFAULT_SEARCH_OPTIONS = SearchOptions()


def _resolve_options(options: SearchOptions | Mapping[str, Any] | None) -> SearchOptions:
    if options is None:
        return DEFAULT_SEARCH_OPTIONS
    if isinstance(options, SearchOptions):
        return options
    return replace(DEFAULT_SEARCH_OPTIONS, **dict(options))


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _to_match_expression(query: object, tokenizer: str) -> str:
    terms = extract_query_terms(query)
    usable = [t for t in terms if len(t) >= 3] if tokenizer == "trigram" else terms
    if not usable:
        return _quote(str(query).strip() or " ")
    return " OR ".join(_quote(t) for t in usable)


def _build_filter_sql(opts: SearchOptions) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if opts.source:
        clauses.append("d.source = ?")
        params.append(opts.source)
    if opts.document_type:
        clauses.append("d.document_type = ?")
        params.append(opts.document_type)
    if opts.status:
        clauses.append("d.status = ?")
        params.append(opts.status)
    if opts.path_prefix:
        clauses.append("c.path LIKE ?")
        params.append(f"{str(opts.path_prefix).rstrip('/')}%")
    sql = " AND " + " AND ".join(clauses) if clauses else ""
    return sql, params


_ROW_COLUMNS = """
    c.id AS chunk_id, c.path, c.chunk_index, c.title, c.heading_path,
    c.text, c.start_line, c.end_line, c.content_hash,
    d.post_number, d.status, d.source, d.document_type, d.url,
    d.document_hash, d.indexed_at, d.canonical_path
"""


def keyword_search(
    db: sqlite3.Connection,
    query: object,
    options: SearchOptions | Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """全文検索(BM25)。`bm25()` は負値を返すため昇順(小さいほど良い)。"""
    opts = _resolve_options(options)
    table = fts_table_name(opts.tokenizer)
    filter_sql, filter_params = _build_filter_sql(opts)

    sql = f"""
        SELECT {_ROW_COLUMNS},
               bm25({table}, 1.0, 2.0, 3.0) AS bm25
          FROM {table}
          JOIN chunks    c ON c.id = {table}.rowid
          JOIN documents d ON d.path = c.path
         WHERE {table} MATCH ?{filter_sql}
         ORDER BY bm25
         LIMIT ?
    """
    match_expr = _to_match_expression(query, opts.tokenizer)
    rows = db.execute(sql, (match_expr, *filter_params, opts.candidate_limit)).fetchall()
    return [dict(row) for row in rows]


def short_term_search(
    db: sqlite3.Connection,
    query: object,
    options: SearchOptions | Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """短い語の完全一致補助検索(LIKE)。

    FTS5 の trigram は3文字未満を索引できないため、「蛇腹」「要件」「図面」
    のような2文字の日本語が MATCH から丸ごと落ちる。業務でよく使う語がここに
    集中するので、LIKE による部分一致で補う。対象語が無ければ空を返す
    (純 ASCII クエリで全表走査を起こさないため)。全走査になるが対象は
    2文字語のみで、件数も `candidate_limit` で抑える。
    """
    opts = _resolve_options(options)
    short_terms = [t for t in extract_query_terms(query) if len(t) == 2 and _SHORT_TERM_RE.match(t)]
    if not short_terms:
        return []

    filter_sql, filter_params = _build_filter_sql(opts)
    conditions = " OR ".join(
        "(c.text LIKE ? OR c.title LIKE ? OR c.heading_path LIKE ?)" for _ in short_terms
    )
    like_params: list[str] = []
    for term in short_terms:
        like_params.extend([f"%{term}%", f"%{term}%", f"%{term}%"])

    # 一致した語数が多いチャンクを優先する
    score_expr = " + ".join(
        "(CASE WHEN c.text LIKE ? OR c.title LIKE ? OR c.heading_path LIKE ? THEN 1 ELSE 0 END)"
        for _ in short_terms
    )
    score_params: list[str] = []
    for term in short_terms:
        score_params.extend([f"%{term}%", f"%{term}%", f"%{term}%"])

    sql = f"""
        SELECT {_ROW_COLUMNS},
               ({score_expr}) AS term_hits
          FROM chunks c
          JOIN documents d ON d.path = c.path
         WHERE ({conditions}){filter_sql}
         ORDER BY term_hits DESC, length(c.text) ASC
         LIMIT ?
    """
    params = [*score_params, *like_params, *filter_params, opts.candidate_limit]
    rows = db.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# ベクトル検索
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _VectorCacheEntry:
    conn: sqlite3.Connection
    count: int
    ids: np.ndarray
    matrix: np.ndarray | None
    dimensions: int


#: プロセス寿命でキャッシュするベクトル行列。`sqlite3.Connection` は弱参照や
#: 動的属性付与に対応しない C 拡張型のため、`id(conn)` をキーにした通常の辞書で
#: 保持し、値に `conn` への強参照を持たせて `id()` の再利用による取り違えを防ぐ
#: (`conn` が GC されない限り同じ id が別の接続へ再利用されることはない)。
#: 索引が更新されたら作り直す必要があるため、`embeddings` の件数が変わったら破棄する。
_VECTOR_CACHE: dict[int, _VectorCacheEntry] = {}


def _from_blob(blob: bytes) -> np.ndarray:
    """Float32 リトルエンディアン BLOB → ndarray。"""
    return np.frombuffer(blob, dtype="<f4")


def _load_vector_matrix(db: sqlite3.Connection) -> _VectorCacheEntry:
    count = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    cached = _VECTOR_CACHE.get(id(db))
    if cached is not None and cached.conn is db and cached.count == count:
        return cached

    rows = db.execute("SELECT chunk_id, vector FROM embeddings ORDER BY chunk_id").fetchall()
    if not rows:
        entry = _VectorCacheEntry(
            conn=db, count=0, ids=np.array([], dtype=np.int64), matrix=None, dimensions=0
        )
        _VECTOR_CACHE[id(db)] = entry
        return entry

    dimensions = int(_from_blob(rows[0]["vector"]).shape[0])
    matrix = np.empty((len(rows), dimensions), dtype=np.float32)
    ids = np.empty(len(rows), dtype=np.int64)
    for i, row in enumerate(rows):
        ids[i] = row["chunk_id"]
        matrix[i] = _from_blob(row["vector"])

    entry = _VectorCacheEntry(conn=db, count=count, ids=ids, matrix=matrix, dimensions=dimensions)
    _VECTOR_CACHE[id(db)] = entry
    return entry


def vector_search(
    db: sqlite3.Connection,
    query_vector: Sequence[float] | None,
    options: SearchOptions | Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """ベクトル検索(全チャンク線形走査)。

    `query_vector` が無い、埋め込みが1件も無い、次元が合わない場合は空配列を
    返す(全文検索だけで動く)。呼び出し元(`search()`)が識別子だけのクエリでは
    そもそもこの関数を呼ばない(brief Step2: 丸ごとスキップが仕様)。
    """
    opts = _resolve_options(options)
    if query_vector is None:
        return []

    entry = _load_vector_matrix(db)
    if entry.count == 0 or entry.matrix is None:
        return []

    qvec = np.asarray(query_vector, dtype=np.float32)
    if qvec.shape[0] != entry.dimensions:
        return []

    scores = entry.matrix @ qvec
    limit = min(opts.candidate_limit, scores.shape[0])
    if limit <= 0:
        return []

    top_idx = np.argpartition(-scores, limit - 1)[:limit]
    top_idx = top_idx[np.argsort(-scores[top_idx])]

    chunk_ids = [int(entry.ids[i]) for i in top_idx]
    score_by_id = {
        chunk_id: float(scores[i]) for chunk_id, i in zip(chunk_ids, top_idx, strict=True)
    }

    filter_sql, filter_params = _build_filter_sql(opts)
    placeholders = ",".join("?" for _ in chunk_ids)
    sql = f"""
        SELECT {_ROW_COLUMNS}
          FROM chunks    c
          JOIN documents d ON d.path = c.path
         WHERE c.id IN ({placeholders}){filter_sql}
    """
    rows = db.execute(sql, [*chunk_ids, *filter_params]).fetchall()
    by_id = {row["chunk_id"]: dict(row) for row in rows}

    results: list[dict[str, Any]] = []
    for chunk_id in chunk_ids:
        row = by_id.get(chunk_id)
        if row is not None:
            row = dict(row)
            row["similarity"] = score_by_id[chunk_id]
            results.append(row)
    return results


# ---------------------------------------------------------------------------
# RRF・ブースト・重複排除
# ---------------------------------------------------------------------------

#: 1つのランキング: (手法名, 順位順の行のリスト)。
Ranking = tuple[str, Sequence[Mapping[str, Any]]]


@dataclass(slots=True)
class FusedEntry:
    """RRF 統合後の1エントリ。ブースト・重複排除の過程で `score`/`boosts`/
    `other_paths`/`document_key` が書き換わる。
    """

    row: dict[str, Any]
    score: float
    sources: list[str] = field(default_factory=list)
    boosts: list[str] = field(default_factory=list)
    other_paths: list[str] = field(default_factory=list)
    document_key: str | None = None


def fuse_rrf(rankings: Sequence[Ranking], k: int = 60) -> list[FusedEntry]:
    """RRF で複数のランキングを統合する。

    スコアの絶対値が比較できない手法同士でも順位だけで混ぜられる。
    空のランキングは呼び出し元で除外しておくこと(含めても無害だが意味が無い)。
    """
    scores: dict[Any, FusedEntry] = {}
    for method, rows in rankings:
        for index, row in enumerate(rows):
            chunk_id = row["chunk_id"]
            entry = scores.get(chunk_id)
            if entry is None:
                entry = FusedEntry(row=dict(row), score=0.0)
                scores[chunk_id] = entry
            entry.score += 1 / (k + index + 1)
            entry.sources.append(method)
    return sorted(scores.values(), key=lambda e: e.score, reverse=True)


def apply_boosts(
    entries: Sequence[FusedEntry],
    query: object,
    options: SearchOptions | Mapping[str, Any] | None = None,
) -> list[FusedEntry]:
    """タイトル・識別子の完全一致と active 状態でブーストする(RRF スコアに加算)。"""
    opts = _resolve_options(options)
    normalized_query = str(query).strip().lower()
    identifiers = [t.lower() for t in identifier_terms(query)]

    for entry in entries:
        row = entry.row
        title = str(row.get("title") or "").lower()
        text = str(row.get("text") or "").lower()
        heading = str(row.get("heading_path") or "").lower()
        entry.boosts = []

        if title == normalized_query or heading.endswith(normalized_query):
            entry.score += opts.title_boost
            entry.boosts.append("title_exact")
        elif normalized_query and normalized_query in title:
            entry.score += opts.title_boost / 2
            entry.boosts.append("title_partial")

        for identifier in identifiers:
            if identifier in text or identifier in title:
                entry.score += opts.identifier_boost
                entry.boosts.append(f"identifier:{identifier}")
                break

        if row.get("status") == "active":
            entry.score += opts.active_boost
            entry.boosts.append("active")

    return sorted(entries, key=lambda e: e.score, reverse=True)


def deduplicate(
    entries: Sequence[FusedEntry],
    options: SearchOptions | Mapping[str, Any] | None = None,
) -> list[FusedEntry]:
    """重複を落とす。

    1. 同一内容のチャンク(`content_hash`)
    2. 同一記事の別パス(`post_number`。バッチ間でカテゴリが重なるため実体が
       複数ある。無ければ `canonical_path`/`path` で束ねる)
    3. 1文書あたりのチャンク数上限

    落とした側のパスは残った側の `other_paths` に集約する(呼び出し側が
    「この記事は他のパスにも実在する」ことを知れるようにするため)。
    """
    opts = _resolve_options(options)
    seen_content: set[Any] = set()
    per_document: dict[str, int] = {}
    duplicate_paths: dict[str, dict[str, None]] = {}
    results: list[FusedEntry] = []

    for entry in entries:
        row = entry.row
        document_key = (
            f"post:{row['post_number']}"
            if row.get("post_number")
            else f"path:{row.get('canonical_path') or row['path']}"
        )

        if row.get("content_hash") in seen_content:
            duplicate_paths.setdefault(document_key, {})[row["path"]] = None
            continue

        used = per_document.get(document_key, 0)
        if used >= opts.max_chunks_per_document:
            duplicate_paths.setdefault(document_key, {})[row["path"]] = None
            continue

        seen_content.add(row.get("content_hash"))
        per_document[document_key] = used + 1
        entry.document_key = document_key
        results.append(entry)

    for entry in results:
        others = duplicate_paths.get(entry.document_key or "")
        entry.other_paths = [p for p in others if p != entry.row["path"]] if others else []

    return results


def make_snippet(text: object, query: object, max_length: int = 240) -> str:
    """検索語の周辺を抜き出したスニペット。"""
    text_str = str(text)
    terms = extract_query_terms(query)
    lower = text_str.lower()

    position = -1
    for term in terms:
        found = lower.find(term.lower())
        if found >= 0 and (position == -1 or found < position):
            position = found
    if position == -1:
        position = 0

    start = max(0, position - max_length // 3)
    snippet = _WHITESPACE_RE.sub(" ", text_str[start : start + max_length]).strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + max_length < len(text_str) else ""
    return prefix + snippet + suffix


def check_index_stale(
    docs_dir: Path | str, relative_path: str, indexed_hash: str | None
) -> tuple[bool, str | None]:
    """索引時の本文と現在のファイルを比べ、行番号が信用できるかを判定する。

    ファイルが読めない場合も stale 扱いにする(信用できないことに変わりはない)。
    戻り値は `(index_stale, current_hash)`。
    """
    try:
        content = (Path(docs_dir) / relative_path).read_text(encoding="utf-8")
    except OSError:
        return True, None
    current = hash_body(content)
    return current != indexed_hash, current


# ---------------------------------------------------------------------------
# 統合検索
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SearchResult:
    results: list[dict[str, Any]]
    diagnostics: dict[str, Any]


def search(
    db: sqlite3.Connection,
    query: object,
    *,
    query_vector: Sequence[float] | None = None,
    docs_dir: Path | str | None = None,
    options: SearchOptions | Mapping[str, Any] | None = None,
) -> SearchResult:
    """統合検索を実行する。

    `query_vector`/`docs_dir` はどちらも任意。埋め込みが無い、または渡されない
    環境では全文検索だけで動く。`docs_dir` を渡さない場合 `index_stale` は
    `None`(判定不能)になる。
    """
    opts = _resolve_options(options)
    started = time.perf_counter()

    keyword_rows = keyword_search(db, query, opts)
    short_term_rows = short_term_search(db, query, opts)

    # 識別子だけのクエリは完全一致が答え。意味的近傍を混ぜると薄まるので使わない
    # (旧版の実測: 混ぜると識別子クエリの Recall@5 が 1.000 → 0.917 に低下)。
    identifier_only = is_pure_identifier_query(query)
    vector_rows = [] if identifier_only else vector_search(db, query_vector, opts)

    rankings: list[Ranking] = [("keyword", keyword_rows)]
    if short_term_rows:
        rankings.append(("short_term", short_term_rows))
    if vector_rows:
        rankings.append(("vector", vector_rows))

    fused = fuse_rrf(rankings, opts.rrf_k)
    boosted = apply_boosts(fused, query, opts)
    deduped = deduplicate(boosted, opts)[: opts.limit]

    results: list[dict[str, Any]] = []
    for entry in deduped:
        row = entry.row
        if docs_dir is not None:
            stale, _current_hash = check_index_stale(
                docs_dir, row["path"], row.get("document_hash")
            )
        else:
            stale = None

        results.append(
            {
                "path": row["path"],
                "title": row.get("title"),
                "heading_path": row.get("heading_path"),
                "start_line": row.get("start_line"),
                "end_line": row.get("end_line"),
                "snippet": make_snippet(row.get("text") or "", query),
                "url": row.get("url"),
                "status": row.get("status"),
                "source": row.get("source"),
                "document_type": row.get("document_type"),
                "post_number": row.get("post_number"),
                "document_hash": row.get("document_hash"),
                "indexed_at": row.get("indexed_at"),
                "index_stale": stale,
                "score": round(entry.score, 6),
                "matched_by": list(dict.fromkeys(entry.sources)),
                "boosts": entry.boosts,
                "other_paths": entry.other_paths,
                "chunk_id": row["chunk_id"],
            }
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    diagnostics = {
        "query": query,
        "terms": extract_query_terms(query),
        "tokenizer": opts.tokenizer,
        "keyword_candidates": len(keyword_rows),
        "short_term_candidates": len(short_term_rows),
        "vector_candidates": len(vector_rows),
        "identifier_only_query": identifier_only,
        "vector_available": len(vector_rows) > 0,
        "elapsed_ms": round(elapsed_ms, 1),
    }

    return SearchResult(results=results, diagnostics=diagnostics)


__all__ = [
    "DEFAULT_SEARCH_OPTIONS",
    "FusedEntry",
    "Ranking",
    "SearchOptions",
    "SearchResult",
    "apply_boosts",
    "check_index_stale",
    "deduplicate",
    "fuse_rrf",
    "keyword_search",
    "make_snippet",
    "search",
    "short_term_search",
    "vector_search",
]
