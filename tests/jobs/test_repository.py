"""`JobRepository` の単一プロセス内での振る舞い(投入・claim・復旧・再試行)。

複数プロセスをまたぐ排他そのものは `test_multiprocess_leases.py` が担当する。
ここでは同一接続内でのロジック(状態遷移・クラッシュ復旧の判定条件)を検証する。
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import JobState, ResourceKind
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema
from abist_kb.infrastructure.jobs.repository import JobRepository


@pytest.fixture
def repo(tmp_root: Path) -> JobRepository:
    conn = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(conn)
    return JobRepository(conn)


def test_submit_creates_queued_job(repo: JobRepository) -> None:
    job = repo.submit("noop", {"a": 1})
    assert job.state is JobState.QUEUED
    assert job.params == {"a": 1}
    assert job.retry_of is None


def test_claim_moves_job_to_running_and_sets_owner(repo: JobRepository) -> None:
    job = repo.submit("noop")
    claimed = repo.claim("owner-1", ttl_seconds=5.0)
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.state is JobState.RUNNING
    assert claimed.owner_id == "owner-1"
    assert claimed.heartbeat_at is not None
    assert claimed.lease_expires_at is not None


def test_claim_returns_none_when_queue_is_empty(repo: JobRepository) -> None:
    assert repo.claim("owner-1", ttl_seconds=5.0) is None


# -- resource_key の配線(コードレビュー Important 3 の回帰テスト) -------------


def test_claim_without_resource_for_kind_leaves_resource_key_null(repo: JobRepository) -> None:
    """`resource_for_kind` を渡さない既存呼び出しは、以前と同じく NULL のままであること。"""
    repo.submit("noop")
    claimed = repo.claim("owner-1", ttl_seconds=5.0)
    assert claimed is not None
    assert claimed.resource_key is None


def test_claim_populates_resource_key_from_resource_for_kind(repo: JobRepository) -> None:
    """claim したジョブの `kind` に対応するリソース要求があれば `resource_key` を書き込む。

    以前はどちらの本番呼び出し元(`JobService.run_inline`/`WorkerSupervisor`)も
    `resource_key` を渡していなかったため、この列が常に NULL だった
    (コードレビュー Important 3)。キュー経由の実行では claim するまでどの
    ジョブ(=どの kind)が選ばれるか分からないため、`resource_for_kind` という
    マッピングを渡し、claim 時に SELECT した `kind` から解決する設計にした。
    """
    repo.submit("sync")
    claimed = repo.claim(
        "owner-1",
        ttl_seconds=5.0,
        resource_for_kind={"sync": (ResourceKind.DOCS_WRITE, None)},
    )
    assert claimed is not None
    assert claimed.resource_key == "docs-write"


def test_claim_leaves_resource_key_null_for_kind_without_requirement(repo: JobRepository) -> None:
    repo.submit("noop")
    claimed = repo.claim(
        "owner-1",
        ttl_seconds=5.0,
        resource_for_kind={"sync": (ResourceKind.DOCS_WRITE, None)},
    )
    assert claimed is not None
    assert claimed.resource_key is None


def test_claim_skips_cancelled_queued_jobs(repo: JobRepository) -> None:
    job = repo.submit("noop")
    repo.request_cancel(job.id)
    assert repo.get(job.id).state is JobState.CANCELLED
    assert repo.claim("owner-1", ttl_seconds=5.0) is None


def test_finish_clears_ownership_fields(repo: JobRepository) -> None:
    job = repo.submit("noop")
    repo.claim("owner-1", ttl_seconds=5.0)
    repo.finish(job.id, state=JobState.SUCCEEDED, result={"ok": True})
    finished = repo.get(job.id)
    assert finished.state is JobState.SUCCEEDED
    assert finished.result == {"ok": True}
    assert finished.owner_id is None
    assert finished.lease_expires_at is None


def test_renew_heartbeat_extends_lease_only_for_current_owner(repo: JobRepository) -> None:
    job = repo.submit("noop")
    repo.claim("owner-1", ttl_seconds=5.0)
    assert repo.renew_heartbeat(job.id, "owner-1", ttl_seconds=5.0) is True
    assert repo.renew_heartbeat(job.id, "owner-2", ttl_seconds=5.0) is False


def test_request_cancel_on_queued_job_is_immediate(repo: JobRepository) -> None:
    job = repo.submit("noop")
    repo.request_cancel(job.id)
    cancelled = repo.get(job.id)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.finished_at is not None


def test_request_cancel_on_running_job_only_sets_flag(repo: JobRepository) -> None:
    job = repo.submit("noop")
    repo.claim("owner-1", ttl_seconds=5.0)
    repo.request_cancel(job.id)
    running = repo.get(job.id)
    assert running.state is JobState.RUNNING
    assert running.cancel_requested is True


def test_request_cancel_on_terminal_job_raises_conflict(repo: JobRepository) -> None:
    job = repo.submit("noop")
    repo.claim("owner-1", ttl_seconds=5.0)
    repo.finish(job.id, state=JobState.SUCCEEDED)
    with pytest.raises(AppError) as excinfo:
        repo.request_cancel(job.id)
    assert excinfo.value.code == ErrorCode.CONFLICT


def test_retry_requires_failed_or_interrupted_state(repo: JobRepository) -> None:
    job = repo.submit("noop")
    with pytest.raises(AppError) as excinfo:
        repo.retry(job.id)
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


def test_retry_creates_new_queued_job_referencing_original(repo: JobRepository) -> None:
    job = repo.submit("noop", {"x": 1})
    repo.claim("owner-1", ttl_seconds=5.0)
    repo.finish(job.id, state=JobState.FAILED, error={"code": "FAILURE", "message": "boom"})
    retried = repo.retry(job.id)
    assert retried.id != job.id
    assert retried.retry_of == job.id
    assert retried.state is JobState.QUEUED
    assert retried.params == {"x": 1}


def test_retry_missing_job_raises_not_found(repo: JobRepository) -> None:
    with pytest.raises(AppError) as excinfo:
        repo.retry("does-not-exist")
    assert excinfo.value.code == ErrorCode.NOT_FOUND


# -- 異常終了からの復旧(設計書 §10.3, brief Step 5) --------------------------


def _make_running_job_with_expired_lease(
    conn, *, resource_key: str | None, resource_lease_expired: bool | None
) -> str:
    """`running` かつ heartbeat 期限切れのジョブ行を直接作る(復旧判定の単体検証用)。"""
    now = datetime.now(UTC)
    past = now - timedelta(minutes=1)
    job_id = "job-1"
    conn.execute(
        "INSERT INTO jobs (id, kind, state, params, cancel_requested, created_at, "
        "owner_id, resource_key, heartbeat_at, lease_expires_at, started_at) "
        "VALUES (?, 'noop', 'running', '{}', 0, ?, 'dead-owner', ?, ?, ?, ?)",
        (
            job_id,
            past.isoformat(),
            resource_key,
            past.isoformat(),
            past.isoformat(),
            past.isoformat(),
        ),
    )
    if resource_key is not None and resource_lease_expired is not None:
        lease_expires = (past if resource_lease_expired else now + timedelta(minutes=5)).isoformat()
        conn.execute(
            "INSERT INTO resource_leases (resource_key, owner_id, job_id, expires_at) "
            "VALUES (?, 'dead-owner', ?, ?)",
            (resource_key, job_id, lease_expires),
        )
    conn.commit()
    return job_id


def test_recover_interrupted_moves_job_when_heartbeat_and_resource_lease_both_expired(
    tmp_root: Path,
) -> None:
    conn = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(conn)
    job_id = _make_running_job_with_expired_lease(
        conn, resource_key="docs-write", resource_lease_expired=True
    )
    repo = JobRepository(conn)
    recovered = repo.recover_interrupted()
    assert recovered == [job_id]
    job = repo.get(job_id)
    assert job.state is JobState.INTERRUPTED
    assert job.error is not None
    assert job.error["code"] == "INTERRUPTED"


def test_recover_interrupted_moves_job_without_resource_key(tmp_root: Path) -> None:
    conn = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(conn)
    job_id = _make_running_job_with_expired_lease(
        conn, resource_key=None, resource_lease_expired=None
    )
    repo = JobRepository(conn)
    assert repo.recover_interrupted() == [job_id]


def test_recover_interrupted_leaves_job_when_resource_lease_still_valid(tmp_root: Path) -> None:
    """heartbeat は切れていても resource lease がまだ有効なら中断しない(両方の期限切れが条件)。"""
    conn = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(conn)
    job_id = _make_running_job_with_expired_lease(
        conn, resource_key="docs-write", resource_lease_expired=False
    )
    repo = JobRepository(conn)
    assert repo.recover_interrupted() == []
    assert repo.get(job_id).state is JobState.RUNNING


def test_recover_interrupted_leaves_job_with_live_heartbeat(tmp_root: Path) -> None:
    conn = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(conn)
    repo = JobRepository(conn)
    job = repo.submit("noop")
    repo.claim("owner-1", ttl_seconds=30.0)
    assert repo.recover_interrupted() == []
    assert repo.get(job.id).state is JobState.RUNNING


def test_recover_interrupted_dual_check_via_real_claim_and_lease_both_expired(
    tmp_root: Path,
) -> None:
    """`_make_running_job_with_expired_lease` のような手作り行ではなく、実際の
    `claim(resource_for_kind=...)` + `acquire_resource_lease` を経由して
    「heartbeat と resource lease が両方期限切れ」の状態を再現する
    (コードレビュー Important 3: 手作り行だけのテストは、本番のどちらの
    呼び出し元も `resource_key` を渡さないという不具合を見逃していた)。
    """
    conn = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(conn)
    repo = JobRepository(conn)
    repo.submit("sync")
    resource_for_kind = {"sync": (ResourceKind.DOCS_WRITE, None)}
    claimed = repo.claim("dead-owner", ttl_seconds=0.1, resource_for_kind=resource_for_kind)
    assert claimed is not None
    assert claimed.resource_key == "docs-write"

    # 本物の resource lease を短い TTL で取得する。`__exit__`(解放)を呼ばずに
    # 抜けることで、「解放せずにクラッシュしたプロセス」を模す
    # (実運用でも異常終了時は解放されない)。
    lease_cm = leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="dead-owner", ttl_seconds=0.1, job_id=claimed.id
    )
    lease_cm.__enter__()

    time.sleep(0.2)  # heartbeat・resource lease ともに TTL(0.1秒)を超えて期限切れになる

    recovered = repo.recover_interrupted()
    assert recovered == [claimed.id]
    assert repo.get(claimed.id).state is JobState.INTERRUPTED


def test_recover_interrupted_dual_check_via_real_claim_and_lease_still_valid(
    tmp_root: Path,
) -> None:
    """heartbeat は切れていても resource lease がまだ有効なら中断しないことを、
    同じく実際の `claim`/`acquire_resource_lease` 経由で確認する。
    """
    conn = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(conn)
    repo = JobRepository(conn)
    repo.submit("sync")
    resource_for_kind = {"sync": (ResourceKind.DOCS_WRITE, None)}
    # ジョブの heartbeat/lease は短く(すぐ切れる)、resource lease は長く保つ。
    claimed = repo.claim("dead-owner", ttl_seconds=0.1, resource_for_kind=resource_for_kind)
    assert claimed is not None

    lease_cm = leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="dead-owner", ttl_seconds=30.0, job_id=claimed.id
    )
    lease_cm.__enter__()

    time.sleep(0.2)  # ジョブの heartbeat/lease だけが期限切れになる

    assert repo.recover_interrupted() == []
    assert repo.get(claimed.id).state is JobState.RUNNING


def test_recover_interrupted_does_not_auto_retry(tmp_root: Path) -> None:
    """§10.3: 自動再投入はしない(interrupted のままで、新しい queued 行は作られない)。"""
    conn = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(conn)
    job_id = _make_running_job_with_expired_lease(
        conn, resource_key=None, resource_lease_expired=None
    )
    repo = JobRepository(conn)
    repo.recover_interrupted()
    all_jobs = repo.list()
    assert len(all_jobs) == 1
    assert all_jobs[0].id == job_id
    assert all_jobs[0].state is JobState.INTERRUPTED


def test_list_filters_by_state(repo: JobRepository) -> None:
    a = repo.submit("noop")
    b = repo.submit("noop")
    repo.claim("owner-1", ttl_seconds=5.0)
    queued = repo.list(state=JobState.QUEUED)
    running = repo.list(state=JobState.RUNNING)
    assert {j.id for j in queued} | {j.id for j in running} == {a.id, b.id}
    assert len(running) == 1


def test_row_json_round_trip_preserves_params(tmp_root: Path) -> None:
    conn = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(conn)
    repo = JobRepository(conn)
    job = repo.submit("noop", {"nested": {"a": [1, 2, 3]}, "text": "日本語"})
    row = conn.execute("SELECT params FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert json.loads(row["params"]) == {"nested": {"a": [1, 2, 3]}, "text": "日本語"}
