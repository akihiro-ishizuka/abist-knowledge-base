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

**マイグレーション番号の採番規則(コードレビュー Important 5 への対応、次の
マイルストーンの実装者へ)**:

`app.sqlite` は1個の `schema_migrations` テーブルをパッケージ横断で共有するため、
バージョン番号は「ディレクトリごと」ではなく「`app.sqlite` 全体」で一意かつ
連続でなければならない。`apply_migrations` 自身は渡された一覧内の重複を検出して
拒否するが、それは実際に DB を開いたときにしか働かない。ディレクトリを分けたまま
座組みだけで衝突を防ぐため、本モジュールの `load_app_migrations()` を
**唯一の採番台帳**とする:

1. 新しいテーブルを追加するマイグレーションを書くときは、既存の
   `infrastructure/db/migrations/`(`sources`/`batches`/`documents` など)か
   `infrastructure/jobs/migrations/`(`jobs`/`resource_leases` など、モジュール名
   `migrations.py` と衝突するため別ディレクトリに退避してある)のどちらかに置く。
   **新しい第3のディレクトリを増やさない。**
2. バージョン番号は必ず `load_app_migrations()` が返す一覧(= 本ファイルが
   import 時点で把握する全パッケージの合算)を確認し、その最大値+1から採番する。
   現時点の最大値は 0003(`infrastructure/db/migrations/0003_sources_batches.sql`)。
   M4(埋め込み)以降でテーブルを追加する場合は 0004 から使うこと。
3. `tests/db/test_migrations.py::test_load_app_migrations_versions_are_unique_and_sequential`
   が `load_app_migrations()` を実DBなしで直接検査し、バージョンの重複・欠番を
   コミットのたびに検出する(2パッケージが独立に採番して衝突する事故を、実際に
   DBを開くまで気づけない状態にしないため)。新しいマイグレーションを追加したら
   このテストがまず先に通ることを確認してから DB を触ること。
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


def open_app_db(db_path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """app.sqlite を開き、スキーマを保証してから返す。呼び出し元が `close()` する。

    `check_same_thread=False` は ASGI 層(MCP Streamable HTTP)専用
    (`connection.py::connect` の docstring 参照)。CLI/stdio MCP は既定の
    `True` のままにする。
    """
    conn = connect(db_path, check_same_thread=check_same_thread)
    ensure_app_schema(conn)
    return conn


__all__ = [
    "ensure_app_schema",
    "load_app_migrations",
    "load_core_migrations",
    "open_app_db",
]
