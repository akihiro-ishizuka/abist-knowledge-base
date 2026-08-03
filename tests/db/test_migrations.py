import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

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


# 以下はコードレビュー(CRITICAL 3 / IMPORTANT 5 / 付随する堅牢性改善)で
# 指摘された不具合の回帰テスト。


def test_apply_migrations_raises_conflict_on_lock_contention(tmp_root: Path):
    """`_apply_one` 内の `BEGIN IMMEDIATE` がロック競合で失敗した場合、
    生の `sqlite3.OperationalError` ではなく再試行可能な `AppError(CONFLICT)` になること。

    ジョブキューのワーカー・リース取得など、複数プロセスが同時に
    `BEGIN IMMEDIATE` を試みるのは M1 以降で通常運用となるため、
    生の sqlite3 例外が漏れてはならない。
    """
    db_path = tmp_root / "m.sqlite"
    holder = open_db(tmp_root)
    # 先にテーブルを作っておき、`current_version` 自体の暗黙 CREATE TABLE が
    # ロック競合に巻き込まれないようにする(それ自体は今回の検証対象ではない)。
    assert current_version(holder) == 0
    holder.execute("BEGIN IMMEDIATE")

    contender = connect(db_path, timeout_ms=300)
    try:
        with pytest.raises(AppError) as excinfo:
            apply_migrations(contender, [M1])
        assert excinfo.value.code == ErrorCode.CONFLICT
        assert excinfo.value.retryable is True
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        contender.close()


def test_apply_migrations_no_ops_when_already_applied_by_another_connection(tmp_root: Path):
    """未初期化DBへ2接続が同時にブートストラップするレースを再現する。

    B が「バージョン0(未適用)」を読んだ直後に、A が先に M1 を適用・コミットする
    ケースを実スレッド+イベントで確定的に再現する。B はロックを取得した後に
    改めてバージョンを確認し、既に適用済みなら例外を出さず、
    「自分は何も適用しなかった」ことを戻り値(空リスト)で報告できなければならない
    (Web/TUI/CLI/MCP を同時起動すると、各エントリポイントが同じ未初期化DBへ
    同時にマイグレーションを試みるのは設計上の想定内であるため)。
    """
    import abist_kb.infrastructure.db.migrations as migrations_module

    db_path = tmp_root / "race.sqlite"
    conn_a = connect(db_path)

    real_current_version = migrations_module.current_version
    b_read_stale_version = threading.Event()
    a_has_committed = threading.Event()
    # `sqlite3` の接続はそれを作成したスレッドでしか使えないため、B 用の接続は
    # スレッド B の中で作成する。ここにはそのオブジェクトを1個だけ後で入れる。
    conn_b_holder: list[sqlite3.Connection] = []

    def current_version_with_injected_race(conn: sqlite3.Connection) -> int:
        # `apply_migrations` が内部で読む最初の `current_version(conn_b)` 呼び出しを
        # A のコミットが完了するまで足止めし、その間に読み取った「古い」値を返す。
        result = real_current_version(conn)
        if conn_b_holder and conn is conn_b_holder[0]:
            b_read_stale_version.set()
            assert a_has_committed.wait(timeout=5), "A 側の適用がタイムアウトした"
        return result

    b_outcome: list[list[int]] = []

    def run_b() -> None:
        conn_b = connect(db_path)
        conn_b_holder.append(conn_b)
        try:
            with patch.object(
                migrations_module,
                "current_version",
                side_effect=current_version_with_injected_race,
            ):
                b_outcome.append(apply_migrations(conn_b, [M1]))
        finally:
            conn_b.close()

    thread_b = threading.Thread(target=run_b)
    try:
        thread_b.start()
        assert b_read_stale_version.wait(timeout=5), "B 側の読み取りが発生しなかった"

        # A が先に(B の読み取りより後、B の適用より前に)適用・コミットする。
        assert apply_migrations(conn_a, [M1]) == [1]
        assert current_version(conn_a) == 1

        a_has_committed.set()
        thread_b.join(timeout=5)
        assert not thread_b.is_alive(), "B 側のスレッドが完了しなかった"

        # B は例外を出さず、「自分は何も適用しなかった」ことを空リストで報告する。
        assert b_outcome == [[]]

        # conn_b はスレッド B のものなのでここでは使わず、新しい接続で最終状態を検証する。
        verify_conn = connect(db_path)
        try:
            assert current_version(verify_conn) == 1
            table_count = verify_conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='a'"
            ).fetchone()[0]
            assert table_count == 1
        finally:
            verify_conn.close()
    finally:
        conn_a.close()


def test_apply_one_ensures_schema_migrations_table_even_without_prior_call(tmp_root: Path):
    """`_apply_one` を(将来の再配線等で)直接呼んでも `schema_migrations` が無い状態で
    「no such table」にならないこと。呼び出し順序に依存しない堅牢性の回帰テスト。
    """
    from abist_kb.infrastructure.db.migrations import _apply_one

    conn = open_db(tmp_root)
    try:
        applied = _apply_one(conn, M1)
        assert applied is True
        assert current_version(conn) == 1
    finally:
        conn.close()
