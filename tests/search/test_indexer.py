"""`IndexBuilder`/`resolve_canonical_paths`(brief Step2〜4)。"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.search.index_schema import (
    DEFAULT_TOKENIZERS,
    create_index_schema,
    fts_table_name,
)
from abist_kb.infrastructure.search.indexer import IndexBuilder, resolve_canonical_paths


def _open(tmp_root: Path):
    conn = connect(tmp_root / "index.sqlite")
    create_index_schema(conn, DEFAULT_TOKENIZERS)
    return conn


def _write(docs_dir: Path, path: str, content: str) -> None:
    full = docs_dir / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _row(path: str, **overrides) -> dict:
    base = {
        "path": path,
        "post_number": None,
        "title": "タイトル",
        "source": "esa",
        "document_type": "article",
        "status": "active",
        "url": None,
        "category": None,
    }
    base.update(overrides)
    return base


def _all_chunks(conn):
    return conn.execute("SELECT id, path, chunk_index, title FROM chunks ORDER BY id").fetchall()


# -- 基本の構築 ------------------------------------------------------------


def test_build_inserts_new_document_and_chunks(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    conn = _open(tmp_root)
    try:
        summary = IndexBuilder(conn).build([_row("a.md")], docs_dir)
        assert summary.documents_added == 1
        assert summary.chunks_added >= 1
        assert summary.documents_updated == 0
        assert summary.documents_unchanged == 0

        doc = conn.execute("SELECT * FROM documents WHERE path = 'a.md'").fetchone()
        assert doc["document_hash"]
        assert doc["chunk_count"] == summary.chunks_added
        assert doc["indexed_at"]
        assert doc["canonical_path"] == "a.md"

        chunks = _all_chunks(conn)
        assert len(chunks) == summary.chunks_added
        assert all(c["path"] == "a.md" for c in chunks)
    finally:
        conn.close()


def test_build_populates_fts_after_insert(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 蛇腹\n\n蛇腹の要件についての本文です。\n")
    conn = _open(tmp_root)
    try:
        IndexBuilder(conn).build([_row("a.md")], docs_dir)
        table = fts_table_name("trigram")
        hit = conn.execute(
            f"SELECT count(*) FROM {table} WHERE {table} MATCH '蛇腹の要件'"
        ).fetchone()[0]
        assert hit >= 1
    finally:
        conn.close()


def test_unchanged_document_takes_fast_path_without_rechunking(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    conn = _open(tmp_root)
    try:
        builder = IndexBuilder(conn)
        builder.build([_row("a.md")], docs_dir)
        chunk_ids_before = [c["id"] for c in _all_chunks(conn)]

        summary = builder.build([_row("a.md", title="新タイトル")], docs_dir)
        assert summary.documents_unchanged == 1
        assert summary.documents_updated == 0
        assert summary.chunks_added == 0
        assert summary.chunks_removed == 0

        chunk_ids_after = [c["id"] for c in _all_chunks(conn)]
        assert chunk_ids_before == chunk_ids_after  # 再チャンクしていない

        doc = conn.execute("SELECT title FROM documents WHERE path = 'a.md'").fetchone()
        assert doc["title"] == "新タイトル"
        # chunks.title はチャンクの再挿入なしで追従する(非正規化の複製列)。
        chunk_titles = {c["title"] for c in _all_chunks(conn)}
        assert chunk_titles == {"新タイトル"}
    finally:
        conn.close()


def test_changed_document_replaces_chunks(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    # 変更されない別文書を1つ同居させる: SQLite は AUTOINCREMENT 無しの
    # INTEGER PRIMARY KEY で「テーブルが空になった直後は rowid 1 から採番し直す」
    # ため、対象文書のチャンクだけの単独テーブルだと削除後の再挿入がたまたま
    # 同じ rowid を引いてしまい、「新しい rowid で再挿入される」ことの検証に
    # ならない。もう1文書分のチャンクを残しておくことでその偶然を避ける。
    _write(docs_dir, "keep.md", "# 見出し\n\n変わらない本文です。\n")
    conn = _open(tmp_root)
    try:
        builder = IndexBuilder(conn)
        builder.build([_row("a.md"), _row("keep.md")], docs_dir)
        old_ids = {c["id"] for c in _all_chunks(conn) if c["path"] == "a.md"}

        _write(docs_dir, "a.md", "# 見出し2\n\n書き換えた本文です。もっと長くしています。\n")
        summary = builder.build([_row("a.md"), _row("keep.md")], docs_dir)

        assert summary.documents_updated == 1
        assert summary.documents_added == 0
        assert summary.chunks_removed == len(old_ids)

        new_ids = {c["id"] for c in _all_chunks(conn) if c["path"] == "a.md"}
        assert new_ids.isdisjoint(old_ids)  # 新しい rowid で再挿入されている
    finally:
        conn.close()


def test_document_removed_from_rows_is_deleted_with_chunks(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    _write(docs_dir, "b.md", "# 見出し\n\n別の本文です。\n")
    conn = _open(tmp_root)
    try:
        builder = IndexBuilder(conn)
        builder.build([_row("a.md"), _row("b.md")], docs_dir)

        summary = builder.build([_row("a.md")], docs_dir)
        assert summary.documents_removed == 1

        assert conn.execute("SELECT count(*) FROM documents WHERE path = 'b.md'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chunks WHERE path = 'b.md'").fetchone()[0] == 0
    finally:
        conn.close()


def test_unreadable_document_is_recorded_as_error_and_does_not_abort_build(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    conn = _open(tmp_root)
    try:
        summary = IndexBuilder(conn).build([_row("a.md"), _row("missing.md")], docs_dir)
        assert summary.documents_added == 1
        assert len(summary.errors) == 1
        assert summary.errors[0]["path"] == "missing.md"
    finally:
        conn.close()


# -- 孤児埋め込みの修正(brief Step3、旧版の20件バグの回帰テスト) -------------


def test_reindexing_changed_document_deletes_orphaned_embeddings_in_same_transaction(
    tmp_root: Path,
):
    """旧版は再索引でチャンクを削除しても embeddings を削除せず、20件の孤児を残した。

    移植版は `chunk_id` を指定して同一トランザクションで削除するため、
    どれだけ再索引を繰り返しても `embeddings` に孤児(`chunks` に存在しない
    `chunk_id`)が残らないことを固定する。
    """
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    conn = _open(tmp_root)
    try:
        builder = IndexBuilder(conn)
        builder.build([_row("a.md")], docs_dir)
        old_chunk_ids = [c["id"] for c in _all_chunks(conn)]

        # 埋め込みを旧チャンクへ人工的に挿入する(Task2 の埋め込みプロバイダの
        # 代わりに、削除対象になるべき行がちゃんと消えることだけを検証する)。
        for chunk_id in old_chunk_ids:
            conn.execute(
                "INSERT INTO embeddings (chunk_id, vector, model, dimensions, input_hash, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (chunk_id, b"\x00" * 4, "test-model", 1, "hash", "now"),
            )
        assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == len(old_chunk_ids)

        # 内容を変更して再索引する -> 旧チャンクは削除され、新しい rowid で再挿入される。
        _write(docs_dir, "a.md", "# 見出し2\n\n変更後の本文です。もっと長くしています。\n")
        summary = builder.build([_row("a.md")], docs_dir)
        assert summary.embeddings_removed == len(old_chunk_ids)

        # 孤児(chunks に存在しない chunk_id を指す embeddings 行)が無いこと。
        orphans = conn.execute(
            "SELECT count(*) FROM embeddings WHERE chunk_id NOT IN (SELECT id FROM chunks)"
        ).fetchone()[0]
        assert orphans == 0
        assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 0
    finally:
        conn.close()


def test_removing_document_from_rows_deletes_its_embeddings_too(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    conn = _open(tmp_root)
    try:
        builder = IndexBuilder(conn)
        builder.build([_row("a.md")], docs_dir)
        chunk_ids = [c["id"] for c in _all_chunks(conn)]
        for chunk_id in chunk_ids:
            conn.execute(
                "INSERT INTO embeddings (chunk_id, vector) VALUES (?, ?)", (chunk_id, b"x")
            )

        summary = builder.build([], docs_dir)
        assert summary.documents_removed == 1
        assert summary.embeddings_removed == len(chunk_ids)
        assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 0
    finally:
        conn.close()


# -- canonical_path(brief Step4) -------------------------------------------


def test_resolve_canonical_paths_groups_by_post_number_and_picks_lexicographic_min():
    rows = [
        {"path": "b/article.md", "post_number": 42},
        {"path": "a/article.md", "post_number": 42},
        {"path": "z/other.md", "post_number": None},
    ]
    result = resolve_canonical_paths(rows)
    assert result["a/article.md"] == "a/article.md"
    assert result["b/article.md"] == "a/article.md"
    assert result["z/other.md"] == "z/other.md"  # post_number無し -> 自分自身


def test_build_applies_canonical_path_to_new_documents(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "b/article.md", "# 見出し\n\n本文A。\n")
    _write(docs_dir, "a/article.md", "# 見出し\n\n本文B。\n")
    conn = _open(tmp_root)
    try:
        IndexBuilder(conn).build(
            [
                _row("b/article.md", post_number=42),
                _row("a/article.md", post_number=42),
            ],
            docs_dir,
        )
        rows = {
            r["path"]: r["canonical_path"]
            for r in conn.execute("SELECT path, canonical_path FROM documents").fetchall()
        }
        assert rows["a/article.md"] == "a/article.md"
        assert rows["b/article.md"] == "a/article.md"
    finally:
        conn.close()


def test_canonical_path_is_recomputed_even_on_unchanged_fast_path(tmp_root: Path):
    """未変更の高速パスでも canonical_path は更新される(brief Step4)。"""
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "b/article.md", "# 見出し\n\n本文A。\n")
    conn = _open(tmp_root)
    try:
        builder = IndexBuilder(conn)
        # 1回目: 単独文書なので canonical_path は自分自身。
        builder.build([_row("b/article.md", post_number=42)], docs_dir)
        assert (
            conn.execute(
                "SELECT canonical_path FROM documents WHERE path = 'b/article.md'"
            ).fetchone()[0]
            == "b/article.md"
        )

        # 2回目: 内容は変えず(document_hash 同一、高速パス)、同じ post_number を持つ
        # より辞書順で小さいパスが仲間入りする -> canonical_path が変わるはず。
        _write(docs_dir, "a/article.md", "# 見出し\n\n本文B。\n")
        builder.build(
            [
                _row("b/article.md", post_number=42),
                _row("a/article.md", post_number=42),
            ],
            docs_dir,
        )
        assert (
            conn.execute(
                "SELECT canonical_path FROM documents WHERE path = 'b/article.md'"
            ).fetchone()[0]
            == "a/article.md"
        )
    finally:
        conn.close()


# -- meta(brief Step6) ------------------------------------------------------


def test_build_writes_meta_rows(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    conn = _open(tmp_root)
    try:
        IndexBuilder(conn, tokenizers=DEFAULT_TOKENIZERS).build([_row("a.md")], docs_dir)
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta").fetchall()}
        assert meta["schema_version"] == "1"
        assert json.loads(meta["tokenizers"]) == list(DEFAULT_TOKENIZERS)
        assert meta["last_indexed_at"]
        chunk_options = json.loads(meta["chunk_options"])
        assert "max_tokens" in chunk_options
    finally:
        conn.close()


# -- check_lease(協調的なリース確認、M3 の契約) -----------------------------


def test_check_lease_is_called_before_each_side_effecting_write(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    _write(docs_dir, "b.md", "# 見出し\n\n本文です。\n")
    conn = _open(tmp_root)
    try:
        calls = 0

        def check_lease():
            nonlocal calls
            calls += 1

        IndexBuilder(conn).build([_row("a.md"), _row("b.md")], docs_dir, check_lease=check_lease)
        assert calls == 2  # 2文書分、各書込の前に呼ばれる
    finally:
        conn.close()


def test_check_lease_failure_stops_further_writes(tmp_root: Path):
    """リースを失った合図(例外)を受け取ったら、それ以降の書込を行わないこと。

    (M3 で実証された「リース喪失後も20回中16回書き込みを続けた」不具合の
    直接の再発防止テスト。)
    """
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "# 見出し\n\n本文です。\n")
    _write(docs_dir, "b.md", "# 見出し\n\n本文です。\n")
    _write(docs_dir, "c.md", "# 見出し\n\n本文です。\n")
    conn = _open(tmp_root)
    try:
        calls = 0

        class LeaseLost(Exception):
            pass

        def check_lease():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise LeaseLost()

        with contextlib.suppress(LeaseLost):
            IndexBuilder(conn).build(
                [_row("a.md"), _row("b.md"), _row("c.md")],
                docs_dir,
                check_lease=check_lease,
            )

        added = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        assert added == 1  # 1件目だけ書き込まれ、2件目以降は書き込まれていない
    finally:
        conn.close()
