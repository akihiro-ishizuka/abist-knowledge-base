"""`WorkerSupervisor` の単一プロセス内での振る舞い。

リーダー選出そのもの(プロセス境界を越えた排他)は
`test_multiprocess_leases.py` が担当する。ここでは「リーダーになった Supervisor
だけがキューを消費し、handler の成功/失敗を正しくジョブへ反映する」ことを検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.infrastructure.jobs.supervisor import JobRunContext, WorkerSupervisor


@pytest.fixture
def conn(tmp_root: Path):
    c = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(c)
    yield c
    c.close()


def test_tick_becomes_leader_and_claims_job(conn) -> None:
    repo = JobRepository(conn)
    job = repo.submit("noop")
    calls: list[str] = []

    def handler(run: JobRunContext) -> None:
        calls.append(run.job.id)
        run.emit(phase="done", current=1, total=1, message="ok")

    supervisor = WorkerSupervisor(conn, owner_id="A", handlers={"noop": handler})
    did_work = supervisor.tick()
    assert did_work is True
    assert supervisor.is_leader is True
    assert calls == [job.id]
    assert repo.get(job.id).state is JobState.SUCCEEDED


def test_tick_does_nothing_when_not_leader(conn) -> None:
    from abist_kb.infrastructure.jobs import leases

    leases.try_acquire_worker_lease(conn, "other-owner", ttl_seconds=30.0)
    supervisor = WorkerSupervisor(conn, owner_id="A")
    assert supervisor.tick() is False
    assert supervisor.is_leader is False


def test_tick_marks_job_failed_on_app_error(conn) -> None:
    repo = JobRepository(conn)
    job = repo.submit("noop")

    def handler(run: JobRunContext) -> None:
        raise AppError(code=ErrorCode.FAILURE, message="ハンドラ失敗")

    supervisor = WorkerSupervisor(conn, owner_id="A", handlers={"noop": handler})
    supervisor.tick()
    finished = repo.get(job.id)
    assert finished.state is JobState.FAILED
    assert finished.error["code"] == "FAILURE"


def test_tick_marks_job_failed_on_unexpected_exception_without_crashing(conn) -> None:
    """ハンドラのバグ(想定外の例外)でもワーカー自体は例外を伝播させない。"""
    repo = JobRepository(conn)
    job = repo.submit("noop")

    def handler(run: JobRunContext) -> None:
        raise KeyError("bug")

    supervisor = WorkerSupervisor(conn, owner_id="A", handlers={"noop": handler})
    supervisor.tick()  # 例外を投げずに完了すること
    assert repo.get(job.id).state is JobState.FAILED


def test_tick_fails_unregistered_job_kind(conn) -> None:
    repo = JobRepository(conn)
    job = repo.submit("unknown-kind")
    supervisor = WorkerSupervisor(conn, owner_id="A", handlers={})
    supervisor.tick()
    finished = repo.get(job.id)
    assert finished.state is JobState.FAILED
    assert finished.error["code"] == "UNKNOWN_JOB_KIND"


def test_tick_recovers_interrupted_jobs_before_claiming_new_ones(conn) -> None:
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    conn.execute(
        "INSERT INTO jobs (id, kind, state, params, cancel_requested, created_at, "
        "owner_id, heartbeat_at, lease_expires_at, started_at) "
        "VALUES ('stuck', 'noop', 'running', '{}', 0, ?, 'dead', ?, ?, ?)",
        (past, past, past, past),
    )
    conn.commit()

    supervisor = WorkerSupervisor(conn, owner_id="A", handlers={})
    supervisor.tick()
    repo = JobRepository(conn)
    assert repo.get("stuck").state is JobState.INTERRUPTED


def test_emit_renews_job_heartbeat(conn) -> None:
    repo = JobRepository(conn)
    repo.submit("noop")

    observed_expiry: list[object] = []

    def handler(run: JobRunContext) -> None:
        run.emit(phase="p", current=1, total=2, message="half")
        observed_expiry.append(repo.get(run.job.id).lease_expires_at)

    supervisor = WorkerSupervisor(conn, owner_id="A", handlers={"noop": handler}, lease_ttl=100.0)
    supervisor.tick()
    assert observed_expiry[0] is not None
