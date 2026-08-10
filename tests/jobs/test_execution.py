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

    fix2 補足: このハンドラは `run.check_lease()` を一度も呼ばない。つまり
    本テストは「協調的な確認を一切行わないハンドラ」に対する事後検知の
    バックストップが引き続き機能することの回帰テストでもある(fix2 は
    バックストップを置き換えるのではなく、事前に早期打ち切りできる手段を
    追加するものであるため)。書き込みが実際に横取り後止まることの直接検証は
    `test_multiprocess_leases.py::test_handler_writes_stop_after_resource_lease_is_stolen_mid_run`
    が行う。
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


def test_check_lease_does_not_false_positive_during_normal_run(conn) -> None:
    """fix2: 更新が常に成功する通常実行では `check_lease()` が誤って送出しないこと。

    `JobRunContext.check_lease()` はハンドラが各反復の間で呼ぶ協調的な確認だが、
    横取りが起きていない通常運用でこれが誤発火すると、正常なジョブが不要に
    失敗させられてしまう(false positive)。ここでは奪う側が存在しない状態で
    ループの中で毎回呼び、最後まで例外が出ないこと・ジョブが `SUCCEEDED` で
    終わることを確認する。
    """
    repo = JobRepository(conn)
    repo.submit("sync")
    resource_for_kind = {"sync": (ResourceKind.DOCS_WRITE, None)}
    claimed = repo.claim("A", ttl_seconds=0.3, resource_for_kind=resource_for_kind)
    assert claimed is not None

    check_count = 0

    def handler(run: JobRunContext) -> None:
        nonlocal check_count
        for _ in range(20):
            run.check_lease()  # 誰も奪っていないので何も起きないはず
            check_count += 1
            time.sleep(0.05)  # 合計1秒、TTL(0.3秒)の更新を複数回跨ぐ

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
    assert check_count == 20, "ハンドラのループが誤って途中で打ち切られた"
    assert finished.state is JobState.SUCCEEDED


def test_check_lease_call_overhead_is_negligible(conn) -> None:
    """fix2: `check_lease()` はタイトなループの中で毎回呼んでも支配的にならない
    ほど軽量であること(属性1回の読み取り程度)。

    実測のばらつきに対して十分な余裕を持った上限(10万回呼んで1秒未満)で
    検証する。CIの遅い環境でもフレーキーにならないよう、極端に厳しい閾値は
    避ける。
    """
    repo = JobRepository(conn)
    repo.submit("sync")
    claimed = repo.claim("A", ttl_seconds=30.0)
    assert claimed is not None

    elapsed_holder: dict[str, float] = {}

    def handler(run: JobRunContext) -> None:
        start = time.perf_counter()
        for _ in range(100_000):
            run.check_lease()
        elapsed_holder["elapsed"] = time.perf_counter() - start

    finished = run_job(
        conn,
        repo,
        events_mod.EventBus(),
        claimed,
        handler,
        owner_id="A",
        ttl_seconds=30.0,
        resource=None,
    )
    assert finished.state is JobState.SUCCEEDED
    assert elapsed_holder["elapsed"] < 1.0, (
        f"check_lease() 10万回呼び出しに {elapsed_holder['elapsed']:.3f}秒かかった"
        "(軽量であるべき確認処理が重くなっている)"
    )


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


def test_progress_snapshot_is_persisted_during_and_after_the_run(conn) -> None:
    """`jobs.progress` に最新の `emit()` 内容が保存されること。

    `job_events` は履歴、`jobs.progress` は最新状態という役割分担
    (設計書 §10.3)。`progress` は `job_to_dict()` 経由で `jobs show`／API／MCP が
    既に公開しているフィールドだが、以前は書き手が一人もおらず恒久的に
    `null` のままだった(読み手だけが存在する未配線)。実行中(ハンドラの中から
    別接続で観測)と終了後の両方で、直前の `emit()` と一致することを固定する。
    """
    repo = JobRepository(conn)
    job = repo.submit("sync")
    claimed = repo.claim("A", ttl_seconds=30.0)
    assert claimed is not None

    observed_mid_run: list[dict] = []

    def handler(run: JobRunContext) -> None:
        run.emit(phase="index-build", current=1, total=3, message="1件目", item="docs/a.md")
        # 実行中に別接続から観測する(finish 時にまとめて書いているのではなく、
        # emit のたびに最新状態が見えることの確認)。
        observer = connect(conn.execute("PRAGMA database_list").fetchone()[2])
        try:
            observed_mid_run.append(JobRepository(observer).get(job.id).progress)
        finally:
            observer.close()
        run.emit(phase="index-build", current=3, total=3, message="完了", item="docs/c.md")

    finished = run_job(
        conn,
        repo,
        events_mod.EventBus(),
        claimed,
        handler,
        owner_id="A",
        ttl_seconds=30.0,
        resource=None,
    )
    assert finished.state is JobState.SUCCEEDED

    assert observed_mid_run[0] is not None, "実行中に progress が書かれていない"
    assert observed_mid_run[0]["current"] == 1
    assert observed_mid_run[0]["message"] == "1件目"

    # 終了後は最後の emit の内容が残る(finish() は progress を消さない)。
    progress = finished.progress
    assert progress is not None
    assert progress["job_id"] == job.id
    assert progress["phase"] == "index-build"
    assert progress["current"] == 3
    assert progress["total"] == 3
    assert progress["message"] == "完了"
    assert progress["item"] == "docs/c.md"
    assert progress["severity"] == "info"
    assert progress["timestamp"]

    # 履歴(job_events)は全件、スナップショットは最新1件という役割分担。
    events = events_mod.list_events(conn, job.id)
    assert [e.current for e in events] == [1, 3]
    assert events[-1].to_dict() == progress
