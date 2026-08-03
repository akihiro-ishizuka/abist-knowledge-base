"""`infrastructure.db.schema`: app.sqlite 全体(jobs + sources/batches/documents)の
唯一のブートストラップ経路。

`infrastructure.jobs.db.ensure_jobs_schema` は自分の既知マイグレーション
(バージョン2)だけを `apply_migrations` へ渡す。これを `ensure_app_schema`
(バージョン2+3を把握)適用後の DB に対して**単独で**呼ぶと、`apply_migrations`
の「DB の版が渡された一覧の最大版より新しければ拒否する」ガード(古いバイナリが
新しい DB を誤って開くことを防ぐためのもの)に、`ensure_jobs_schema` 側が
意図せず引っかかる(DB は既にバージョン3なのに、`ensure_jobs_schema` が知る
最大版は2のため「未来の版を要求された」と誤検知する)。

これは「`ensure_jobs_schema` を単独の入口として使い続けるコード経路がある限り
発生しうる」設計上の制約であり、`ensure_app_schema`/`open_app_db` 側を直しても
解消できない(`ensure_jobs_schema` 自身が把握する一覧を広げない限り)。
そのため、`presentation/cli` 配下で `app_db_path` を開く全コマンド
(`jobs`/`worker`/`source`/`batch`/`document`)は `infrastructure.jobs.db.open_jobs_db`
を直接使わず、必ずこの `open_app_db`(jobs 側の既知マイグレーションも合わせて
1つの一覧として渡す)を使う(`jobs_cmd.py`/`worker_cmd.py` もこのタスクで
切り替え済み)。逆方向(`ensure_jobs_schema` を先に、`ensure_app_schema` を
後に)は `ensure_app_schema` 側がバージョン2を含む全量を渡すため安全である。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.migrations import current_version
from abist_kb.infrastructure.db.schema import ensure_app_schema, open_app_db
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema


def test_ensure_app_schema_creates_core_tables(tmp_root: Path) -> None:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"sources", "batches", "batch_items", "documents", "audit_events"} <= tables
    conn.close()


def test_ensure_app_schema_also_creates_job_tables(tmp_root: Path) -> None:
    """jobs_cmd/worker_cmd と同じ app.sqlite を共有するため、jobs テーブルも含む。"""
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"jobs", "job_events", "worker_leases", "resource_leases"} <= tables
    conn.close()


def test_ensure_jobs_schema_before_ensure_app_schema_is_safe(tmp_root: Path) -> None:
    """`ensure_jobs_schema`(バージョン2のみ把握)を先に呼んでも、後から
    `ensure_app_schema`(バージョン2+3を把握)を同じ接続に対して呼べば
    `MIGRATION_FAILED` にならず、バージョン3まで正しく進む。
    """
    conn = connect(tmp_root / "app.sqlite")
    ensure_jobs_schema(conn)
    assert current_version(conn) == 2
    ensure_app_schema(conn)
    assert current_version(conn) == 3
    conn.close()


def test_open_app_db_returns_usable_connection(tmp_root: Path) -> None:
    conn = open_app_db(tmp_root / "data" / "app.sqlite")
    try:
        conn.execute("SELECT 1 FROM sources")
    finally:
        conn.close()


def test_ensure_app_schema_raises_app_error_on_lock_contention(tmp_root: Path) -> None:
    db_path = tmp_root / "app.sqlite"
    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    contender = connect(db_path, timeout_ms=300)
    try:
        with pytest.raises(AppError):
            ensure_app_schema(contender)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        contender.close()
