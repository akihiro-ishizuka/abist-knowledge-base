"""ワーカーリース・リソースリース(設計書 §10.1, §10.2)。

`worker_leases`(単一行、リーダー選出)と `resource_leases`(区画ごとの排他)は
どちらも `BEGIN IMMEDIATE` トランザクションの下でのみ読み書きする。これにより
プロセス内 `asyncio.Lock` ではなく SQLite 自身が複数プロセス間の調停を行う
(§10.2 の明示的な要求)。

**docs-write の排他方針(brief Step 2 で決定・記録する契約)**:
設計書 §10.2 は「バッチ・esa・Web・Git同期を全プロセス横断で直列化する」と
書いており、「拒否する」ではなく「直列化する」という語を使っている。そのため
`acquire_resource_lease` の既定は `wait=True`: 2つ目の取得者は最初の解放を
待ってから取得できる(相手を CONFLICT で弾かない)。既存の同期 MCP ツールが
維持すべき busy/`CONCURRENT_RENDER` 系の即時エラー意味論(§10.2 末尾)は、
呼び出し側が明示的に `wait=False` を渡すことで再現できるようにしてある
(`render` の単一実行制限などが将来これを使う想定)。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.job import ResourceKind
from abist_kb.domain.job import resource_key as build_resource_key
from abist_kb.infrastructure.db.connection import transaction

_WORKER_LEASE_ROW_ID = 1
DEFAULT_POLL_INTERVAL_SECONDS = 0.1


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _now() -> datetime:
    return datetime.now(UTC)


# -- worker_leases(単一行、リーダー選出) ------------------------------------


def try_acquire_worker_lease(
    conn: sqlite3.Connection, owner_id: str, *, ttl_seconds: float
) -> bool:
    """ノンブロッキングで1回だけ試みる。取得(または既に自分がリーダー)なら True。

    `BEGIN IMMEDIATE` の下で「現在のリーダーが誰か・期限切れか」を確認してから
    行を作成/更新するため、複数プロセスが同時に呼んでも DB が調停する。
    """
    now = _now()
    expires_at = now + timedelta(seconds=ttl_seconds)
    with transaction(conn):
        row = conn.execute(
            "SELECT owner_id, expires_at FROM worker_leases WHERE id = ?",
            (_WORKER_LEASE_ROW_ID,),
        ).fetchone()
        held_by_other = (
            row is not None and row["owner_id"] != owner_id and _parse(row["expires_at"]) > now
        )
        if held_by_other:
            return False
        conn.execute(
            "INSERT INTO worker_leases (id, owner_id, heartbeat_at, expires_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "owner_id = excluded.owner_id, "
            "heartbeat_at = excluded.heartbeat_at, "
            "expires_at = excluded.expires_at",
            (_WORKER_LEASE_ROW_ID, owner_id, _iso(now), _iso(expires_at)),
        )
    return True


def renew_worker_lease(conn: sqlite3.Connection, owner_id: str, *, ttl_seconds: float) -> bool:
    """自分が現在のリーダーである場合のみ heartbeat/期限を更新する。"""
    now = _now()
    expires_at = now + timedelta(seconds=ttl_seconds)
    with transaction(conn):
        cur = conn.execute(
            "UPDATE worker_leases SET heartbeat_at = ?, expires_at = ? "
            "WHERE id = ? AND owner_id = ?",
            (_iso(now), _iso(expires_at), _WORKER_LEASE_ROW_ID, owner_id),
        )
    return cur.rowcount == 1


def release_worker_lease(conn: sqlite3.Connection, owner_id: str) -> None:
    """自分が保持しているリーダー権を明示的に手放す(正常終了時のみ意味がある)。"""
    with transaction(conn):
        conn.execute(
            "DELETE FROM worker_leases WHERE id = ? AND owner_id = ?",
            (_WORKER_LEASE_ROW_ID, owner_id),
        )


def current_worker_lease(conn: sqlite3.Connection) -> tuple[str, datetime] | None:
    """現在の `(owner_id, expires_at)`。行が無ければ `None`。"""
    row = conn.execute(
        "SELECT owner_id, expires_at FROM worker_leases WHERE id = ?",
        (_WORKER_LEASE_ROW_ID,),
    ).fetchone()
    if row is None:
        return None
    return row["owner_id"], _parse(row["expires_at"])


def has_live_worker(conn: sqlite3.Connection) -> bool:
    """`--detach` の契約判定に使う: 期限切れでない worker heartbeat が存在するか。"""
    lease = current_worker_lease(conn)
    return lease is not None and lease[1] > _now()


# -- resource_leases(区画ごとの排他) -----------------------------------------


def _try_take_resource_lease(
    conn: sqlite3.Connection,
    resource_key_value: str,
    owner_id: str,
    *,
    ttl_seconds: float,
    job_id: str | None,
) -> bool:
    now = _now()
    expires_at = now + timedelta(seconds=ttl_seconds)
    with transaction(conn):
        row = conn.execute(
            "SELECT owner_id, expires_at FROM resource_leases WHERE resource_key = ?",
            (resource_key_value,),
        ).fetchone()
        held_by_other = (
            row is not None and row["owner_id"] != owner_id and _parse(row["expires_at"]) > now
        )
        if held_by_other:
            return False
        conn.execute(
            "INSERT INTO resource_leases (resource_key, owner_id, job_id, expires_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (resource_key) DO UPDATE SET "
            "owner_id = excluded.owner_id, "
            "job_id = excluded.job_id, "
            "expires_at = excluded.expires_at",
            (resource_key_value, owner_id, job_id, _iso(expires_at)),
        )
    return True


def renew_resource_lease(
    conn: sqlite3.Connection, resource_key_value: str, owner_id: str, *, ttl_seconds: float
) -> bool:
    now = _now()
    expires_at = now + timedelta(seconds=ttl_seconds)
    with transaction(conn):
        cur = conn.execute(
            "UPDATE resource_leases SET expires_at = ? WHERE resource_key = ? AND owner_id = ?",
            (_iso(expires_at), resource_key_value, owner_id),
        )
    return cur.rowcount == 1


def release_resource_lease(
    conn: sqlite3.Connection, resource_key_value: str, owner_id: str
) -> None:
    with transaction(conn):
        conn.execute(
            "DELETE FROM resource_leases WHERE resource_key = ? AND owner_id = ?",
            (resource_key_value, owner_id),
        )


@contextmanager
def acquire_resource_lease(
    conn: sqlite3.Connection,
    kind: ResourceKind,
    *,
    key: str | None = None,
    owner_id: str,
    ttl_seconds: float,
    job_id: str | None = None,
    wait: bool = True,
    timeout: float | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> Iterator[str]:
    """`resource_leases` を取得するコンテキストマネージャ。

    既定(`wait=True`)は取得できるまで待つ(§10.2 の「直列化する」という
    設計文言に基づく契約決定)。`wait=False` は1回だけ試みて、取得できなければ
    即座に `AppError(ErrorCode.CONFLICT, retryable=True)` を送出する(既存の
    同期ツールの busy/`CONCURRENT_RENDER` 系の即時応答と互換な経路)。
    `timeout` を指定すると `wait=True` でも無限には待たず、超過時に同じ
    `CONFLICT` を送出する。

    取得できた場合、`with` を抜けるときに(例外の有無によらず)必ず解放する。
    解放は TTL 経過を待たずに行うため、待機中の別プロセスはリース期限一杯まで
    待たされずに済む。
    """
    resource_key_value = build_resource_key(kind, key)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        acquired = _try_take_resource_lease(
            conn, resource_key_value, owner_id, ttl_seconds=ttl_seconds, job_id=job_id
        )
        if acquired:
            break
        if not wait or (deadline is not None and time.monotonic() >= deadline):
            raise AppError(
                code=ErrorCode.CONFLICT,
                message=f"リソース '{resource_key_value}' は他のプロセスが使用中です。",
                hint="しばらく待ってから再試行してください。",
                retryable=True,
                exit_code=ExitCode.CONFLICT,
            )
        time.sleep(poll_interval)

    try:
        yield resource_key_value
    finally:
        release_resource_lease(conn, resource_key_value, owner_id)


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "acquire_resource_lease",
    "current_worker_lease",
    "has_live_worker",
    "release_resource_lease",
    "release_worker_lease",
    "renew_resource_lease",
    "renew_worker_lease",
    "try_acquire_worker_lease",
]
