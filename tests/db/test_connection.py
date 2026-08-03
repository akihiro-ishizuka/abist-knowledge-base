import sqlite3
from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.connection import (
    MIN_SQLITE_VERSION,
    check_sqlite_capabilities,
    connect,
    is_lock_contention,
    sqlite_version_tuple,
    transaction,
    wrap_begin_immediate_failure,
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


def test_connect_handles_uri_reserved_characters_in_path(tmp_root: Path):
    """I4(IMPORTANT)の回帰テスト。`_build_uri` は SQLite の URI フィラメント予約文字
    (`%` `?` `#`)をパーセントエンコードしなければならない。`#` はNTFS上有効な
    ファイル名文字だが、URIとしてはフラグメント区切りであり、これをエスケープせずに
    `sqlite3.connect(..., uri=True)` へ渡すと `#` 以降が黙って切り捨てられ、
    たとえば `a#b%c.sqlite` のつもりが `a` という全く別のファイルを黙って
    開いてしまう(実測で確認済みのデータ破損バグ)。将来 `Path.as_uri()` への
    「簡略化」でこのエスケープ処理が失われることを防ぐための固定化。
    """
    db_path = tmp_root / "a#b%c.sqlite"
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
    finally:
        conn.close()

    # 意図した通りのファイル名そのもので作成されていること(`a` 等の別名ではない)。
    assert db_path.is_file()
    assert {p.name for p in tmp_root.iterdir() if p.suffix == ".sqlite"} == {"a#b%c.sqlite"}

    # 同じ Path から2回目の接続を開いても同じファイルを読めること。
    reopened = connect(db_path)
    try:
        row = reopened.execute("SELECT x FROM t").fetchone()
        assert row[0] == 42
    finally:
        reopened.close()


def test_immutable_read_only_connection_does_not_create_wal_sidecar_files(tmp_root: Path):
    """小項目の回帰テスト: WAL モードのDBを `mode=ro` のみで開くと、変更しないことが
    分かっていても SQLite は整合性確認のために `-wal`/`-shm` を元DBの隣に新規作成
    してしまう。§11.1 は移行元DBを無傷のまま残すことを要求するため、
    `immutable=True` を指定した読み取り専用接続はこれらのサイドカーファイルを
    一切作らないこと(=移行元のディレクトリを汚さないこと)を確認する。
    """
    db = tmp_root / "source.sqlite"
    writer = connect(db)
    writer.execute("CREATE TABLE t (x INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    writer.close()

    before = {p.name for p in tmp_root.iterdir()}

    reader = connect(db, read_only=True, immutable=True)
    try:
        assert reader.execute("SELECT x FROM t").fetchone()[0] == 1
        after = {p.name for p in tmp_root.iterdir()}
        assert after == before, f"immutable読取がサイドカーを作成した: {after - before}"
    finally:
        reader.close()


def test_capability_report_on_this_machine():
    report = check_sqlite_capabilities()
    assert report.fts5 is True
    assert report.unicode61 is True
    assert report.trigram is True
    assert report.external_content is True
    assert report.bm25 is True
    assert report.ok is True
    assert report.problems == ()
    assert report.sqlite_version == sqlite3.sqlite_version


def test_capability_report_flags_external_content_failure(monkeypatch):
    """テスト網羅の抜け: §4 が2つの検索索引(実務/参照コーパス)の実装に前提とする
    external-content FTS5 テーブルと `bm25()` ランキング関数は、実測されず、
    黙って使えると仮定されていた。ここでは external-content テーブルの作成が
    実際に失敗する場合、`CapabilityReport.external_content` が `False` になり
    `problems` に理由が残ることを確認する(feature detection の失敗経路)。

    `sqlite3.Connection` は組込みの immutable 型で、そのインスタンスメソッドを
    直接 monkeypatch できないため、`sqlite3.connect` 自体を薄いプロキシへ
    差し替え、特定のDDLだけ失敗させる(それ以外は実接続へ委譲する)。
    """
    import sqlite3 as sqlite3_module

    from abist_kb.infrastructure.db import connection as connection_module

    class _BreakingConnection:
        def __init__(self, real: sqlite3_module.Connection) -> None:
            self._real = real

        def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            if "probe_external_content" in sql and "USING fts5" in sql:
                raise sqlite3_module.OperationalError(
                    "external content tables disabled (simulated)"
                )
            return self._real.execute(sql, *args, **kwargs)

        def close(self) -> None:
            self._real.close()

    real_connect = sqlite3_module.connect

    def _fake_connect(target, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if target == ":memory:":
            return _BreakingConnection(real_connect(target, *args, **kwargs))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(connection_module.sqlite3, "connect", _fake_connect)
    report = connection_module.check_sqlite_capabilities()
    assert report.external_content is False
    assert report.ok is False
    assert any("external" in p.lower() for p in report.problems)


# レビュー指摘 CRITICAL 3 の回帰テスト:
# 別接続が `BEGIN IMMEDIATE` で書込ロックを保持している間に `transaction()` を
# 呼ぶと、`BEGIN IMMEDIATE` 自体が SQLITE_BUSY で失敗する。これは生の
# `sqlite3.OperationalError` ではなく、再試行可能な `AppError(ErrorCode.CONFLICT)`
# へ正規化されなければならない(ジョブキューのワーカー・リース取得など、
# 複数プロセスが同時に `BEGIN IMMEDIATE` を試みるのは通常運用のため)。
def test_transaction_normalizes_commit_failure_and_recovers_connection(tmp_root: Path):
    """I1(IMPORTANT)の回帰テスト: `COMMIT` は `try` の外側(`else` 節)で発行されて
    いたため、`COMMIT` 自体が失敗する経路(例: `PRAGMA defer_foreign_keys=ON` で
    遅延された外部キー制約違反)では生の `sqlite3.IntegrityError` がそのまま漏れ、
    かつ `ROLLBACK` されないまま接続がトランザクション内に取り残されていた。
    以降の `transaction()` はすべて `AppError(FAILURE, "トランザクションを開始
    できませんでした")` という、原因と無関係でロック競合を思わせるメッセージで
    失敗し続ける(§10.1 のワーカーは1つの接続を5秒ハートビートで長時間保持するため、
    1回の制約違反がプロセス寿命全体を道連れにする)。
    """
    conn = connect(tmp_root / "commit_fail.sqlite")
    try:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
        )
        conn.execute("PRAGMA defer_foreign_keys=ON")

        with pytest.raises(AppError) as excinfo, transaction(conn):
            conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")
        assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
        assert conn.in_transaction is False, "COMMIT失敗後はROLLBACKされて解放されていること"

        # 接続は使い物にならなくなっていない: 次の transaction() が正常に完結する。
        with transaction(conn):
            conn.execute("INSERT INTO parent (id) VALUES (999)")
        assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 1
    finally:
        conn.close()


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


def test_wrap_begin_immediate_failure_non_lock_branch_stays_failure_and_not_retryable(
    tmp_root: Path,
):
    """テスト計画書に「手作業でchmod'd read-only DBを使って検証済み」とだけ
    記録され、コミットされたテストが存在しなかった項目。`is_lock_contention` /
    `wrap_begin_immediate_failure` の非ロック分岐が `FAILURE`/非再試行のままで
    あることを、実際の(合成ではない)`sqlite3.OperationalError`
    (`SQLITE_READONLY`)で固定化する。

    `design/plans/M0-foundation.md` は後続マイルストーンに「恒久エラーは
    FAILURE/非再試行のまま」と申し送っている。この述語をメッセージ文字列一致に
    緩めてしまうと CI では何も壊れないまま、M3 のワーカーで恒久エラーが
    無限リトライループに変わる。

    `BEGIN IMMEDIATE` 自体は読み取り専用接続でも(実測により)成功することが
    多く、実際に失敗するのは最初の書込み文で SQLite が遅延評価するタイミングの
    ため、ここでは `transaction()` 経由ではなく、実際に読み取り専用データベースへ
    書き込もうとして得られる本物の `SQLITE_READONLY` を
    `wrap_begin_immediate_failure`/`is_lock_contention` へ直接渡して検証する
    (この2関数はどちらも任意の `sqlite3.Error` を受け取る純粋関数であり、
    呼び出し元が `BEGIN IMMEDIATE` の文脈からのものかを区別しない)。
    """
    db = tmp_root / "readonly.sqlite"
    writer = connect(db)
    writer.execute("CREATE TABLE t (x INTEGER)")
    writer.commit()
    writer.close()

    reader = connect(db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            reader.execute("INSERT INTO t VALUES (1)")
    finally:
        reader.close()

    raw_exc = excinfo.value
    assert raw_exc.sqlite_errorname == "SQLITE_READONLY"
    assert is_lock_contention(raw_exc) is False

    err = wrap_begin_immediate_failure(raw_exc)
    assert err.code == ErrorCode.FAILURE
    assert err.retryable is False
    assert err.exit_code == ExitCode.FAILURE
