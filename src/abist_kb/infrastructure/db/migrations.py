"""番号付きマイグレーションランナー(設計書 §9.2, §10)。

スキーマ変更はすべて本モジュール経由で適用し、`schema_migrations` に履歴を残す。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from abist_kb.domain.errors import AppError, ErrorCode, wrap
from abist_kb.infrastructure.db.connection import wrap_begin_immediate_failure

_MIGRATION_FILENAME = re.compile(r"^(\d{4})_(.+)\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    """1つのスキーマ変更。

    `sql` は通常は複数文をまとめた1本の文字列でよい(内部で安全に分割する)。
    分割の推測に頼りたくない場合(トリガ本体を独自の理由で分けて管理したいなど)は
    `tuple[str, ...]` として文ごとに渡すこともでき、その場合は一切分割せず
    要素をそのまま順に実行する。
    """

    version: int
    name: str
    sql: str | tuple[str, ...]


def _split_sql_statements(sql: str) -> list[str]:
    """SQL 文字列を完全な文単位へ分割する。

    素朴に `;` で分割すると、文字列リテラル内の `;` や
    `CREATE TRIGGER ... BEGIN ... END;` のトリガ本体内の `;` を壊してしまう。
    標準ライブラリの `sqlite3.complete_statement()` は SQLite 本体の
    `sqlite3_complete()` を薄くラップしたもので、文字列リテラル・コメントに加えて
    トリガ本体の `BEGIN`/`END` 対応も認識した上で「文として完結しているか」を
    判定する専用のステートマシンを持つ。独自の正規表現分割器を書く代わりに
    これへ委譲することで、トリガや文字列リテラル中の `;` を誤って分割点として
    扱う不具合を避ける。
    """
    statements: list[str] = []
    buf = ""
    for ch in sql:
        buf += ch
        if ch == ";" and sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    tail = buf.strip()
    if tail:
        statements.append(tail)
    return statements


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    """`schema_migrations` が無ければ作る。

    このステートメントはオートコミットモードで発行される素朴な DDL だが、
    `schema_migrations` がまだ存在しない真っさらなデータベースでは実際の
    書込(テーブル作成)であり書込ロックを要求する(テーブルが既に存在すれば
    メタデータ参照に短絡され、WAL の読者は書き手にブロックされないため
    問題にならない ―― 未初期化DBに限って踏み抜く経路)。複数プロセスが
    同時に未初期化DBへ `apply_migrations` を試みるのは通常運用(Web/TUI/CLI/MCP
    の同時起動)であるため、`transaction()`/`_apply_one` の `BEGIN IMMEDIATE` と
    同じく `wrap_begin_immediate_failure` を再利用してロック競合を
    `AppError(ErrorCode.CONFLICT, retryable=True)` へ正規化する
    (判定ロジックを重複させず、他の2箇所と同一の正規化結果にするため)。
    """
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "applied_at TEXT NOT NULL"
            ")"
        )
    except sqlite3.Error as exc:
        raise wrap_begin_immediate_failure(exc) from exc


def current_version(conn: sqlite3.Connection) -> int:
    """適用済みマイグレーションの最大バージョン。未適用なら 0。"""
    _ensure_schema_migrations_table(conn)
    row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
    value = row[0] if row is not None else None
    return int(value) if value is not None else 0


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> bool:
    """1つのマイグレーションを `BEGIN IMMEDIATE` 〜 `COMMIT` の1トランザクションで適用する。

    実際に適用した場合は `True`、他の接続が同時にブートストラップして
    既に適用済みだったため何もしなかった場合は `False` を返す。

    `sqlite3` の `executescript()` は呼び出し前に暗黙の `COMMIT` を発行してしまい、
    ここで開始した `BEGIN IMMEDIATE` を台無しにする(未定義動作の温床になる)。
    そのため `executescript` は使わず、`_split_sql_statements` で分割した文を
    1つずつ `execute()` する。失敗時は `ROLLBACK` してから `AppError` を送出するため、
    途中まで実行された文の効果は残らない。
    `sqlite3.Error` 以外の例外(バグ等)でもトランザクションを開いたままにしないよう
    `BaseException` を捕捉してロールバックしてから再送出する。

    呼び出し順序に依存しないよう、このスコープでも `schema_migrations` の存在を
    保証する(呼び出し元の `apply_migrations` は `current_version` 経由で既に
    保証しているが、`_apply_one` を直接呼ぶ将来の再配線でも安全にする)。
    """
    _ensure_schema_migrations_table(conn)
    statements = (
        list(migration.sql)
        if isinstance(migration.sql, tuple)
        else _split_sql_statements(migration.sql)
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise wrap_begin_immediate_failure(exc) from exc
    try:
        # ロックを取得した直後に改めてバージョンを確認する。呼び出し元
        # `apply_migrations` が読んだバージョンは、ロック待ちの間に他の接続が
        # 同時にブートストラップして古くなっている可能性がある
        # (Web/TUI/CLI/MCP を同時起動すると各エントリポイントの
        # WorkerSupervisor が未初期化DBへ同時にマイグレーションを試みうるため、
        # これは通常運用であり、"table already exists" のようなエラーにしてはならない)。
        if migration.version <= current_version(conn):
            conn.execute("ROLLBACK")
            return False
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(UTC).isoformat()),
        )
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise wrap(
            exc,
            code=ErrorCode.MIGRATION_FAILED,
            message=(
                f"マイグレーション {migration.version:04d}_{migration.name} の適用に失敗しました。"
            ),
            details={"version": migration.version, "name": migration.name},
        ) from exc
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
        return True


def apply_migrations(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> list[int]:
    """未適用のマイグレーションをバージョン昇順に適用し、適用したバージョン一覧を返す。

    同じバージョン番号の重複、および DB が把握済みマイグレーションの最大バージョンより
    新しい場合は `AppError(ErrorCode.MIGRATION_FAILED)` を送出する。
    """
    versions = [m.version for m in migrations]
    duplicates = sorted({v for v in versions if versions.count(v) > 1})
    if duplicates:
        raise AppError(
            code=ErrorCode.MIGRATION_FAILED,
            message=f"マイグレーションのバージョン番号が重複しています: {duplicates}",
            details={"duplicates": duplicates},
        )

    applied = current_version(conn)
    known_max = max(versions) if versions else 0
    if applied > known_max:
        raise AppError(
            code=ErrorCode.MIGRATION_FAILED,
            message=(
                f"データベースのスキーマ(バージョン {applied})が本バージョンが把握する"
                f"最新マイグレーション(バージョン {known_max})より新しいため開けません。"
            ),
            details={"database_version": applied, "known_max_version": known_max},
        )

    pending = sorted((m for m in migrations if m.version > applied), key=lambda m: m.version)
    newly_applied: list[int] = []
    for migration in pending:
        # `_apply_one` はロック取得後に改めてバージョンを再確認し、他の接続が
        # 同時にブートストラップして既に適用済みなら `False` を返す。その場合は
        # このプロセスが適用したわけではないので `newly_applied` に含めない。
        if _apply_one(conn, migration):
            newly_applied.append(migration.version)
    return newly_applied


def load_migrations(directory: Path) -> list[Migration]:
    """`NNNN_name.sql` 形式のファイルを読み込み、バージョン昇順で返す。"""
    migrations: list[Migration] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = _MIGRATION_FILENAME.match(path.name)
        if match is None:
            continue
        version = int(match.group(1))
        name = match.group(2)
        sql = path.read_text(encoding="utf-8")
        migrations.append(Migration(version=version, name=name, sql=sql))
    migrations.sort(key=lambda m: m.version)
    return migrations


__all__ = [
    "Migration",
    "apply_migrations",
    "current_version",
    "load_migrations",
]
