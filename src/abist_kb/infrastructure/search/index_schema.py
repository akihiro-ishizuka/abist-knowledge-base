"""検索索引DB(`kb-index.sqlite` / `reference-index.sqlite`)のスキーマ(設計書 §9.2)。

**マイグレーション番号の採番規則との関係(M4 task-1 の決定事項)**:
`infrastructure/db/schema.py` の `load_app_migrations()` は `data/app.sqlite`
**1個**に適用しうる全マイグレーションの唯一の採番台帳であり、そのモジュール
docstring は「M4(埋め込み)以降でテーブルを追加する場合は0004から使うこと」と
書いている。しかし `kb-index.sqlite`/`reference-index.sqlite` は
**`app.sqlite` とは別のファイル**であり(`design/system-design.md` §9.2、
`design/plans/M4-index-search.md` Global Constraints: 「索引DBは work /
reference の2ファイルに分ける」)、`schema_migrations` テーブルも共有しない。
したがって、この2つの索引DBのスキーマは `load_app_migrations()` の採番台帳に
**含めない**。

代わりに `create_index_schema()` は `CREATE TABLE IF NOT EXISTS` /
`CREATE VIRTUAL TABLE IF NOT EXISTS` を直接発行する冪等な関数として実装する
(`infrastructure/db/migrations.py` の番号付きマイグレーション機構は使わない)。
理由は2つ:

1. `design/system-design.md` §11.1 の運用表が示すとおり、索引DBは
   「検索正確性を優先してMarkdownから再構築(原則再生成)」が既定の運用であり、
   `app.sqlite` の `sources`/`batches`/`documents` のような「積み上げていく
   正データ」ではない。履歴(`schema_migrations` に版を積み上げていく意味論)を
   持たせる価値が薄い。
2. `chunks_fts_unicode61`/`chunks_fts_trigram` の2テーブルは
   `tokenizers` 引数(`meta.tokenizers` に記録する値と同じ)に応じて動的に
   生成されるテーブル名(`chunks_fts_<tokenizer>`)を持つ。静的な `NNNN_name.sql`
   ファイル1本には収まらない可変長の集合であり、既存の版付きマイグレーション
   ファイル形式とは相性が悪い。

`documents`/`chunks`/`embeddings`/`meta` の4列構成は旧版 `kb-index.sqlite` /
`reference-index.sqlite` の列名・型をそのまま再現する(M8 が旧DBと突き合わせる
ため、列名・型の改変は禁止)。FTS5 の2テーブルはどちらも `chunks` への
external-content(`content='chunks', content_rowid='id'`)として作り、投入は
トリガではなく `'rebuild'` コマンドで行う(`indexer.py` 参照)。列順は
`text, heading_path, title` の順で固定する。旧版の `bm25(<table>, 1.0, 2.0, 3.0)`
呼び出し(text 1.0 / heading_path 2.0 / title 3.0)は列の宣言順に重みを対応させる
ため、この順序を変えると重み付けが入れ替わってしまう。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

#: FTS5 の tokenize= 引数値。`meta.tokenizers` に記録するキー(`chunks_fts_<key>`
#: のテーブル名サフィックスにも使う)と、実際にトークナイザへ渡す文字列が
#: 異なる場合はここに追加する(現状は同じ文字列)。
SUPPORTED_TOKENIZERS: tuple[str, ...] = ("unicode61", "trigram")

DEFAULT_TOKENIZERS: tuple[str, ...] = ("unicode61", "trigram")

#: 索引DBスキーマの版(`meta.schema_version` に書く値)。`app.sqlite` の
#: `schema_migrations` とは無関係の、この2ファイル固有の版番号。
INDEX_SCHEMA_VERSION = 1

_CORE_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    path TEXT PRIMARY KEY,
    post_number INTEGER,
    title TEXT,
    source TEXT,
    document_type TEXT,
    status TEXT,
    url TEXT,
    category TEXT,
    document_hash TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT,
    canonical_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_post_number ON documents (post_number);
CREATE INDEX IF NOT EXISTS idx_documents_canonical_path ON documents (canonical_path);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL REFERENCES documents (path),
    chunk_index INTEGER NOT NULL,
    title TEXT,
    heading_path TEXT,
    text TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    content_hash TEXT,
    token_estimate INTEGER,
    UNIQUE (path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks (path);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks (id),
    vector BLOB,
    model TEXT,
    dimensions INTEGER,
    input_hash TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _fts5_ddl(table_name: str, tokenizer: str) -> str:
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} USING fts5("
        "text, heading_path, title, "
        "content='chunks', content_rowid='id', "
        f"tokenize='{tokenizer}'"
        ")"
    )


def fts_table_name(tokenizer: str) -> str:
    """トークナイザ名から external-content FTS5 テーブル名を組み立てる。"""
    return f"chunks_fts_{tokenizer}"


def create_index_schema(
    conn: sqlite3.Connection, tokenizers: Sequence[str] = DEFAULT_TOKENIZERS
) -> None:
    """`documents`/`chunks`/`embeddings`/`meta` と `tokenizers` の数だけの
    external-content FTS5 テーブル(既定は `chunks_fts_unicode61` と
    `chunks_fts_trigram` の2つ)を作る。既に存在するテーブルには触れない
    (`CREATE TABLE IF NOT EXISTS`)ため、何度呼んでも安全(冪等)。
    """
    if not tokenizers:
        raise ValueError("tokenizers は少なくとも1つ指定してください。")
    for tokenizer in tokenizers:
        if tokenizer not in SUPPORTED_TOKENIZERS:
            raise ValueError(
                f"未対応のトークナイザです: {tokenizer}"
                f"(対応済み: {', '.join(SUPPORTED_TOKENIZERS)})"
            )

    conn.executescript(_CORE_DDL)
    for tokenizer in tokenizers:
        conn.execute(_fts5_ddl(fts_table_name(tokenizer), tokenizer))


def rebuild_fts_indexes(conn: sqlite3.Connection, tokenizers: Sequence[str]) -> None:
    """external-content FTS5 テーブルを `'rebuild'` コマンドで `chunks` から作り直す。

    external-content 構成はトリガを持たないため、`chunks` への
    挿入・更新・削除は自動反映されない。索引構築(`IndexBuilder.build`)が
    `chunks` への全ての書込を終えた**後**に一度だけ呼ぶ想定。
    """
    for tokenizer in tokenizers:
        table = fts_table_name(tokenizer)
        conn.execute(f"INSERT INTO {table}({table}) VALUES ('rebuild')")


__all__ = [
    "DEFAULT_TOKENIZERS",
    "INDEX_SCHEMA_VERSION",
    "SUPPORTED_TOKENIZERS",
    "create_index_schema",
    "fts_table_name",
    "rebuild_fts_indexes",
]
