"""ジョブ用 SQLite 接続のブートストラップ(設計書 §9.2, §10)。

`app_db_path` を開き、`0002_jobs.sql` のマイグレーションを適用する唯一の経路。
複数プロセス(Web/TUI/CLI/MCP)が同時に未初期化DBへ最初に触れても、
`apply_migrations`/`transaction` が `BEGIN IMMEDIATE` で調停するため安全である
(`infrastructure.db.migrations` の設計と同じ前提)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.migrations import Migration, apply_migrations, load_migrations

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def load_job_migrations() -> list[Migration]:
    """`infrastructure/jobs/migrations/*.sql` を読み込む。"""
    return load_migrations(_MIGRATIONS_DIR)


def ensure_jobs_schema(conn: sqlite3.Connection) -> None:
    """ジョブ関連テーブルが無ければ作る(既に適用済みなら何もしない)。"""
    apply_migrations(conn, load_job_migrations())


def open_jobs_db(db_path: Path) -> sqlite3.Connection:
    """ジョブ用DBを開き、スキーマを保証してから返す。呼び出し元が `close()` する。"""
    conn = connect(db_path)
    ensure_jobs_schema(conn)
    return conn


__all__ = ["ensure_jobs_schema", "load_job_migrations", "open_jobs_db"]
