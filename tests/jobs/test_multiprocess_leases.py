"""複数の実プロセスによるリース検証(brief Step 2: 実装前に書く「赤」テスト)。

`subprocess` で実プロセスを2〜3起動する。スレッドは同一インタプリタ・同一の
`sqlite3.Connection` プールを共有してしまい、`BEGIN IMMEDIATE` が実際に
プロセス境界を越えて調停しているかどうかを何も証明しないため、意図的に
スレッドではなく `subprocess.Popen` を使う(brief の指示どおり)。

このモジュールを最初に実行した時点では `abist_kb.infrastructure.jobs.leases` /
`abist_kb.infrastructure.jobs.repository` が存在せず、全テストが
`ModuleNotFoundError` 相当(`ImportError`)で失敗することを確認済み(赤)。
実装後にこのテストが緑になることで、複数プロセス間の排他が
`asyncio.Lock` 等のインプロセスの仕組みではなく DB(`BEGIN IMMEDIATE`)で
実現されていることを検証する。
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema
from abist_kb.infrastructure.jobs.repository import JobRepository

_WORKER_SCRIPT = Path(__file__).with_name("mp_worker.py")


class _LineReader:
    """子プロセスの stdout を別スレッドで読み、行単位でキューへ流す。

    `Popen.stdout.readline()` は素朴に呼ぶとブロックしてタイムアウトできない
    ため、専用スレッドで読み続けてタイムアウト付きで取り出せるようにする。
    """

    def __init__(self, stream: Any) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: Any) -> None:
        for line in iter(stream.readline, ""):
            if line:
                self._queue.put(line)

    def read_json(self, *, timeout: float) -> dict[str, Any] | None:
        try:
            line = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        return json.loads(line)

    def read_json_matching(self, predicate: Any, *, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            payload = self.read_json(timeout=remaining)
            if payload is None:
                return None
            if predicate(payload):
                return payload

    def drain(self) -> list[dict[str, Any]]:
        collected = []
        while True:
            payload = self.read_json(timeout=0.05)
            if payload is None:
                return collected
            collected.append(payload)


def _spawn(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(_WORKER_SCRIPT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


@pytest.fixture
def db_path(tmp_root: Path) -> Iterator[Path]:
    path = tmp_root / "jobs.sqlite"
    conn = connect(path)
    ensure_jobs_schema(conn)
    conn.close()
    yield path


def test_exactly_one_leader_across_two_processes(db_path: Path) -> None:
    """`worker_leases` のリーダーがプロセス境界を越えて常に1つだけであること。

    2プロセスが同時に取得・更新を試み続ける。先にリーダーになった方は自分の
    heartbeat を ttl 内に更新し続けるため、正しい実装では負けた側は本テストの
    観測窓の間ずっとリーダーになれない(=「両方が交互にリーダーになる」ことは
    期待しない。それは早取り勝ちの単一リーダー原則に反する)。ここで実際に
    検証したい不変条件は「ある瞬間に自分がリーダーだと報告する区間
    ([t_before, t_after])が2プロセス間で重ならないこと」であり、重なりが
    起きるのはアプリ側のロックだけで調停している(=別プロセスの書込を検知
    できない)壊れた実装だけである。DB の `BEGIN IMMEDIATE` が実際に
    プロセス間で調停していれば理論上重ならない。
    """
    proc_a = _spawn(
        "worker-lease",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--ttl",
        "2.0",
        "--interval",
        "0.1",
        "--duration",
        "3.0",
    )
    proc_b = _spawn(
        "worker-lease",
        "--db",
        str(db_path),
        "--owner",
        "B",
        "--ttl",
        "2.0",
        "--interval",
        "0.1",
        "--duration",
        "3.0",
    )
    reader_a = _LineReader(proc_a.stdout)
    reader_b = _LineReader(proc_b.stdout)
    try:
        lines_a = [reader_a.read_json(timeout=5.0) for _ in range(25)]
        lines_b = [reader_b.read_json(timeout=5.0) for _ in range(25)]
    finally:
        _terminate(proc_a)
        _terminate(proc_b)

    events_a = [line for line in lines_a if line and line["leader"]]
    events_b = [line for line in lines_b if line and line["leader"]]
    # 早取り勝ちの単一リーダー原則により、通常はどちらか一方だけが
    # ずっとリーダーであり続ける。少なくとも一方は必ずリーダーになれる。
    assert events_a or events_b, "A/B どちらもリーダーになれなかった"

    for a in events_a:
        for b in events_b:
            overlap = a["t_before"] < b["t_after"] and b["t_before"] < a["t_after"]
            assert not overlap, f"A と B が同時にリーダーを名乗った: {a} / {b}"


def test_leadership_transfers_after_leader_dies_not_before(db_path: Path) -> None:
    """リーダー停止後、lease 期限切れを待ってから別プロセスが引き継ぐこと。"""
    ttl = 1.5
    proc_a = _spawn(
        "worker-lease",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--ttl",
        str(ttl),
        "--interval",
        "0.15",
        "--duration",
        "20.0",
    )
    reader_a = _LineReader(proc_a.stdout)
    became_leader = reader_a.read_json_matching(lambda p: p["leader"], timeout=5.0)
    assert became_leader is not None, "A がリーダーになれなかった"

    proc_b = _spawn(
        "worker-lease",
        "--db",
        str(db_path),
        "--owner",
        "B",
        "--ttl",
        str(ttl),
        "--interval",
        "0.15",
        "--duration",
        "20.0",
    )
    reader_b = _LineReader(proc_b.stdout)

    # A が生きている間は B がリーダーになれないこと(期限切れ前の横取り禁止)。
    time.sleep(ttl * 0.6)
    b_before_kill = reader_b.drain()
    assert not any(p["leader"] for p in b_before_kill), (
        "A が生きている間に B がリーダーを横取りした"
    )

    kill_time = time.time()
    _terminate(proc_a)

    b_became_leader = reader_b.read_json_matching(lambda p: p["leader"], timeout=10.0)
    assert b_became_leader is not None, "A 停止後に B が引き継がなかった"
    elapsed = b_became_leader["t_after"] - kill_time
    assert elapsed >= ttl * 0.5, (
        f"B が lease 期限切れを待たずに引き継いだ(kill後 {elapsed:.2f}秒、ttl={ttl}秒)"
    )

    _terminate(proc_b)


def test_docs_write_second_acquirer_waits_then_succeeds(db_path: Path) -> None:
    """`docs-write` は2つ目の取得者を待たせる(拒否ではなく直列化する)という決定の検証。

    設計書 §10.2 の「バッチ・esa・Web・Git同期を全プロセス横断で直列化する」という
    文言("拒否する"ではなく "直列化する")に基づき、既定の `acquire_resource_lease`
    は `wait=True` で待機し、CONFLICT を出さずに前者の解放後に取得できる仕様とした。
    """
    proc_a = _spawn(
        "resource-lease",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--kind",
        "docs-write",
        "--wait",
        "--hold",
        "1.2",
        "--ttl",
        "5.0",
    )
    reader_a = _LineReader(proc_a.stdout)
    acquired_a = reader_a.read_json_matching(lambda p: p["status"] == "acquired", timeout=5.0)
    assert acquired_a is not None, "A が docs-write を取得できなかった"

    proc_b = _spawn(
        "resource-lease",
        "--db",
        str(db_path),
        "--owner",
        "B",
        "--kind",
        "docs-write",
        "--wait",
        "--hold",
        "0.1",
        "--ttl",
        "5.0",
        "--timeout",
        "10.0",
    )
    reader_b = _LineReader(proc_b.stdout)

    released_a = reader_a.read_json_matching(lambda p: p["status"] == "released", timeout=5.0)
    assert released_a is not None
    acquired_b = reader_b.read_json_matching(lambda p: p["status"] == "acquired", timeout=10.0)
    assert acquired_b is not None, "B が待った末に取得できなかった(仕様どおりなら取得できるはず)"
    assert acquired_b["t"] >= released_a["t"], "B が A の解放前に取得した(直列化されていない)"

    _terminate(proc_a)
    _terminate(proc_b)


def test_docs_write_second_acquirer_fails_fast_when_wait_is_false(db_path: Path) -> None:
    """`wait=False` を明示した場合は待たずに `CONFLICT` になること(busy 系の互換用)。"""
    proc_a = _spawn(
        "resource-lease",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--kind",
        "docs-write",
        "--wait",
        "--hold",
        "2.0",
        "--ttl",
        "5.0",
    )
    reader_a = _LineReader(proc_a.stdout)
    assert reader_a.read_json_matching(lambda p: p["status"] == "acquired", timeout=5.0)

    proc_b = _spawn(
        "resource-lease",
        "--db",
        str(db_path),
        "--owner",
        "B",
        "--kind",
        "docs-write",
        "--hold",
        "0.1",
        "--ttl",
        "5.0",
    )
    reader_b = _LineReader(proc_b.stdout)
    denied = reader_b.read_json_matching(lambda p: p["status"] == "denied", timeout=3.0)
    assert denied is not None, "wait 指定無しの2つ目の取得者が即座に CONFLICT にならなかった"
    assert denied["code"] == "CONFLICT"

    _terminate(proc_a)
    _terminate(proc_b)


def test_corpus_write_different_corpora_do_not_contend(db_path: Path) -> None:
    """`corpus-write:<corpus>` はコーパスが違えば同時取得できること。"""
    proc_a = _spawn(
        "resource-lease",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--kind",
        "corpus-write",
        "--key",
        "corpus-1",
        "--wait",
        "--hold",
        "1.0",
        "--ttl",
        "5.0",
    )
    proc_b = _spawn(
        "resource-lease",
        "--db",
        str(db_path),
        "--owner",
        "B",
        "--kind",
        "corpus-write",
        "--key",
        "corpus-2",
        "--wait",
        "--hold",
        "1.0",
        "--ttl",
        "5.0",
    )
    reader_a = _LineReader(proc_a.stdout)
    reader_b = _LineReader(proc_b.stdout)
    acquired_a = reader_a.read_json_matching(lambda p: p["status"] == "acquired", timeout=2.0)
    acquired_b = reader_b.read_json_matching(lambda p: p["status"] == "acquired", timeout=2.0)
    assert acquired_a is not None
    assert acquired_b is not None
    # 両方がほぼ同時に取得できていること(片方が待たされていない)。
    assert abs(acquired_a["t"] - acquired_b["t"]) < 0.5

    _terminate(proc_a)
    _terminate(proc_b)


def test_corpus_write_same_corpus_contends(db_path: Path) -> None:
    """同一コーパスの `corpus-write` は排他されること(直列化される)。"""
    proc_a = _spawn(
        "resource-lease",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--kind",
        "corpus-write",
        "--key",
        "corpus-1",
        "--wait",
        "--hold",
        "1.0",
        "--ttl",
        "5.0",
    )
    reader_a = _LineReader(proc_a.stdout)
    assert reader_a.read_json_matching(lambda p: p["status"] == "acquired", timeout=2.0)

    proc_b = _spawn(
        "resource-lease",
        "--db",
        str(db_path),
        "--owner",
        "B",
        "--kind",
        "corpus-write",
        "--key",
        "corpus-1",
        "--hold",
        "0.1",
        "--ttl",
        "5.0",
    )
    reader_b = _LineReader(proc_b.stdout)
    denied = reader_b.read_json_matching(lambda p: p["status"] == "denied", timeout=3.0)
    assert denied is not None, "同一コーパスへの corpus-write が競合しなかった"

    _terminate(proc_a)
    _terminate(proc_b)


def test_exactly_one_process_claims_the_same_queued_job(db_path: Path) -> None:
    """複数プロセスが同一ジョブを claim しようとして1つだけ成功すること(brief Step 4)。"""
    conn = connect(db_path)
    try:
        repo = JobRepository(conn)
        job = repo.submit("noop", {})
    finally:
        conn.close()

    owners = ("A", "B", "C")
    procs = [
        _spawn("claim-job", "--db", str(db_path), "--owner", owner, "--ttl", "5.0")
        for owner in owners
    ]
    readers = [_LineReader(proc.stdout) for proc in procs]
    claims = [reader.read_json(timeout=5.0) for reader in readers]
    for proc in procs:
        assert proc.wait(timeout=5) == 0

    claimed_job_ids = [c["claimed"] for c in claims if c is not None and c["claimed"] is not None]
    assert claimed_job_ids == [job.id], f"claim すべきは厳密に1プロセスのはずが: {claims}"

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT state, owner_id FROM jobs WHERE id = ?", (job.id,)).fetchone()
    finally:
        conn.close()
    assert row["state"] == "running"
    assert row["owner_id"] in owners
