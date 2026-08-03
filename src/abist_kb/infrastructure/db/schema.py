"""`data/app.sqlite` 全体(jobs + sources/batches/documents)の唯一のブートストラップ経路。

`infrastructure.jobs.db.ensure_jobs_schema` は自分が把握する範囲のマイグレーション
(バージョン2)だけを `apply_migrations` へ渡す。もし `sources`/`batches`/`documents`
(バージョン3)側もこの同じ設計(自分の版だけを渡す)を単独で採用すると、
同じ `app.sqlite` に対して異なる版一覧でブートストラップする2つの経路ができてしまい、
`apply_migrations` の「DB の版が渡された一覧の最大版より新しければ
`MIGRATION_FAILED` で拒否する」ガード(古いバイナリが新しい DB を誤って開くことを
防ぐためのもの)に、後から呼ばれた側が意図せず引っかかる
(例: 先に本モジュール経由でバージョン3まで適用された DB に対し、後から
`ensure_jobs_schema`(バージョン2のみ把握)を呼ぶと「DB(3)が既知の最大(2)より
新しい」と誤検知して失敗する)。

そのため `ensure_app_schema`/`open_app_db` は jobs 側の既知マイグレーションも
合わせて1つの一覧として `apply_migrations` へ渡す。`presentation/cli` 配下で
`app_db_path` を開く全コマンド(`jobs`/`worker`/`source`/`batch`/`document`)は
`infrastructure.jobs.db.open_jobs_db` ではなくこの `open_app_db` を使うこと。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.migrations import Migration, apply_migrations, load_migrations
from abist_kb.infrastructure.jobs.db import load_job_migrations

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def load_core_migrations() -> list[Migration]:
    """`infrastructure/db/migrations/*.sql`(sources/batches/documents)を読み込む。"""
    return load_migrations(_MIGRATIONS_DIR)


def load_app_migrations() -> list[Migration]:
    """app.sqlite に適用しうる全マイグレーション(jobs + core)を版昇順で返す。"""
    return [*load_job_migrations(), *load_core_migrations()]


def ensure_app_schema(conn: sqlite3.Connection) -> None:
    """app.sqlite の全テーブル(jobs 含む)が無ければ作る(既に適用済みなら何もしない)。"""
    apply_migrations(conn, load_app_migrations())


def open_app_db(db_path: Path) -> sqlite3.Connection:
    """app.sqlite を開き、スキーマを保証してから返す。呼び出し元が `close()` する。"""
    conn = connect(db_path)
    ensure_app_schema(conn)
    return conn


__all__ = [
    "ensure_app_schema",
    "load_app_migrations",
    "load_core_migrations",
    "open_app_db",
]
