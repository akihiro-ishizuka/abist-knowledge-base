from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.migrations import (
    Migration,
    apply_migrations,
    current_version,
    load_migrations,
)

M1 = Migration(version=1, name="core", sql="CREATE TABLE a (id INTEGER PRIMARY KEY);")
M2 = Migration(version=2, name="jobs", sql="CREATE TABLE b (id INTEGER PRIMARY KEY);")


def open_db(tmp_root: Path):
    return connect(tmp_root / "m.sqlite")


def test_applies_migrations_in_order(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        assert apply_migrations(conn, [M1, M2]) == [1, 2]
        assert current_version(conn) == 2
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"a", "b"} <= names
    finally:
        conn.close()


def test_is_idempotent(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        apply_migrations(conn, [M1, M2])
        assert apply_migrations(conn, [M1, M2]) == []
        assert current_version(conn) == 2
    finally:
        conn.close()


def test_applies_only_new_migrations(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        apply_migrations(conn, [M1])
        assert apply_migrations(conn, [M1, M2]) == [2]
    finally:
        conn.close()


def test_records_history_in_schema_migrations(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        apply_migrations(conn, [M1, M2])
        rows = conn.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [r["version"] for r in rows] == [1, 2]
        assert [r["name"] for r in rows] == ["core", "jobs"]
        assert all(r["applied_at"] for r in rows)
    finally:
        conn.close()


def test_current_version_is_zero_on_empty_database(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        assert current_version(conn) == 0
    finally:
        conn.close()


def test_failed_migration_rolls_back_and_is_not_recorded(tmp_root: Path):
    bad = Migration(version=2, name="bad", sql="CREATE TABLE b (x); SELECT bogus();")
    conn = open_db(tmp_root)
    try:
        with pytest.raises(AppError) as excinfo:
            apply_migrations(conn, [M1, bad])
        assert excinfo.value.code == ErrorCode.MIGRATION_FAILED
        assert current_version(conn) == 1
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "b" not in names
    finally:
        conn.close()


def test_duplicate_versions_are_rejected(tmp_root: Path):
    dup = Migration(version=1, name="other", sql="CREATE TABLE c (x INTEGER);")
    conn = open_db(tmp_root)
    try:
        with pytest.raises(AppError):
            apply_migrations(conn, [M1, dup])
    finally:
        conn.close()


def test_database_newer_than_known_migrations_is_refused(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        apply_migrations(conn, [M1, M2])
        with pytest.raises(AppError) as excinfo:
            apply_migrations(conn, [M1])
        assert excinfo.value.code == ErrorCode.MIGRATION_FAILED
        assert "新しい" in excinfo.value.message
    finally:
        conn.close()


def test_load_migrations_reads_numbered_sql_files(tmp_root: Path):
    d = tmp_root / "migrations"
    d.mkdir()
    (d / "0002_jobs.sql").write_text("CREATE TABLE b (x INTEGER);", encoding="utf-8")
    (d / "0001_core.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
    (d / "README.md").write_text("無視される", encoding="utf-8")
    loaded = load_migrations(d)
    assert [(m.version, m.name) for m in loaded] == [(1, "core"), (2, "jobs")]


# 以下は brief の必須テストではなく、タスク説明で名指しされた鋭利な角
# (「トリガ本体やリテラル中のセミコロンを含むマイグレーションも1トランザクションで
# 適用でき、失敗時テストは本当に何も残さずロールバックすること」)を
# 実測で保証するための追加テスト。


def test_migration_with_trigger_body_applies_atomically(tmp_root: Path):
    """トリガ本体内の `;` が文の区切りと誤認されず、1トランザクションで適用されること。"""
    trigger_migration = Migration(
        version=1,
        name="with_trigger",
        sql=(
            "CREATE TABLE counters (name TEXT PRIMARY KEY, n INTEGER NOT NULL);"
            "CREATE TABLE counter_log (name TEXT NOT NULL, changed_at TEXT NOT NULL);"
            "CREATE TRIGGER trg_counters_ai AFTER UPDATE ON counters "
            "BEGIN "
            "  INSERT INTO counter_log (name, changed_at) VALUES (NEW.name, 'x');"
            "  UPDATE counters SET n = n WHERE name = NEW.name;"
            "END;"
        ),
    )
    conn = open_db(tmp_root)
    try:
        assert apply_migrations(conn, [trigger_migration]) == [1]
        assert current_version(conn) == 1
        conn.execute("INSERT INTO counters (name, n) VALUES ('a', 1)")
        conn.execute("UPDATE counters SET n = 2 WHERE name = 'a'")
        logged = conn.execute("SELECT count(*) FROM counter_log").fetchone()[0]
        assert logged == 1
    finally:
        conn.close()


def test_migration_with_semicolon_in_string_literal_is_not_split(tmp_root: Path):
    """文字列リテラル中の `;` が文の区切りと誤認されないこと。"""
    literal_migration = Migration(
        version=1,
        name="with_literal_semicolon",
        sql=(
            "CREATE TABLE notes (body TEXT);"
            "INSERT INTO notes (body) VALUES ('a;b');"
            "INSERT INTO notes (body) VALUES ('c');"
        ),
    )
    conn = open_db(tmp_root)
    try:
        apply_migrations(conn, [literal_migration])
        rows = [r[0] for r in conn.execute("SELECT body FROM notes ORDER BY body")]
        assert rows == ["a;b", "c"]
    finally:
        conn.close()


def test_migration_with_trigger_rolls_back_fully_on_later_failure(tmp_root: Path):
    """トリガを含むマイグレーションが後続文の失敗で完全にロールバックされること。"""
    bad_with_trigger = Migration(
        version=1,
        name="bad_with_trigger",
        sql=(
            "CREATE TABLE counters (name TEXT PRIMARY KEY, n INTEGER NOT NULL);"
            "CREATE TRIGGER trg_counters_ai AFTER UPDATE ON counters "
            "BEGIN "
            "  UPDATE counters SET n = n WHERE name = NEW.name;"
            "END;"
            "SELECT bogus();"
        ),
    )
    conn = open_db(tmp_root)
    try:
        with pytest.raises(AppError) as excinfo:
            apply_migrations(conn, [bad_with_trigger])
        assert excinfo.value.code == ErrorCode.MIGRATION_FAILED
        assert current_version(conn) == 0
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "counters" not in names
    finally:
        conn.close()


def test_migration_sql_as_tuple_bypasses_splitting(tmp_root: Path):
    """`Migration.sql` をタプルで渡した場合、分割せずそのまま順に実行されること。"""
    tuple_migration = Migration(
        version=1,
        name="tuple_form",
        sql=(
            "CREATE TABLE t (x INTEGER)",
            "INSERT INTO t (x) VALUES (1)",
        ),
    )
    conn = open_db(tmp_root)
    try:
        assert apply_migrations(conn, [tuple_migration]) == [1]
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 1
    finally:
        conn.close()
