"""実プロセス間のリース検証に使う補助スクリプト(pytest からのみ起動される)。

`test_multiprocess_leases.py` が `subprocess.Popen([sys.executable, __file__, ...])`
で複数の実プロセスとして起動する。スレッドではなく別プロセスにする理由は
brief に明記されている通り: スレッドは同一インタプリタ・同一コネクションプールを
共有してしまい、`BEGIN IMMEDIATE` が実際に複数プロセス間で調停しているかどうかを
何も証明しない。

標準出力へ1行1 JSON(NDJSON)で状態を吐く。テスト側はそれを読んでアサートする。
このファイルは `test_*.py`/`*_test.py` のどちらにも一致しないため pytest には
収集されない。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from abist_kb.application.job_service import JobService
from abist_kb.domain.errors import AppError
from abist_kb.domain.job import ResourceKind
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.infrastructure.jobs.supervisor import JobRunContext, WorkerSupervisor


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def cmd_worker_lease(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    ensure_jobs_schema(conn)
    if args.force_always_leader:
        # Minor 6 の検証専用: DBの調停を完全に迂回して「常に自分がリーダーだ」と
        # 詐称する壊れた実装を模する(レビューが使った手法そのもの)。
        # `test_leadership_overlap_check_detects_broken_try_acquire` が、この
        # 明らかに壊れた実装に対して `test_exactly_one_leader_across_two_processes`
        # と同じ重なり検出ロジックが確実に失敗を検知することを確認するために使う。
        leases.try_acquire_worker_lease = lambda *_a, **_k: True  # type: ignore[assignment]
    is_leader = False
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            t_before = time.time()
            if is_leader:
                is_leader = leases.renew_worker_lease(conn, args.owner, ttl_seconds=args.ttl)
            if not is_leader:
                is_leader = leases.try_acquire_worker_lease(conn, args.owner, ttl_seconds=args.ttl)
            t_after = time.time()
            _emit(
                {
                    "owner": args.owner,
                    "leader": is_leader,
                    "t_before": t_before,
                    "t_after": t_after,
                }
            )
            time.sleep(args.interval)
    finally:
        conn.close()


def cmd_resource_lease(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    ensure_jobs_schema(conn)
    kind = ResourceKind(args.kind)
    try:
        with leases.acquire_resource_lease(
            conn,
            kind,
            key=args.key,
            owner_id=args.owner,
            ttl_seconds=args.ttl,
            wait=args.wait,
            timeout=args.timeout,
        ):
            _emit({"owner": args.owner, "status": "acquired", "t": time.time()})
            time.sleep(args.hold)
        _emit({"owner": args.owner, "status": "released", "t": time.time()})
    except AppError as exc:
        _emit(
            {
                "owner": args.owner,
                "status": "denied",
                "code": str(exc.code),
                "t": time.time(),
            }
        )
    finally:
        conn.close()


def cmd_claim_job(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    ensure_jobs_schema(conn)
    repo = JobRepository(conn)
    job = repo.claim(args.owner, ttl_seconds=args.ttl)
    _emit({"owner": args.owner, "claimed": job.id if job is not None else None})
    conn.close()


def _make_slow_handler(owner: str, sleep_seconds: float):
    """`sleep_seconds` 秒スリープするだけのハンドラ。`emit()` を一切呼ばない。

    レビュー Critical 1 が指摘した「ハンドラが進捗報告(`emit()`)を止めても
    リースが生き続けなければならない」という条件を意図的に満たすため、
    このハンドラはスリープ中一度も `run.emit()` を呼ばない。
    """

    def handler(run: JobRunContext) -> None:
        _emit({"owner": owner, "status": "handler_start", "t": time.time()})
        time.sleep(sleep_seconds)
        _emit({"owner": owner, "status": "handler_end", "t": time.time()})

    return handler


def cmd_run_inline_job(args: argparse.Namespace) -> None:
    """`JobService.run_inline` 経由でスリープするジョブを実行する(Critical 1 再現用)。"""
    conn = connect(Path(args.db))
    ensure_jobs_schema(conn)
    resource_for_kind = (
        {args.kind: (ResourceKind(args.resource), args.key)} if args.resource else {}
    )
    service = JobService(
        conn,
        owner_id=args.owner,
        handlers={args.kind: _make_slow_handler(args.owner, args.sleep)},
        resource_for_kind=resource_for_kind,
        lease_ttl_seconds=args.ttl,
    )
    try:
        service.run_inline(args.kind)
        _emit({"owner": args.owner, "status": "succeeded", "t": time.time()})
    except AppError as exc:
        _emit({"owner": args.owner, "status": "failed", "code": str(exc.code), "t": time.time()})
    finally:
        conn.close()


def cmd_worker_run_job(args: argparse.Namespace) -> None:
    """`WorkerSupervisor` 経由でキューのジョブを消費し続ける
    (Critical 2/Important 4 再現用: キュー経由でも resource lease を取り、
    長時間ジョブの間もリーダーシップを保つことを確認する)。
    """
    conn = connect(Path(args.db))
    ensure_jobs_schema(conn)
    resource_for_kind = (
        {args.kind: (ResourceKind(args.resource), args.key)} if args.resource else {}
    )
    supervisor = WorkerSupervisor(
        conn,
        owner_id=args.owner,
        lease_ttl=args.ttl,
        handlers={args.kind: _make_slow_handler(args.owner, args.sleep)},
        resource_for_kind=resource_for_kind,
    )
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            t_before = time.time()
            did_work = supervisor.tick()
            t_after = time.time()
            _emit(
                {
                    "owner": args.owner,
                    "leader": supervisor.is_leader,
                    "did_work": did_work,
                    "t_before": t_before,
                    "t_after": t_after,
                }
            )
            if not did_work:
                time.sleep(args.poll)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    worker_lease = sub.add_parser("worker-lease")
    worker_lease.add_argument("--db", required=True)
    worker_lease.add_argument("--owner", required=True)
    worker_lease.add_argument("--ttl", type=float, default=2.0)
    worker_lease.add_argument("--interval", type=float, default=0.2)
    worker_lease.add_argument("--duration", type=float, default=5.0)
    worker_lease.add_argument(
        "--force-always-leader",
        action="store_true",
        help="Minor 6 検証専用: try_acquire_worker_lease を常に True にする壊れた実装を模する。",
    )
    worker_lease.set_defaults(func=cmd_worker_lease)

    resource_lease = sub.add_parser("resource-lease")
    resource_lease.add_argument("--db", required=True)
    resource_lease.add_argument("--owner", required=True)
    resource_lease.add_argument("--kind", required=True)
    resource_lease.add_argument("--key", default=None)
    resource_lease.add_argument("--ttl", type=float, default=5.0)
    resource_lease.add_argument("--wait", action="store_true")
    resource_lease.add_argument("--timeout", type=float, default=None)
    resource_lease.add_argument("--hold", type=float, default=1.0)
    resource_lease.set_defaults(func=cmd_resource_lease)

    claim_job = sub.add_parser("claim-job")
    claim_job.add_argument("--db", required=True)
    claim_job.add_argument("--owner", required=True)
    claim_job.add_argument("--ttl", type=float, default=5.0)
    claim_job.set_defaults(func=cmd_claim_job)

    run_inline_job = sub.add_parser("run-inline-job")
    run_inline_job.add_argument("--db", required=True)
    run_inline_job.add_argument("--owner", required=True)
    run_inline_job.add_argument("--kind", default="sleepy")
    run_inline_job.add_argument("--sleep", type=float, default=1.0)
    run_inline_job.add_argument("--ttl", type=float, default=2.0)
    run_inline_job.add_argument("--resource", default=None)
    run_inline_job.add_argument("--key", default=None)
    run_inline_job.set_defaults(func=cmd_run_inline_job)

    worker_run_job = sub.add_parser("worker-run-job")
    worker_run_job.add_argument("--db", required=True)
    worker_run_job.add_argument("--owner", required=True)
    worker_run_job.add_argument("--kind", default="sleepy")
    worker_run_job.add_argument("--sleep", type=float, default=1.0)
    worker_run_job.add_argument("--ttl", type=float, default=2.0)
    worker_run_job.add_argument("--resource", default=None)
    worker_run_job.add_argument("--key", default=None)
    worker_run_job.add_argument("--duration", type=float, default=10.0)
    worker_run_job.add_argument("--poll", type=float, default=0.1)
    worker_run_job.set_defaults(func=cmd_worker_run_job)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
