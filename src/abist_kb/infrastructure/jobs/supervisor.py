"""`WorkerSupervisor`(設計書 §10.1, §10.3)。

Web、デスクトップ、TUI、MCPの各長時間稼働エントリポイントは起動時にこれを
開始し、リーダー選出を試みる。`<cli> worker run` も同じ Supervisor を起動する。
リーダーだけがキューを消費する。heartbeat 5秒、lease 15秒(既定値。テストは
実行時間短縮のため上書きする)。
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from abist_kb.domain.errors import AppError
from abist_kb.domain.job import Job, JobState, ProgressEvent, Severity
from abist_kb.infrastructure.jobs import events as events_mod
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.repository import JobRepository

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 5.0
"""設計書 §10.1 が定める既定の heartbeat 間隔。"""

WORKER_LEASE_TTL_SECONDS = 15.0
"""設計書 §10.1 が定める既定の lease 期限。"""


EmitFn = Callable[..., None]


@dataclass(slots=True)
class JobRunContext:
    """ジョブハンドラへ渡す実行コンテキスト。"""

    job: Job
    emit: EmitFn


JobHandler = Callable[[JobRunContext], None]
"""ジョブ種別ごとのハンドラ。失敗は例外(`AppError` 推奨)で表す。"""


class WorkerSupervisor:
    """リーダー選出とキュー消費を担う。1プロセスにつき1インスタンス。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        owner_id: str | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        lease_ttl: float = WORKER_LEASE_TTL_SECONDS,
        handlers: dict[str, JobHandler] | None = None,
        event_bus: events_mod.EventBus | None = None,
    ) -> None:
        self.owner_id = owner_id or str(uuid.uuid4())
        self._conn = conn
        self._heartbeat_interval = heartbeat_interval
        self._lease_ttl = lease_ttl
        self._handlers = dict(handlers or {})
        self._event_bus = event_bus or events_mod.EventBus()
        self._repo = JobRepository(conn)
        self._is_leader = False
        self._stop_requested = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def events(self) -> events_mod.EventBus:
        return self._event_bus

    def register(self, kind: str, handler: JobHandler) -> None:
        self._handlers[kind] = handler

    def request_stop(self) -> None:
        self._stop_requested = True

    def _become_or_stay_leader(self) -> None:
        if self._is_leader:
            self._is_leader = leases.renew_worker_lease(
                self._conn, self.owner_id, ttl_seconds=self._lease_ttl
            )
        if not self._is_leader:
            self._is_leader = leases.try_acquire_worker_lease(
                self._conn, self.owner_id, ttl_seconds=self._lease_ttl
            )

    def tick(self) -> bool:
        """1周分の処理を行う。何らかの作業をした場合は `True` を返す。"""
        did_work = False
        self._become_or_stay_leader()
        if not self._is_leader:
            return did_work

        recovered = self._repo.recover_interrupted()
        if recovered:
            did_work = True
            logger.warning("interrupted_jobs_recovered", extra={"job_ids": recovered})

        job = self._repo.claim(self.owner_id, ttl_seconds=self._lease_ttl)
        if job is None:
            return did_work

        did_work = True
        self._run_job(job)
        return did_work

    def _run_job(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._repo.finish(
                job.id,
                state=JobState.FAILED,
                error={
                    "code": "UNKNOWN_JOB_KIND",
                    "message": f"未登録のジョブ種別です: {job.kind}",
                },
            )
            return

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
            # 進捗報告のたびに heartbeat も更新する。長時間ハンドラが定期的に
            # emit する限り、実行中ジョブの lease は自動的に生き続ける。
            self._repo.renew_heartbeat(job.id, self.owner_id, ttl_seconds=self._lease_ttl)

        try:
            handler(JobRunContext(job=job, emit=emit))
        except AppError as exc:
            self._repo.finish(job.id, state=JobState.FAILED, error=exc.to_dict())
        except Exception as exc:  # ハンドラのバグでもワーカー自体は落とさない
            logger.exception("job_handler_failed", extra={"job_id": job.id, "kind": job.kind})
            self._repo.finish(
                job.id,
                state=JobState.FAILED,
                error={"code": "FAILURE", "message": str(exc)},
            )
        else:
            self._repo.finish(job.id, state=JobState.SUCCEEDED)

    def run_forever(self, *, poll_interval: float = 1.0) -> None:
        """`tick()` をループする(`<cli> worker run` の実体)。"""
        self._stop_requested = False
        while not self._stop_requested:
            try:
                did_work = self.tick()
            except AppError:
                logger.warning("worker_tick_conflict", exc_info=True)
                did_work = False
            if not did_work:
                time.sleep(poll_interval)


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "WORKER_LEASE_TTL_SECONDS",
    "JobHandler",
    "JobRunContext",
    "WorkerSupervisor",
]
