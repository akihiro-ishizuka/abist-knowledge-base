"""`JobService`(設計書 §10): CLI/TUI/Web/MCP から共通で使うジョブ操作の窓口。

インライン実行(CLIの既定)と `--detach`(キュー投入のみ)の両方をここで扱う。
インライン実行も `resource_leases` を取得するため、他プロセスのキュージョブと
競合しない(§10.2)。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from typing import Any

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.job import Job, JobState, ProgressEvent, ResourceKind, Severity
from abist_kb.infrastructure.jobs import events as events_mod
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.infrastructure.jobs.supervisor import (
    WORKER_LEASE_TTL_SECONDS,
    JobHandler,
    JobRunContext,
)

ResourceRequirement = tuple[ResourceKind, str | None]
"""ジョブ種別が必要とするリソース(種別, 区画キー)。"""


@contextlib.contextmanager
def _no_resource_lease() -> Iterator[None]:
    yield None


class JobService:
    """`JobRepository`/リース/イベントバスをまとめ、ジョブのライフサイクルを扱う。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        owner_id: str,
        handlers: dict[str, JobHandler] | None = None,
        resource_for_kind: dict[str, ResourceRequirement] | None = None,
        lease_ttl_seconds: float = WORKER_LEASE_TTL_SECONDS,
        event_bus: events_mod.EventBus | None = None,
    ) -> None:
        self._conn = conn
        self._owner_id = owner_id
        self._handlers = dict(handlers or {})
        self._resource_for_kind = dict(resource_for_kind or {})
        self._lease_ttl = lease_ttl_seconds
        self._repo = JobRepository(conn)
        self._event_bus = event_bus or events_mod.EventBus()

    @property
    def events(self) -> events_mod.EventBus:
        return self._event_bus

    # -- 参照 -----------------------------------------------------------

    def get(self, job_id: str) -> Job:
        job = self._repo.get(job_id)
        if job is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"ジョブが見つかりません: {job_id}")
        return job

    def list(self, *, state: JobState | None = None) -> list[Job]:
        return self._repo.list(state=state)

    def history(self, job_id: str) -> list[ProgressEvent]:
        self.get(job_id)  # 存在確認(NOT_FOUND を先に出す)
        return events_mod.list_events(self._conn, job_id)

    # -- 操作 -----------------------------------------------------------

    def cancel(self, job_id: str) -> None:
        self._repo.request_cancel(job_id)

    def retry(self, job_id: str) -> Job:
        """`failed`/`interrupted` のジョブだけを再投入する。

        利用者確認(§10.3: 自動再投入はしない)は呼び出し元(CLI)の責務。
        """
        return self._repo.retry(job_id)

    def submit(self, kind: str, params: dict[str, Any] | None = None) -> Job:
        return self._repo.submit(kind, params)

    def detach(self, kind: str, params: dict[str, Any] | None = None) -> Job:
        """`--detach`: キューへ投入するだけで実行はしない。

        有効な worker heartbeat が無ければ `WORKER_UNAVAILABLE` で失敗する
        (§10.1: 実行されないジョブを放置しない)。
        """
        if not leases.has_live_worker(self._conn):
            raise AppError(
                code=ErrorCode.WORKER_UNAVAILABLE,
                message="有効なワーカーが起動していないため、ジョブをキューに投入できません。",
                hint="`worker run` を起動するか、--detach を外してインライン実行してください。",
                exit_code=ExitCode.CONFLICT,
            )
        return self.submit(kind, params)

    def run_inline(self, kind: str, params: dict[str, Any] | None = None) -> Job:
        """CLI 既定の同期実行: このプロセス内でジョブを最後まで実行する。

        インライン実行も他プロセスのキュージョブと同じ `resource_leases` を
        取得するため、バッチ・sync・Webの同時実行と衝突しない(§10.2)。
        """
        handler = self._handlers.get(kind)
        if handler is None:
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"未登録のジョブ種別です: {kind}",
                exit_code=ExitCode.INVALID_INPUT,
            )

        job = self.submit(kind, params)
        claimed = self._repo.claim(self._owner_id, ttl_seconds=self._lease_ttl)
        if claimed is None or claimed.id != job.id:
            # 投入直後にこのプロセス自身が最初の claim 者になれなかった場合
            # (理論上、他プロセスのワーカーに先を越された場合のみ起こりうる)。
            raise AppError(
                code=ErrorCode.CONFLICT,
                message="ジョブの投入直後に他のワーカーへ横取りされました。",
                hint="`jobs show` で実行状況を確認してください。",
                retryable=True,
            )

        resource = self._resource_for_kind.get(kind)
        lease_cm = (
            leases.acquire_resource_lease(
                self._conn,
                resource[0],
                key=resource[1],
                owner_id=self._owner_id,
                ttl_seconds=self._lease_ttl,
                job_id=job.id,
            )
            if resource is not None
            else _no_resource_lease()
        )

        def emit(
            *,
            phase: str,
            current: int | None = None,
            total: int | None = None,
            message: str = "",
            severity: Severity = Severity.INFO,
            item: str | None = None,
        ) -> None:
            event = ProgressEvent(
                job_id=job.id,
                phase=phase,
                current=current,
                total=total,
                message=message,
                severity=severity,
                item=item,
            )
            events_mod.append_event(self._conn, event)
            self._event_bus.publish(event)
            self._repo.renew_heartbeat(job.id, self._owner_id, ttl_seconds=self._lease_ttl)

        try:
            with lease_cm:
                handler(JobRunContext(job=claimed, emit=emit))
        except AppError as exc:
            self._repo.finish(job.id, state=JobState.FAILED, error=exc.to_dict())
            raise
        except Exception as exc:
            self._repo.finish(
                job.id, state=JobState.FAILED, error={"code": "FAILURE", "message": str(exc)}
            )
            raise
        else:
            self._repo.finish(job.id, state=JobState.SUCCEEDED)
        return self.get(job.id)


__all__ = ["JobService", "ResourceRequirement"]
