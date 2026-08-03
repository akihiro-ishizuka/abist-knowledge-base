"""SQLite 接続ファクトリ(設計書 §9.2, §10)。

全ての接続で WAL ジャーナルモード・外部キー制約・busy_timeout を強制する。
スキーマ変更は本モジュールでは行わず、`migrations.py` の連番マイグレーションに委ねる。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode, wrap

MIN_SQLITE_VERSION: tuple[int, int, int] = (3, 34, 0)

_TRIGRAM_PROBE_TERM = "sqlite"
"""trigram トークナイザーの実測に使う語。3文字未満は既知のトークナイザー仕様で
0件になるため(3文字が最小トークン長)、必ず3文字以上の語を使うこと。"""


def sqlite_version_tuple() -> tuple[int, int, int]:
    """実行中の SQLite ライブラリのバージョンを (major, minor, patch) で返す。"""
    major, minor, patch = (int(part) for part in sqlite3.sqlite_version.split(".")[:3])
    return (major, minor, patch)


def _build_uri(path: Path, *, read_only: bool) -> str:
    """SQLite の URI フィラメントを組み立てる。

    `Path.as_uri()` は使わない(Windows のドライブレターを持つ絶対パスに対して
    `file:///C:/...` 形式を要求せず、素朴な `file:C:/...` 形式で SQLite に受理される
    ことを確認済み)。バックスラッシュを `/` に置換し、URI 予約文字である `%` `?` `#` を
    パーセントエンコードする。これを怠ると、たとえば `#` を含むパスは URI のフラグメント
    区切りと誤認され、`#` 以降が黙って切り捨てられた別ファイルが開かれてしまう
    (実測で確認済みの不具合)。
    """
    resolved = str(path.resolve())
    normalized = resolved.replace("\\", "/")
    escaped = normalized.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    uri = f"file:{escaped}"
    if read_only:
        uri += "?mode=ro"
    return uri


def connect(
    path: Path | str, *, read_only: bool = False, timeout_ms: int = 5000
) -> sqlite3.Connection:
    """SQLite 接続を開く。

    書込接続では WAL ジャーナルモードへ切り替える(読取専用接続への発行は書込に
    あたるため行わない)。`foreign_keys` と `busy_timeout` はどちらの接続種別でも
    設定する。`isolation_level=None` により暗黙のトランザクション開始を無効化し、
    `transaction()` による明示的な `BEGIN IMMEDIATE` 制御を可能にする。
    """
    db_path = Path(path)
    if not read_only:
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise wrap(
                exc,
                code=ErrorCode.CONFIG_ERROR,
                message=f"データベース用のディレクトリを作成できません: {db_path.parent}",
            ) from exc

    uri = _build_uri(db_path, read_only=read_only)
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise wrap(
            exc,
            code=ErrorCode.CONFIG_ERROR,
            message=f"データベースに接続できません: {db_path}",
        ) from exc

    conn.row_factory = sqlite3.Row
    try:
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    except sqlite3.Error as exc:
        conn.close()
        raise wrap(
            exc,
            code=ErrorCode.CONFIG_ERROR,
            message=f"データベースの初期設定に失敗しました: {db_path}",
        ) from exc
    return conn


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """このプロセスで使う SQLite の機能実測結果。"""

    sqlite_version: str
    fts5: bool
    unicode61: bool
    trigram: bool
    problems: tuple[str, ...]
    ok: bool


def _probe_virtual_table(
    conn: sqlite3.Connection, ddl: str, label: str, problems: list[str]
) -> bool:
    try:
        conn.execute(ddl)
    except sqlite3.Error as exc:
        problems.append(f"{label} が利用できません: {exc}")
        return False
    return True


def _probe_trigram(conn: sqlite3.Connection, problems: list[str]) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE probe_trigram USING fts5(body, tokenize='trigram')")
        conn.execute("INSERT INTO probe_trigram(body) VALUES (?)", (_TRIGRAM_PROBE_TERM,))
        row = conn.execute(
            "SELECT count(*) FROM probe_trigram WHERE probe_trigram MATCH ?",
            (_TRIGRAM_PROBE_TERM[:3],),
        ).fetchone()
    except sqlite3.Error as exc:
        problems.append(f"trigram トークナイザーが利用できません: {exc}")
        return False
    if row is None or row[0] == 0:
        problems.append(
            "trigram トークナイザーが3文字語の検索で0件を返しました"
            "(3文字未満のトークン化は既知の仕様上の制約であり、"
            "本チェックは意図的に3文字以上の語で実測しています)。"
        )
        return False
    return True


def check_sqlite_capabilities() -> CapabilityReport:
    """FTS5・unicode61・trigram をメモリDB上で実測する。

    バージョン不足や機能欠如は `problems` に日本語の説明として積む。
    実測中の例外は握り潰さず、同じく `problems` へ記録する
    (呼び出し側が `AppError` へ正規化するかどうかを選べるよう、ここでは送出しない)。
    """
    problems: list[str] = []
    version = sqlite_version_tuple()
    if version < MIN_SQLITE_VERSION:
        problems.append(
            "SQLite のバージョンが古すぎます"
            f"(検出: {sqlite3.sqlite_version}, "
            f"必要: {'.'.join(str(v) for v in MIN_SQLITE_VERSION)} 以上)。"
        )

    conn = sqlite3.connect(":memory:")
    try:
        fts5 = _probe_virtual_table(
            conn, "CREATE VIRTUAL TABLE probe_fts5 USING fts5(body)", "FTS5", problems
        )
        unicode61 = _probe_virtual_table(
            conn,
            "CREATE VIRTUAL TABLE probe_unicode61 USING fts5(body, tokenize='unicode61')",
            "unicode61 トークナイザー",
            problems,
        )
        trigram = _probe_trigram(conn, problems)
    finally:
        conn.close()

    ok = version >= MIN_SQLITE_VERSION and fts5 and unicode61 and trigram and not problems
    return CapabilityReport(
        sqlite_version=sqlite3.sqlite_version,
        fts5=fts5,
        unicode61=unicode61,
        trigram=trigram,
        problems=tuple(problems),
        ok=ok,
    )


def is_lock_contention(exc: sqlite3.Error) -> bool:
    """SQLite のロック競合(`SQLITE_BUSY` / `SQLITE_LOCKED` 系)を表す例外か判定する。

    メッセージ文字列(`"database is locked"` 等、ロケールや将来の文言変更に弱い)
    ではなく、Python 3.11 以降で全ての `sqlite3` 例外に付与される
    `sqlite_errorname`(拡張結果コード名。例: `SQLITE_BUSY`, `SQLITE_BUSY_TIMEOUT`,
    `SQLITE_LOCKED_SHAREDCACHE`)で判定する。本プロジェクトは Python 3.12 固定
    (`pyproject.toml` の `requires-python`)のためこの属性は常に存在する。
    """
    name = getattr(exc, "sqlite_errorname", None) or ""
    return name.startswith("SQLITE_BUSY") or name.startswith("SQLITE_LOCKED")


def wrap_begin_immediate_failure(exc: sqlite3.Error) -> AppError:
    """`BEGIN IMMEDIATE` の失敗を `AppError` へ正規化する。

    ロック競合(他接続が同時に書込トランザクションを保持している)は日常的に
    起こり得る運用状態であり、`sqlite3.OperationalError` を生で送出してはならない。
    ジョブキューのワーカー・リソースリース取得のように、複数プロセスが同時に
    `BEGIN IMMEDIATE` を試みる設計を M1 以降で前提にしているため、
    `ErrorCode.CONFLICT` / `retryable=True` として呼び出し側が再試行を判断できる
    形にする。ロック競合以外の失敗(稀だが起こり得る)は一般的な失敗として扱い、
    再試行可能とは見なさない。
    """
    if is_lock_contention(exc):
        return wrap(
            exc,
            code=ErrorCode.CONFLICT,
            message="データベースが他の接続でロックされているため、処理を開始できません。",
            hint="しばらく待ってから再試行してください。",
            retryable=True,
            exit_code=ExitCode.CONFLICT,
        )
    return wrap(
        exc,
        code=ErrorCode.FAILURE,
        message="トランザクションを開始できませんでした。",
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`BEGIN IMMEDIATE` で書込ロックを即座に取得する明示トランザクション。

    正常終了で `COMMIT`、例外発生で `ROLLBACK` してから再送出する。
    `AppError` を含むあらゆる例外で確実にロールバックするため `BaseException` を捕捉する。
    `BEGIN IMMEDIATE` 自体はこの `try` の外側で発行すると、ロック競合時に生の
    `sqlite3.OperationalError` がそのまま呼び出し側へ漏れてしまう(実測で確認済み)。
    そのため `BEGIN IMMEDIATE` も専用の `try/except` で保護し、
    `wrap_begin_immediate_failure` で正規化する。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise wrap_begin_immediate_failure(exc) from exc
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


__all__ = [
    "MIN_SQLITE_VERSION",
    "CapabilityReport",
    "check_sqlite_capabilities",
    "connect",
    "is_lock_contention",
    "sqlite_version_tuple",
    "transaction",
    "wrap_begin_immediate_failure",
]
