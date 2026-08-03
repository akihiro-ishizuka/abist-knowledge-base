"""`JobService`: インライン実行・`--detach` 契約・キャンセル・再試行。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.job_service import JobService
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import Job, JobState, ResourceKind
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema
from abist_kb.infrastructure.jobs.supervisor import JobRunContext


@pytest.fixture
def conn(tmp_root: Path):
    c = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(c)
    yield c
    c.close()


def _noop(run: JobRunContext) -> None:
    run.emit(phase="noop", current=1, total=1, message="done")


def test_run_inline_executes_synchronously_and_returns_succeeded_job(conn) -> None:
    service = JobService(conn, owner_id="A", handlers={"noop": _noop})
    job = service.run_inline("noop", {"x": 1})
    assert job.state is JobState.SUCCEEDED
    assert job.params == {"x": 1}


def test_run_inline_records_progress_events(conn) -> None:
    service = JobService(conn, owner_id="A", handlers={"noop": _noop})
    job = service.run_inline("noop")
    history = service.history(job.id)
    assert len(history) == 1
    assert history[0].message == "done"


def test_run_inline_publishes_to_event_bus(conn) -> None:
    service = JobService(conn, owner_id="A", handlers={"noop": _noop})
    received = []
    service.events.subscribe(received.append)
    service.run_inline("noop")
    assert len(received) == 1


def test_run_inline_unregistered_kind_raises_invalid_input(conn) -> None:
    service = JobService(conn, owner_id="A", handlers={})
    with pytest.raises(AppError) as excinfo:
        service.run_inline("does-not-exist")
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


def test_run_inline_marks_job_failed_and_reraises_on_handler_error(conn) -> None:
    def failing(run: JobRunContext) -> None:
        raise AppError(code=ErrorCode.FAILURE, message="boom")

    service = JobService(conn, owner_id="A", handlers={"noop": failing})
    with pytest.raises(AppError):
        service.run_inline("noop")
    jobs = service.list(state=JobState.FAILED)
    assert len(jobs) == 1


def test_run_inline_acquires_and_releases_resource_lease(conn) -> None:
    """インライン実行も resource_leases を取得し、他プロセスのキュージョブと衝突しない(§10.2)。"""
    observed_conflict = {"seen": False}

    def handler(run: JobRunContext) -> None:
        # ハンドラ実行中、同じリソースは他者から取得できないはず。
        try:
            with leases.acquire_resource_lease(
                conn, ResourceKind.DOCS_WRITE, owner_id="someone-else", ttl_seconds=5.0, wait=False
            ):
                pass
        except AppError as exc:
            observed_conflict["seen"] = exc.code == ErrorCode.CONFLICT

    service = JobService(
        conn,
        owner_id="A",
        handlers={"sync": handler},
        resource_for_kind={"sync": (ResourceKind.DOCS_WRITE, None)},
    )
    service.run_inline("sync")
    assert observed_conflict["seen"] is True

    # ハンドラ終了後は解放されている。
    with leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="someone-else", ttl_seconds=5.0, wait=False
    ):
        pass


def test_detach_raises_worker_unavailable_without_live_heartbeat(conn) -> None:
    service = JobService(conn, owner_id="A", handlers={"noop": _noop})
    with pytest.raises(AppError) as excinfo:
        service.detach("noop")
    assert excinfo.value.code == ErrorCode.WORKER_UNAVAILABLE


def test_detach_enqueues_without_running_when_worker_is_live(conn) -> None:
    leases.try_acquire_worker_lease(conn, "some-worker", ttl_seconds=30.0)
    calls: list[str] = []

    def handler(run: JobRunContext) -> None:
        calls.append(run.job.id)

    service = JobService(conn, owner_id="A", handlers={"noop": handler})
    job = service.detach("noop")
    assert job.state is JobState.QUEUED
    assert calls == []  # インラインでは実行されていない


def test_cancel_delegates_to_repository(conn) -> None:
    service = JobService(conn, owner_id="A", handlers={"noop": _noop})
    job = service.submit("noop")
    service.cancel(job.id)
    assert service.get(job.id).state is JobState.CANCELLED


def test_retry_requires_retryable_state(conn) -> None:
    service = JobService(conn, owner_id="A", handlers={"noop": _noop})
    job = service.submit("noop")
    with pytest.raises(AppError) as excinfo:
        service.retry(job.id)
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


def test_get_missing_job_raises_not_found(conn) -> None:
    service = JobService(conn, owner_id="A")
    with pytest.raises(AppError) as excinfo:
        service.get("nope")
    assert excinfo.value.code == ErrorCode.NOT_FOUND


def test_list_returns_all_jobs_by_default(conn) -> None:
    service = JobService(conn, owner_id="A", handlers={"noop": _noop})
    service.submit("noop")
    service.submit("noop")
    assert len(service.list()) == 2
    assert all(isinstance(job, Job) for job in service.list())
