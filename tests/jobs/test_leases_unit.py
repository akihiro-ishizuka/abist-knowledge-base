"""`leases` モジュールの単一プロセス内での単体挙動。

プロセス境界を越えた排他そのものは `test_multiprocess_leases.py` が担当する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import ResourceKind
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema


@pytest.fixture
def conn(tmp_root: Path):
    c = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(c)
    yield c
    c.close()


def test_try_acquire_worker_lease_succeeds_when_no_lease_exists(conn) -> None:
    assert leases.try_acquire_worker_lease(conn, "A", ttl_seconds=5.0) is True
    lease = leases.current_worker_lease(conn)
    assert lease is not None
    assert lease[0] == "A"


def test_try_acquire_worker_lease_is_idempotent_for_the_same_owner(conn) -> None:
    assert leases.try_acquire_worker_lease(conn, "A", ttl_seconds=5.0) is True
    assert leases.try_acquire_worker_lease(conn, "A", ttl_seconds=5.0) is True


def test_try_acquire_worker_lease_fails_when_held_by_another_live_owner(conn) -> None:
    assert leases.try_acquire_worker_lease(conn, "A", ttl_seconds=5.0) is True
    assert leases.try_acquire_worker_lease(conn, "B", ttl_seconds=5.0) is False


def test_try_acquire_worker_lease_succeeds_when_previous_lease_expired(conn) -> None:
    assert leases.try_acquire_worker_lease(conn, "A", ttl_seconds=-1.0) is True  # 即座に期限切れ
    assert leases.try_acquire_worker_lease(conn, "B", ttl_seconds=5.0) is True
    assert leases.current_worker_lease(conn)[0] == "B"


def test_renew_worker_lease_fails_for_non_owner(conn) -> None:
    leases.try_acquire_worker_lease(conn, "A", ttl_seconds=5.0)
    assert leases.renew_worker_lease(conn, "B", ttl_seconds=5.0) is False


def test_release_worker_lease_allows_immediate_takeover(conn) -> None:
    leases.try_acquire_worker_lease(conn, "A", ttl_seconds=5.0)
    leases.release_worker_lease(conn, "A")
    assert leases.current_worker_lease(conn) is None
    assert leases.try_acquire_worker_lease(conn, "B", ttl_seconds=5.0) is True


def test_has_live_worker_reflects_expiry(conn) -> None:
    assert leases.has_live_worker(conn) is False
    leases.try_acquire_worker_lease(conn, "A", ttl_seconds=-1.0)
    assert leases.has_live_worker(conn) is False
    leases.try_acquire_worker_lease(conn, "A", ttl_seconds=30.0)
    assert leases.has_live_worker(conn) is True


def test_acquire_resource_lease_wait_false_raises_conflict_immediately(conn) -> None:
    with leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="A", ttl_seconds=30.0
    ):
        with (
            pytest.raises(AppError) as excinfo,
            leases.acquire_resource_lease(
                conn, ResourceKind.DOCS_WRITE, owner_id="B", ttl_seconds=5.0, wait=False
            ),
        ):
            pass
        assert excinfo.value.code == ErrorCode.CONFLICT
        assert excinfo.value.retryable is True


def test_acquire_resource_lease_releases_on_exit(conn) -> None:
    with leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="A", ttl_seconds=30.0
    ):
        pass
    # 解放済みなので即座に別オーナーが取得できる。
    with leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="B", ttl_seconds=30.0, wait=False
    ):
        pass


def test_acquire_resource_lease_releases_even_on_exception(conn) -> None:
    with (
        pytest.raises(RuntimeError),
        leases.acquire_resource_lease(
            conn, ResourceKind.DOCS_WRITE, owner_id="A", ttl_seconds=30.0
        ),
    ):
        raise RuntimeError("boom")
    with leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="B", ttl_seconds=30.0, wait=False
    ):
        pass


def test_different_corpus_keys_do_not_contend(conn) -> None:
    with (
        leases.acquire_resource_lease(
            conn, ResourceKind.CORPUS_WRITE, key="corpus-a", owner_id="A", ttl_seconds=30.0
        ),
        leases.acquire_resource_lease(
            conn,
            ResourceKind.CORPUS_WRITE,
            key="corpus-b",
            owner_id="B",
            ttl_seconds=30.0,
            wait=False,
        ),
    ):
        pass  # 別コーパスなので競合しない


def test_acquire_resource_lease_timeout_raises_conflict(conn) -> None:
    with leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="A", ttl_seconds=30.0
    ):
        with (
            pytest.raises(AppError) as excinfo,
            leases.acquire_resource_lease(
                conn,
                ResourceKind.DOCS_WRITE,
                owner_id="B",
                ttl_seconds=30.0,
                wait=True,
                timeout=0.2,
                poll_interval=0.05,
            ),
        ):
            pass
        assert excinfo.value.code == ErrorCode.CONFLICT


def test_acquire_resource_lease_succeeds_after_expiry_without_release(conn) -> None:
    """明示的な解放が無くても TTL 経過後は取得できる(クラッシュ耐性)。"""
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO resource_leases (resource_key, owner_id, job_id, expires_at) "
        "VALUES ('docs-write', 'dead', NULL, ?)",
        ((now - timedelta(seconds=1)).isoformat(),),
    )
    with leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="B", ttl_seconds=30.0, wait=False
    ):
        pass
