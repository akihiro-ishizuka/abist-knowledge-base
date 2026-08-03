"""索引構築・差分更新・canonical_path(設計書 §9.2、`design/plans/M4-index-search.md` Task1)。

`IndexBuilder.build` は1回の呼び出しで:

1. `rows`(`corpus.select_work_targets`/`select_reference_targets` が返す対象)に
   無くなった文書を `documents`/`chunks`/`embeddings` ごと削除する(brief Step3後段)。
2. `rows` の各行について、`document_hash`(= `hash_body`)が変わっていなければ
   再チャンクせず列メタデータだけ更新する高速パスを取り、変わっていれば
   **同一トランザクションで** 旧チャンクの `embeddings` を `chunk_id` 指定で
   削除してから再チャンク・再挿入する(brief Step3前段。旧版はこれを怠り
   `chunks`(51,391件)と `embeddings`(51,411件)の差=20件の孤児を残した。
   `chunk_id` は再挿入のたびに新しい rowid を振られるため、削除を後回しにすると
   古い `chunk_id` を指す `embeddings` 行が参照先を失ったまま残り続ける)。
3. `post_number` でグループ化し辞書順最小のパスを `canonical_path` とする
   (brief Step4。未変更の高速パスの文書も対象に含む——決定性のための規則であって
   品質判断ではないため、変更の有無によらず毎回計算し直す)。
4. `chunks_fts_*` を `'rebuild'` で作り直す(external-content はトリガを持たない
   ため、`chunks` への書込は自動反映されない)。
5. `meta` へ `schema_version`/`tokenizers`/`last_indexed_at`/`chunk_options` を書く
   (`embedding_*` は Task2 の埋め込みプロバイダが生成後に書く領分なので、
   ここでは触れない)。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.domain.frontmatter import hash_body
from abist_kb.infrastructure.db.connection import transaction
from abist_kb.infrastructure.search.chunker import (
    DEFAULT_CHUNK_OPTIONS,
    Chunk,
    ChunkOptions,
    chunk_markdown,
)
from abist_kb.infrastructure.search.index_schema import (
    DEFAULT_TOKENIZERS,
    INDEX_SCHEMA_VERSION,
    rebuild_fts_indexes,
)

EmitFn = Callable[..., None]
CheckLeaseFn = Callable[[], None]


@dataclass(slots=True)
class IndexSummary:
    """1回の `IndexBuilder.build` 呼び出しの集計。"""

    documents_added: int = 0
    documents_updated: int = 0
    documents_unchanged: int = 0
    documents_removed: int = 0
    chunks_added: int = 0
    chunks_removed: int = 0
    embeddings_removed: int = 0
    canonical_paths_updated: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)


def resolve_canonical_paths(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """`post_number` でグループ化し、辞書順で最小のパスを `canonical_path` とする。

    `post_number` が `None` の行はグループ化の対象外(brief: 「NULLはスキップ」)
    で、自分自身が `canonical_path` になる。同じ記事が複数パスに実在する現実を
    決定的に代表1件へ畳むための規則であり、どのパスが「良い」かの品質判断ではない。
    """
    all_paths: list[str] = []
    groups: dict[Any, list[str]] = {}
    for row in rows:
        path = row["path"]
        all_paths.append(path)
        post_number = row.get("post_number") if hasattr(row, "get") else row["post_number"]
        if post_number is None:
            continue
        groups.setdefault(post_number, []).append(path)

    result = {path: path for path in all_paths}
    for paths in groups.values():
        canonical = min(paths)
        for path in paths:
            result[path] = canonical
    return result


_DOCUMENT_META_COLUMNS: tuple[str, ...] = (
    "post_number",
    "title",
    "source",
    "document_type",
    "status",
    "url",
    "category",
)


def _document_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    return {col: row.get(col) for col in _DOCUMENT_META_COLUMNS}


class IndexBuilder:
    """`create_index_schema` 済みの接続に対する差分索引構築。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        tokenizers: Sequence[str] = DEFAULT_TOKENIZERS,
        chunk_options: ChunkOptions | None = None,
    ) -> None:
        self._conn = conn
        self._tokenizers = tuple(tokenizers)
        self._chunk_options = chunk_options or DEFAULT_CHUNK_OPTIONS

    def build(
        self,
        rows: Sequence[Mapping[str, Any]],
        docs_dir: Path,
        *,
        emit: EmitFn | None = None,
        check_lease: CheckLeaseFn | None = None,
    ) -> IndexSummary:
        conn = self._conn
        docs_dir = Path(docs_dir)
        summary = IndexSummary()

        current_paths = {row["path"] for row in rows}
        existing_paths = {r[0] for r in conn.execute("SELECT path FROM documents").fetchall()}

        removed_paths = sorted(existing_paths - current_paths)
        total = len(removed_paths) + len(rows)
        step = 0

        for path in removed_paths:
            if check_lease is not None:
                check_lease()
            removed_chunks, removed_embeddings = self._delete_document(path)
            summary.documents_removed += 1
            summary.chunks_removed += removed_chunks
            summary.embeddings_removed += removed_embeddings
            step += 1
            if emit is not None:
                emit(phase="index-remove", current=step, total=total, message=path, item=path)

        for row in rows:
            if check_lease is not None:
                check_lease()
            path = row["path"]
            full_path = docs_dir / path
            try:
                content = full_path.read_text(encoding="utf-8")
            except OSError as exc:
                summary.errors.append({"path": path, "error": str(exc)})
                step += 1
                if emit is not None:
                    emit(
                        phase="index-build",
                        current=step,
                        total=total,
                        message=f"読み込み失敗: {exc}",
                        item=path,
                    )
                continue

            doc_hash = hash_body(content)
            existing = self._get_document(path)

            if existing is None:
                chunk_count = self._insert_document(row, content, doc_hash)
                summary.documents_added += 1
                summary.chunks_added += chunk_count
            elif existing["document_hash"] == doc_hash:
                self._update_metadata_only(row)
                summary.documents_unchanged += 1
            else:
                removed_chunks, removed_embeddings, added_chunks = self._replace_document(
                    row, content, doc_hash
                )
                summary.documents_updated += 1
                summary.chunks_removed += removed_chunks
                summary.embeddings_removed += removed_embeddings
                summary.chunks_added += added_chunks

            step += 1
            if emit is not None:
                emit(phase="index-build", current=step, total=total, message=path, item=path)

        summary.canonical_paths_updated = self._apply_canonical_paths()

        with transaction(conn):
            rebuild_fts_indexes(conn, self._tokenizers)

        self._write_meta()

        return summary

    # -- 内部ヘルパー -----------------------------------------------------

    def _get_document(self, path: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM documents WHERE path = ?", (path,)).fetchone()

    def _insert_chunks(self, path: str, title: str | None, chunks: Sequence[Chunk]) -> None:
        for chunk in chunks:
            self._conn.execute(
                "INSERT INTO chunks "
                "(path, chunk_index, title, heading_path, text, start_line, end_line, "
                "content_hash, token_estimate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    path,
                    chunk.index,
                    title,
                    chunk.heading_path,
                    chunk.text,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.content_hash,
                    chunk.token_estimate,
                ),
            )

    def _insert_document(self, row: Mapping[str, Any], content: str, doc_hash: str) -> int:
        path = row["path"]
        chunks = chunk_markdown(content, self._chunk_options)
        now = datetime.now(UTC).isoformat()
        fields = _document_fields(row)
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO documents "
                "(path, post_number, title, source, document_type, status, url, category, "
                "document_hash, chunk_count, indexed_at, canonical_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    path,
                    fields["post_number"],
                    fields["title"],
                    fields["source"],
                    fields["document_type"],
                    fields["status"],
                    fields["url"],
                    fields["category"],
                    doc_hash,
                    len(chunks),
                    now,
                    path,
                ),
            )
            self._insert_chunks(path, fields["title"], chunks)
        return len(chunks)

    def _update_metadata_only(self, row: Mapping[str, Any]) -> None:
        """`document_hash` が変わっていない高速パス: 再チャンクせず列だけ更新する。"""
        path = row["path"]
        fields = _document_fields(row)
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE documents SET post_number = ?, title = ?, source = ?, "
                "document_type = ?, status = ?, url = ?, category = ? WHERE path = ?",
                (
                    fields["post_number"],
                    fields["title"],
                    fields["source"],
                    fields["document_type"],
                    fields["status"],
                    fields["url"],
                    fields["category"],
                    path,
                ),
            )
            # `chunks.title` は文書タイトルの複製(FTS の bm25 重み付けが
            # `chunks_fts_*` からJOIN無しで直接読むための非正規化)。
            # 再チャンクしなくてもタイトルの追従だけは行う。
            self._conn.execute(
                "UPDATE chunks SET title = ? WHERE path = ?", (fields["title"], path)
            )

    def _delete_chunks_and_embeddings(self, path: str) -> tuple[int, int]:
        """`path` の全チャンクとそれらを指す `embeddings` を削除する。

        呼び出し元の `transaction()` の中で実行すること(brief Step3の同一トランザクション契約)。
        """
        old_chunk_ids = [
            r[0]
            for r in self._conn.execute("SELECT id FROM chunks WHERE path = ?", (path,)).fetchall()
        ]
        removed_embeddings = 0
        if old_chunk_ids:
            placeholders = ",".join("?" for _ in old_chunk_ids)
            cur = self._conn.execute(
                f"DELETE FROM embeddings WHERE chunk_id IN ({placeholders})", old_chunk_ids
            )
            valid_rowcount = cur.rowcount is not None and cur.rowcount >= 0
            removed_embeddings = cur.rowcount if valid_rowcount else len(old_chunk_ids)
        self._conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
        return len(old_chunk_ids), removed_embeddings

    def _replace_document(
        self, row: Mapping[str, Any], content: str, doc_hash: str
    ) -> tuple[int, int, int]:
        """`document_hash` が変わった文書のチャンクを入れ替える。

        chunk 削除 → embeddings 削除 → 再チャンク挿入までを**1トランザクション**で
        行う(brief Step3、旧版の孤児バグの修正対象そのもの)。
        """
        path = row["path"]
        chunks = chunk_markdown(content, self._chunk_options)
        now = datetime.now(UTC).isoformat()
        fields = _document_fields(row)

        with transaction(self._conn):
            removed_chunks, removed_embeddings = self._delete_chunks_and_embeddings(path)
            self._conn.execute(
                "UPDATE documents SET post_number = ?, title = ?, source = ?, "
                "document_type = ?, status = ?, url = ?, category = ?, document_hash = ?, "
                "chunk_count = ?, indexed_at = ? WHERE path = ?",
                (
                    fields["post_number"],
                    fields["title"],
                    fields["source"],
                    fields["document_type"],
                    fields["status"],
                    fields["url"],
                    fields["category"],
                    doc_hash,
                    len(chunks),
                    now,
                    path,
                ),
            )
            self._insert_chunks(path, fields["title"], chunks)

        return removed_chunks, removed_embeddings, len(chunks)

    def _delete_document(self, path: str) -> tuple[int, int]:
        with transaction(self._conn):
            removed_chunks, removed_embeddings = self._delete_chunks_and_embeddings(path)
            self._conn.execute("DELETE FROM documents WHERE path = ?", (path,))
        return removed_chunks, removed_embeddings

    def _apply_canonical_paths(self) -> int:
        cursor = self._conn.execute("SELECT path, post_number FROM documents")
        rows = [dict(r) for r in cursor.fetchall()]
        canonical = resolve_canonical_paths(rows)
        updated = 0
        with transaction(self._conn):
            for path, canonical_path in canonical.items():
                cur = self._conn.execute(
                    "UPDATE documents SET canonical_path = ? "
                    "WHERE path = ? AND (canonical_path IS NOT ?)",
                    (canonical_path, path, canonical_path),
                )
                if cur.rowcount and cur.rowcount > 0:
                    updated += cur.rowcount
        return updated

    def _write_meta(self) -> None:
        now = datetime.now(UTC).isoformat()
        chunk_options_json = json.dumps(
            {
                "max_tokens": self._chunk_options.max_tokens,
                "hard_max_tokens": self._chunk_options.hard_max_tokens,
                "min_tokens": self._chunk_options.min_tokens,
            }
        )
        values = {
            "schema_version": str(INDEX_SCHEMA_VERSION),
            "tokenizers": json.dumps(list(self._tokenizers)),
            "last_indexed_at": now,
            "chunk_options": chunk_options_json,
        }
        with transaction(self._conn):
            for key, value in values.items():
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )


__all__ = ["IndexBuilder", "IndexSummary", "resolve_canonical_paths"]
