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

from abist_kb.domain.errors import AppError
from abist_kb.domain.job import ResourceKind
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema
from abist_kb.infrastructure.jobs.repository import JobRepository


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def cmd_worker_lease(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    ensure_jobs_schema(conn)
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


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    worker_lease = sub.add_parser("worker-lease")
    worker_lease.add_argument("--db", required=True)
    worker_lease.add_argument("--owner", required=True)
    worker_lease.add_argument("--ttl", type=float, default=2.0)
    worker_lease.add_argument("--interval", type=float, default=0.2)
    worker_lease.add_argument("--duration", type=float, default=5.0)
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
