import sqlite3
from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.connection import (
    MIN_SQLITE_VERSION,
    check_sqlite_capabilities,
    connect,
    sqlite_version_tuple,
    transaction,
)


def test_sqlite_version_meets_minimum():
    assert sqlite_version_tuple() >= MIN_SQLITE_VERSION


def test_connect_applies_required_pragmas(tmp_root: Path):
    db = tmp_root / "data" / "app.sqlite"
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_connect_creates_parent_directories(tmp_root: Path):
    db = tmp_root / "深い" / "階層" / "app.sqlite"
    conn = connect(db)
    conn.close()
    assert db.is_file()


def test_rows_are_accessible_by_column_name(tmp_root: Path):
    conn = connect(tmp_root / "a.sqlite")
    try:
        conn.execute("CREATE TABLE t (path TEXT, n INTEGER)")
        conn.execute("INSERT INTO t VALUES ('docs/蛇腹.md', 3)")
        row = conn.execute("SELECT path, n FROM t").fetchone()
        assert row["path"] == "docs/蛇腹.md"
        assert row["n"] == 3
    finally:
        conn.close()


def test_read_only_connection_rejects_writes(tmp_root: Path):
    db = tmp_root / "ro.sqlite"
    w = connect(db)
    w.execute("CREATE TABLE t (x INTEGER)")
    w.commit()
    w.close()

    r = connect(db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            r.execute("INSERT INTO t VALUES (1)")
    finally:
        r.close()


def test_read_only_connection_to_missing_file_raises_app_error(tmp_root: Path):
    with pytest.raises(AppError):
        connect(tmp_root / "ない.sqlite", read_only=True)


def test_transaction_commits_on_success(tmp_root: Path):
    conn = connect(tmp_root / "t.sqlite")
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        with transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_transaction_rolls_back_on_error(tmp_root: Path):
    conn = connect(tmp_root / "t.sqlite")
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        with pytest.raises(RuntimeError), transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_capability_report_on_this_machine():
    report = check_sqlite_capabilities()
    assert report.fts5 is True
    assert report.unicode61 is True
    assert report.trigram is True
    assert report.ok is True
    assert report.problems == ()
    assert report.sqlite_version == sqlite3.sqlite_version


# レビュー指摘 CRITICAL 3 の回帰テスト:
# 別接続が `BEGIN IMMEDIATE` で書込ロックを保持している間に `transaction()` を
# 呼ぶと、`BEGIN IMMEDIATE` 自体が SQLITE_BUSY で失敗する。これは生の
# `sqlite3.OperationalError` ではなく、再試行可能な `AppError(ErrorCode.CONFLICT)`
# へ正規化されなければならない(ジョブキューのワーカー・リース取得など、
# 複数プロセスが同時に `BEGIN IMMEDIATE` を試みるのは通常運用のため)。
def test_transaction_raises_conflict_on_lock_contention(tmp_root: Path):
    db = tmp_root / "locked.sqlite"
    holder = connect(db)
    holder.execute("CREATE TABLE t (x INTEGER)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES (1)")

    contender = connect(db, timeout_ms=300)
    try:
        with pytest.raises(AppError) as excinfo, transaction(contender):
            pass  # pragma: no cover - ロック競合で BEGIN 自体が失敗するため到達しない
        assert excinfo.value.code == ErrorCode.CONFLICT
        assert excinfo.value.retryable is True
        assert excinfo.value.exit_code == ExitCode.CONFLICT
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        contender.close()
