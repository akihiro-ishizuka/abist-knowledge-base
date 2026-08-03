"""索引DBスキーマ(brief Step1: 赤)。

旧版 `kb-index.sqlite`/`reference-index.sqlite` の列を正確に再現していること、
external-content FTS5 の2テーブル(`chunks_fts_unicode61`/`chunks_fts_trigram`)が
`'rebuild'` で投入でき、`bm25()` が使え、trigram が3文字以上でマッチすることを検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.search.index_schema import (
    DEFAULT_TOKENIZERS,
    create_index_schema,
    fts_table_name,
    rebuild_fts_indexes,
)


def _open(tmp_root: Path):
    return connect(tmp_root / "index.sqlite")


def _column_names(conn, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_documents_table_has_old_columns_in_expected_types(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        assert _column_names(conn, "documents") == [
            "path",
            "post_number",
            "title",
            "source",
            "document_type",
            "status",
            "url",
            "category",
            "document_hash",
            "chunk_count",
            "indexed_at",
            "canonical_path",
        ]
        pk = [
            row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall() if row[5] == 1
        ]
        assert pk == ["path"]
        not_null = {
            row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall() if row[3]
        }
        assert "document_hash" in not_null
    finally:
        conn.close()


def test_chunks_table_has_old_columns_and_unique_constraint(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        assert _column_names(conn, "chunks") == [
            "id",
            "path",
            "chunk_index",
            "title",
            "heading_path",
            "text",
            "start_line",
            "end_line",
            "content_hash",
            "token_estimate",
        ]
        indexes = conn.execute("PRAGMA index_list(chunks)").fetchall()
        assert any(row[2] == 1 for row in indexes), "UNIQUE制約付きの索引が見つかりません"

        conn.execute("INSERT INTO documents (path, document_hash) VALUES ('a.md', 'h')")
        conn.execute("INSERT INTO chunks (path, chunk_index, text) VALUES ('a.md', 0, 'x')")
        with pytest.raises(Exception):  # noqa: B017 - sqlite3.IntegrityError
            conn.execute("INSERT INTO chunks (path, chunk_index, text) VALUES ('a.md', 0, 'y')")
    finally:
        conn.close()


def test_embeddings_table_has_old_columns(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        assert _column_names(conn, "embeddings") == [
            "chunk_id",
            "vector",
            "model",
            "dimensions",
            "input_hash",
            "created_at",
        ]
        pk = [
            row[1]
            for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()
            if row[5] == 1
        ]
        assert pk == ["chunk_id"]
    finally:
        conn.close()


def test_meta_table_is_key_value(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        assert _column_names(conn, "meta") == ["key", "value"]
        conn.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
        assert conn.execute("SELECT value FROM meta WHERE key = 'k'").fetchone()[0] == "v"
    finally:
        conn.close()


def test_create_index_schema_is_idempotent(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        create_index_schema(conn, DEFAULT_TOKENIZERS)  # 2回目も例外なく成功する
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"documents", "chunks", "embeddings", "meta"} <= names
    finally:
        conn.close()


def test_both_fts5_tables_are_built_as_external_content(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        for tokenizer in DEFAULT_TOKENIZERS:
            table = fts_table_name(tokenizer)
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[
                0
            ]
            assert "content='chunks'" in sql
            assert "content_rowid='id'" in sql
    finally:
        conn.close()


def test_fts5_populated_via_rebuild_command_not_triggers(tmp_root: Path):
    """external-content は自動反映しない(トリガ無し)ので `'rebuild'` 前後で差が出る。

    unicode61 は空白の無い連続した CJK ラン全体を1トークンとして扱うため
    (ICU 無しの unicode61 の既知の制約で、句の部分一致検索ができない)、
    両トークナイザで確実に検証できる ASCII 語を使う。
    """
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        conn.execute("INSERT INTO documents (path, document_hash) VALUES ('a.md', 'h')")
        conn.execute(
            "INSERT INTO chunks (path, chunk_index, title, heading_path, text) "
            "VALUES ('a.md', 0, 'requirement title', 'overview', 'sqlite full text search')"
        )
        # トリガが無いので、rebuild する前は MATCH で見つからない
        # (external-content の `count(*)`(MATCH無し)は content 表を素通しするため
        # 索引化の有無を判定できない。索引化された行しかヒットしない MATCH で確認する)。
        for tokenizer in DEFAULT_TOKENIZERS:
            table = fts_table_name(tokenizer)
            before = conn.execute(
                f"SELECT count(*) FROM {table} WHERE {table} MATCH 'sqlite'"
            ).fetchone()[0]
            assert before == 0

        rebuild_fts_indexes(conn, DEFAULT_TOKENIZERS)

        for tokenizer in DEFAULT_TOKENIZERS:
            table = fts_table_name(tokenizer)
            after = conn.execute(
                f"SELECT count(*) FROM {table} WHERE {table} MATCH 'sqlite'"
            ).fetchone()[0]
            assert after == 1
    finally:
        conn.close()


def test_bm25_ranking_function_is_usable_on_both_tables(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        conn.execute("INSERT INTO documents (path, document_hash) VALUES ('a.md', 'h')")
        conn.execute(
            "INSERT INTO chunks (path, chunk_index, title, heading_path, text) "
            "VALUES ('a.md', 0, 'title', 'heading', 'sqlite full text search')"
        )
        rebuild_fts_indexes(conn, DEFAULT_TOKENIZERS)
        for tokenizer in DEFAULT_TOKENIZERS:
            table = fts_table_name(tokenizer)
            row = conn.execute(
                f"SELECT bm25({table}, 1.0, 2.0, 3.0) FROM {table} WHERE {table} MATCH 'sqlite'"
            ).fetchone()
            assert row is not None
            assert row[0] is not None
    finally:
        conn.close()


def test_trigram_table_matches_three_character_terms(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        conn.execute("INSERT INTO documents (path, document_hash) VALUES ('a.md', 'h')")
        conn.execute(
            "INSERT INTO chunks (path, chunk_index, title, heading_path, text) "
            "VALUES ('a.md', 0, 't', 'h', '蛇腹の要件はどこまで充足しているか')"
        )
        rebuild_fts_indexes(conn, DEFAULT_TOKENIZERS)
        table = fts_table_name("trigram")
        # 3文字以上の語はマッチする。
        hit = conn.execute(
            f"SELECT count(*) FROM {table} WHERE {table} MATCH '要件はど'"
        ).fetchone()[0]
        assert hit == 1
    finally:
        conn.close()


def test_unicode61_table_is_also_built_for_comparison(tmp_root: Path):
    """既定は trigram だが、比較用に unicode61 も構築されていること。"""
    conn = _open(tmp_root)
    try:
        create_index_schema(conn, DEFAULT_TOKENIZERS)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "chunks_fts_unicode61" in names
        assert "chunks_fts_trigram" in names
    finally:
        conn.close()


def test_rejects_unsupported_tokenizer(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        with pytest.raises(ValueError):
            create_index_schema(conn, ["porter"])
    finally:
        conn.close()


def test_rejects_empty_tokenizer_list(tmp_root: Path):
    conn = _open(tmp_root)
    try:
        with pytest.raises(ValueError):
            create_index_schema(conn, [])
    finally:
        conn.close()
