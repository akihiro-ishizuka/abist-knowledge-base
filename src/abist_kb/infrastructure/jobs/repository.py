"""`jobs` テーブルの永続化(設計書 §9.2, §10)。

`claim`/`recover_interrupted` は `BEGIN IMMEDIATE` の下で SELECT→UPDATE を行う。
これにより「同一ジョブを複数プロセスが claim しようとして1つだけ成功する」
という排他が、行の再確認(`rowcount`)ではなくトランザクション分離そのもので
保証される(§10.2: lease取得とジョブclaimは `BEGIN IMMEDIATE` トランザクションで
行い、プロセス内 `asyncio.Lock` だけに依存しない)。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.job import RETRYABLE_STATES, Job, JobState
from abist_kb.infrastructure.db.connection import transaction


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _parse_json(value: str | None) -> dict[str, Any] | None:
    return None if value is None else json.loads(value)


def _dump_json(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        state=JobState(row["state"]),
        params=json.loads(row["params"]),
        created_at=_parse_dt(row["created_at"]),  # type: ignore[arg-type]
        result=_parse_json(row["result"]),
        error=_parse_json(row["error"]),
        progress=_parse_json(row["progress"]),
        cancel_requested=bool(row["cancel_requested"]),
        retry_of=row["retry_of"],
        owner_id=row["owner_id"],
        resource_key=row["resource_key"],
        heartbeat_at=_parse_dt(row["heartbeat_at"]),
        lease_expires_at=_parse_dt(row["lease_expires_at"]),
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt(row["finished_at"]),
    )


class JobRepository:
    """`jobs`/`job_events`/`resource_leases` を横断するジョブの読み書き。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- 参照 -----------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _row_to_job(row)

    def list(self, *, state: JobState | None = None) -> list[Job]:
        if state is None:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY created_at", (state.value,)
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    # -- 投入・claim ------------------------------------------------------

    def submit(
        self, kind: str, params: dict[str, Any] | None = None, *, retry_of: str | None = None
    ) -> Job:
        job_id = str(uuid4())
        now = _iso(datetime.now(UTC))
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO jobs "
                "(id, kind, state, params, cancel_requested, retry_of, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?, ?)",
                (job_id, kind, JobState.QUEUED.value, _dump_json(params or {}), retry_of, now),
            )
        job = self.get(job_id)
        assert job is not None  # 直前に挿入した行なので必ず存在する
        return job

    def claim(
        self, owner_id: str, *, ttl_seconds: float, resource_key: str | None = None
    ) -> Job | None:
        """キューの先頭を1件だけ claim する。他に取れる queued ジョブが無ければ `None`。

        `BEGIN IMMEDIATE` が書込ロックを即座に取るため、この関数内の
        SELECT→UPDATE は他プロセスの同時呼び出しに対して不可分に振る舞う
        (他プロセスは COMMIT/ROLLBACK までブロックされる)。
        """
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with transaction(self._conn):
            candidate = self._conn.execute(
                "SELECT id FROM jobs WHERE state = ? AND cancel_requested = 0 "
                "ORDER BY created_at LIMIT 1",
                (JobState.QUEUED.value,),
            ).fetchone()
            if candidate is None:
                return None
            job_id = candidate["id"]
            cur = self._conn.execute(
                "UPDATE jobs SET state = ?, owner_id = ?, resource_key = ?, "
                "heartbeat_at = ?, lease_expires_at = ?, started_at = ? "
                "WHERE id = ? AND state = ?",
                (
                    JobState.RUNNING.value,
                    owner_id,
                    resource_key,
                    _iso(now),
                    _iso(expires_at),
                    _iso(now),
                    job_id,
                    JobState.QUEUED.value,
                ),
            )
            if cur.rowcount == 0:
                # 同一トランザクション内では起こり得ないが、防御的に扱う。
                return None
        return self.get(job_id)

    def renew_heartbeat(self, job_id: str, owner_id: str, *, ttl_seconds: float) -> bool:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with transaction(self._conn):
            cur = self._conn.execute(
                "UPDATE jobs SET heartbeat_at = ?, lease_expires_at = ? "
                "WHERE id = ? AND owner_id = ? AND state = ?",
                (_iso(now), _iso(expires_at), job_id, owner_id, JobState.RUNNING.value),
            )
        return cur.rowcount == 1

    def update_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE jobs SET progress = ? WHERE id = ?",
                (_dump_json(progress), job_id),
            )

    def finish(
        self,
        job_id: str,
        *,
        state: JobState,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        now = _iso(datetime.now(UTC))
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE jobs SET state = ?, result = ?, error = ?, finished_at = ?, "
                "owner_id = NULL, resource_key = NULL, heartbeat_at = NULL, "
                "lease_expires_at = NULL WHERE id = ?",
                (state.value, _dump_json(result), _dump_json(error), now, job_id),
            )

    # -- キャンセル・再試行 -------------------------------------------------

    def request_cancel(self, job_id: str) -> None:
        """キャンセルを要求する。`queued` は即座に `cancelled`、`running` は
        `cancel_requested` フラグのみ立て、実行中のハンドラが自発的に検知して
        終了するのを待つ(強制終了はしない)。
        """
        now = _iso(datetime.now(UTC))
        with transaction(self._conn):
            cur = self._conn.execute(
                "UPDATE jobs SET cancel_requested = 1 WHERE id = ? AND state IN (?, ?)",
                (job_id, JobState.QUEUED.value, JobState.RUNNING.value),
            )
            if cur.rowcount == 0:
                raise AppError(
                    code=ErrorCode.CONFLICT,
                    message="ジョブは既に終了しているためキャンセルできません。",
                    details={"job_id": job_id},
                )
            self._conn.execute(
                "UPDATE jobs SET state = ?, finished_at = ? WHERE id = ? AND state = ?",
                (JobState.CANCELLED.value, now, job_id, JobState.QUEUED.value),
            )

    def retry(self, job_id: str) -> Job:
        """安全に再試行できる状態(`failed`/`interrupted`)のジョブだけを再投入する。

        利用者確認は呼び出し元(CLI)の責務(§10.3: 自動再投入はしない)。
        """
        original = self.get(job_id)
        if original is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"ジョブが見つかりません: {job_id}")
        if original.state not in RETRYABLE_STATES:
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"状態 '{original.state}' のジョブは再試行できません。",
                details={"job_id": job_id, "state": str(original.state)},
                exit_code=ExitCode.INVALID_INPUT,
            )
        return self.submit(original.kind, original.params, retry_of=job_id)

    # -- 異常終了からの復旧 -------------------------------------------------

    def recover_interrupted(self) -> list[str]:
        """worker heartbeat と resource lease が両方期限切れの `running` ジョブを
        `interrupted` へ遷移させる(§10.3)。自動再投入はしない。
        """
        now = datetime.now(UTC)
        recovered: list[str] = []
        with transaction(self._conn):
            rows = self._conn.execute(
                "SELECT id, resource_key, lease_expires_at FROM jobs WHERE state = ?",
                (JobState.RUNNING.value,),
            ).fetchall()
            for row in rows:
                lease_expires_at = row["lease_expires_at"]
                if lease_expires_at is None:
                    continue
                if _parse_dt(lease_expires_at) >= now:  # type: ignore[operator]
                    continue  # worker heartbeat がまだ有効
                if row["resource_key"] is not None:
                    resource_row = self._conn.execute(
                        "SELECT expires_at FROM resource_leases WHERE resource_key = ?",
                        (row["resource_key"],),
                    ).fetchone()
                    if resource_row is not None and _parse_dt(resource_row["expires_at"]) >= now:  # type: ignore[operator]
                        continue  # resource lease がまだ有効(両方の期限切れではない)
                self._conn.execute(
                    "UPDATE jobs SET state = ?, finished_at = ?, error = ?, "
                    "owner_id = NULL, resource_key = NULL, heartbeat_at = NULL, "
                    "lease_expires_at = NULL WHERE id = ?",
                    (
                        JobState.INTERRUPTED.value,
                        _iso(now),
                        _dump_json(
                            {
                                "code": "INTERRUPTED",
                                "message": (
                                    "ワーカーの heartbeat と resource lease が"
                                    "共に期限切れとなったため中断しました。"
                                ),
                            }
                        ),
                        row["id"],
                    ),
                )
                recovered.append(row["id"])
        return recovered


__all__ = ["JobRepository"]
