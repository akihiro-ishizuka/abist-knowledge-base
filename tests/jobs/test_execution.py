"""`infrastructure.jobs.execution.run_job`: 共有実行経路の単体挙動。

複数プロセスをまたぐ排他そのものは `test_multiprocess_leases.py` が担当する。
ここでは同一プロセス内で完結する契約(resource lease の自動更新・更新失敗の
表面化・`reraise` の分岐)を高速に検証する。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import JobState, ResourceKind
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs import events as events_mod
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema
from abist_kb.infrastructure.jobs.execution import run_job
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.infrastructure.jobs.supervisor import JobRunContext


@pytest.fixture
def conn(tmp_root: Path):
    c = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(c)
    yield c
    c.close()


def test_run_job_keeps_resource_lease_alive_past_ttl_without_emit(conn) -> None:
    """Critical 1: `emit()` を一度も呼ばない長時間ハンドラでも resource lease が
    TTL を超えて自動更新され続けること(同一プロセス内、別接続で実測)。
    """
    repo = JobRepository(conn)
    repo.submit("sync")
    resource_for_kind = {"sync": (ResourceKind.DOCS_WRITE, None)}
    claimed = repo.claim("A", ttl_seconds=0.3, resource_for_kind=resource_for_kind)
    assert claimed is not None

    observed_expiry_before_sleep: list[str] = []

    def handler(run: JobRunContext) -> None:
        row = conn.execute(
            "SELECT expires_at FROM resource_leases WHERE resource_key = 'docs-write'"
        ).fetchone()
        observed_expiry_before_sleep.append(row["expires_at"])
        time.sleep(0.6)  # TTL(0.3秒)の2倍以上、emit() は一度も呼ばない

    finished = run_job(
        conn,
        repo,
        events_mod.EventBus(),
        claimed,
        handler,
        owner_id="A",
        ttl_seconds=0.3,
        resource=(ResourceKind.DOCS_WRITE, None),
    )
    assert finished.state is JobState.SUCCEEDED

    row = conn.execute(
        "SELECT owner_id FROM resource_leases WHERE resource_key = 'docs-write'"
    ).fetchone()
    # ハンドラ終了後は解放されているはず(TTL切れで別オーナーに奪われたのではなく
    # 正常に解放された結果として存在しない)。
    assert row is None


def test_run_job_surfaces_conflict_when_resource_lease_is_stolen_mid_handler(conn) -> None:
    """Critical 1: リースが(TTL切れ等で)他者に奪われたら、ハンドラが正常に
    終わっても成功として記録せず `AppError(CONFLICT)` を送出すること
    (「リースを失ったジョブがそのまま成功したことにされる」事態を防ぐ)。
    """
    repo = JobRepository(conn)
    job = repo.submit("sync")
    resource_for_kind = {"sync": (ResourceKind.DOCS_WRITE, None)}
    claimed = repo.claim("A", ttl_seconds=30.0, resource_for_kind=resource_for_kind)
    assert claimed is not None

    def handler(run: JobRunContext) -> None:
        # 別接続から横から奪う(TTL切れ後に他プロセスが取得した状況を模する)。
        stealer = connect(conn.execute("PRAGMA database_list").fetchone()[2])
        try:
            leases.release_resource_lease(stealer, "docs-write", "A")
            with leases.acquire_resource_lease(
                stealer, ResourceKind.DOCS_WRITE, owner_id="thief", ttl_seconds=30.0, wait=False
            ):
                time.sleep(0.5)  # run_job のバックグラウンド更新スレッドに次の周期を跨がせる
        finally:
            stealer.close()

    with pytest.raises(AppError) as excinfo:
        run_job(
            conn,
            repo,
            events_mod.EventBus(),
            claimed,
            handler,
            owner_id="A",
            ttl_seconds=0.3,  # 更新間隔 0.1秒 程度になり、奪取をすぐ検知できる
            resource=(ResourceKind.DOCS_WRITE, None),
        )
    assert excinfo.value.code == ErrorCode.CONFLICT

    finished = repo.get(job.id)
    assert finished.state is JobState.FAILED


def test_run_job_does_not_reraise_when_reraise_is_false(conn) -> None:
    """`WorkerSupervisor` 向けの `reraise=False`: ハンドラのバグでも例外を伝播させない。"""
    repo = JobRepository(conn)
    job = repo.submit("noop")
    claimed = repo.claim("A", ttl_seconds=5.0)
    assert claimed is not None

    def handler(run: JobRunContext) -> None:
        raise RuntimeError("bug")

    finished = run_job(
        conn,
        repo,
        events_mod.EventBus(),
        claimed,
        handler,
        owner_id="A",
        ttl_seconds=5.0,
        resource=None,
        reraise=False,
    )
    assert finished.state is JobState.FAILED
    assert repo.get(job.id).state is JobState.FAILED


def test_run_job_renews_worker_lease_when_requested(conn) -> None:
    """Important 4: `renew_worker_lease=True` の間、`worker_leases` も更新され続けること。"""
    repo = JobRepository(conn)
    repo.submit("slow")
    claimed = repo.claim("A", ttl_seconds=0.3)
    assert claimed is not None
    assert leases.try_acquire_worker_lease(conn, "A", ttl_seconds=0.3) is True
    _, expires_before = leases.current_worker_lease(conn)

    def handler(run: JobRunContext) -> None:
        time.sleep(0.6)  # TTL(0.3秒)の2倍、更新されなければリーダーシップは失効するはず

    run_job(
        conn,
        repo,
        events_mod.EventBus(),
        claimed,
        handler,
        owner_id="A",
        ttl_seconds=0.3,
        resource=None,
        renew_worker_lease=True,
    )

    _, expires_after = leases.current_worker_lease(conn)
    assert expires_after > expires_before, (
        "ハンドラ実行中に worker_leases の expires_at が更新されなかった"
    )
