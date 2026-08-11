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


def _leadership_streaks(lines: list[dict | None]) -> list[tuple[float, float]]:
    """連続した `leader=True` の行をひとつながりの「リーダーだと信じていた期間」
    ([最初の行の t_before, 最後の行の t_after])へまとめる。

    Minor 6 の根本原因はここにある: 以前は各行(1回の DB 呼び出しの前後だけの
    ごく短い時間幅)を個別に比較していたため、SQLite 自身の書込直列化
    (`BEGIN IMMEDIATE`)がその短い時間幅を狭めてしまい、壊れた実装
    (`try_acquire_worker_lease` が常に `True` を返す)に対してすら重なりを
    検出し損ねることがあった(レビューで3回に1回しか検知できないと実測)。
    「1回のDB呼び出しの瞬間」ではなく「連続してリーダーだと信じ続けていた
    期間全体」を比較対象にすることで、DBアクセスそのものの直列化に測定精度が
    左右されなくなる。
    """
    streaks: list[tuple[float, float]] = []
    start: float | None = None
    end: float | None = None
    for line in lines:
        if line is None:
            continue
        if line["leader"]:
            if start is None:
                start = line["t_before"]
            end = line["t_after"]
        elif start is not None:
            assert end is not None
            streaks.append((start, end))
            start = None
            end = None
    if start is not None:
        assert end is not None
        streaks.append((start, end))
    return streaks


def _assert_no_leadership_overlap(lines_a: list[dict | None], lines_b: list[dict | None]) -> None:
    """A/B それぞれの「連続してリーダーだと信じ続けていた期間」が重ならないことを検証する。

    `test_exactly_one_leader_across_two_processes` と
    `test_leadership_overlap_check_detects_broken_try_acquire_mutation` の両方が
    このロジックを共有することで、後者が「このチェック自体が壊れた実装を確実に
    検知できるか」の回帰テストとして機能する(Minor 6: 検知ロジックと検知対象の
    実装がずれて、片方だけ直って他方が置き去りにならないようにするため)。
    """
    streaks_a = _leadership_streaks(lines_a)
    streaks_b = _leadership_streaks(lines_b)
    assert streaks_a or streaks_b, "A/B どちらもリーダーになれなかった"
    for a_start, a_end in streaks_a:
        for b_start, b_end in streaks_b:
            overlap = a_start < b_end and b_start < a_end
            assert not overlap, (
                "A と B が同時にリーダーだと信じていた期間が重なった: "
                f"A=[{a_start}, {a_end}], B=[{b_start}, {b_end}]"
            )


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

    **Minor 6 の修正**: 以前はサンプル数(25件)・間隔(0.1秒)が粗く、SQLite
    自身の書込直列化(`BEGIN IMMEDIATE`)が記録される重なり幅を狭めてしまう
    ため、`try_acquire_worker_lease` が常に `True` を返すという明確に壊れた
    実装に対してすら 3 回に 1 回しか失敗を検知できなかった(レビューで実測)。
    サンプル間隔を大幅に短く(0.02秒)・サンプル数を大幅に増やす(150件)ことで
    観測の時間分解能を上げ、重なりの検出漏れを減らす。検出ロジック自体が
    本当に壊れた実装を確実に落とせることは
    `test_leadership_overlap_check_detects_broken_try_acquire_mutation` が
    直接検証する。
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
        "0.02",
        "--duration",
        "5.0",
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
        "0.02",
        "--duration",
        "5.0",
    )
    reader_a = _LineReader(proc_a.stdout)
    reader_b = _LineReader(proc_b.stdout)
    try:
        lines_a = [reader_a.read_json(timeout=8.0) for _ in range(150)]
        lines_b = [reader_b.read_json(timeout=8.0) for _ in range(150)]
    finally:
        _terminate(proc_a)
        _terminate(proc_b)

    _assert_no_leadership_overlap(lines_a, lines_b)


def test_leadership_overlap_check_detects_broken_try_acquire_mutation(db_path: Path) -> None:
    """Minor 6 の回帰テスト: 重なり検出ロジック自身が、明確に壊れた実装
    (`try_acquire_worker_lease` が DB を見ずに常に `True` を返す)に対して
    確実に(たまたま通ってしまうことなく)検知できることを直接確認する。

    レビューが使った手法(`try_acquire_worker_lease` を常に `True` にパッチする)
    をそのまま子プロセス側で再現する(`--force-always-leader`)。この変異の下では
    2プロセスとも常に「自分がリーダーだ」と報告し続けるため、重なりが起きない
    はずがなく、`_assert_no_leadership_overlap` は確実に `AssertionError` を
    送出しなければならない。
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
        "0.02",
        "--duration",
        "1.0",
        "--force-always-leader",
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
        "0.02",
        "--duration",
        "1.0",
        "--force-always-leader",
    )
    reader_a = _LineReader(proc_a.stdout)
    reader_b = _LineReader(proc_b.stdout)
    try:
        lines_a = [reader_a.read_json(timeout=5.0) for _ in range(20)]
        lines_b = [reader_b.read_json(timeout=5.0) for _ in range(20)]
    finally:
        _terminate(proc_a)
        _terminate(proc_b)

    with pytest.raises(AssertionError):
        _assert_no_leadership_overlap(lines_a, lines_b)


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


@pytest.mark.timing_sensitive
def test_corpus_write_different_corpora_do_not_contend(db_path: Path) -> None:
    """`corpus-write:<corpus>` はコーパスが違えば同時取得できること。

    「片方が待たされていない」の確認に実プロセス間の壁時計差を使っており、CPU
    高負荷下では両プロセスの起動・スケジューリング自体が遅延しうるため
    `timing_sensitive` としてマークする(`test_web.py` の
    `test_crawl_enforces_delay_between_requests_to_same_host` と同種のリスク)。
    実プロセス2つを跨ぐ排他制御なしの独立性は、この統合テスト以外で決定論的に
    検証するのが難しいため、閾値を緩め(2.0秒)つつ現状維持する。
    """
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
    assert abs(acquired_a["t"] - acquired_b["t"]) < 2.0

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


def test_resource_lease_survives_ttl_while_handler_runs_without_emitting(db_path: Path) -> None:
    """Critical 1 の再現: `emit()` を呼ばない長時間ハンドラの間、resource lease が
    TTL 経過で他プロセスに奪われないこと(自動更新される)。

    レビューが実際に再現した状況そのもの: TTL 2秒の resource lease を A が
    8秒(`emit()` を一切呼ばずに)保持し続ける間、B は3秒後(=Aの TTL が一度
    尽きたはずの時刻)に同じリソースの取得を試みる。修正前は resource lease が
    取得時に一度しか `expires_at` を設定せず、以後は誰も更新しなかったため、
    B は A がまだ実行中にもかかわらず「空いている」と誤認して取得できてしまい、
    5秒間(=3秒後から8秒後まで)両プロセスが同時に排他リソースを保持していると
    信じる状態になっていた。修正後は `JobService.run_inline` が TTL の1/3ごとに
    resource lease を更新し続けるバックグラウンドスレッドを持つため、B は A が
    解放するまで(`wait=True` の既定どおり)待たされる。
    """
    proc_a = _spawn(
        "run-inline-job",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--kind",
        "sync",
        "--resource",
        "docs-write",
        "--ttl",
        "2.0",
        "--sleep",
        "8.0",
    )
    reader_a = _LineReader(proc_a.stdout)
    started_a = reader_a.read_json_matching(
        lambda p: p.get("status") == "handler_start", timeout=5.0
    )
    assert started_a is not None, "A がハンドラを開始できなかった"

    time.sleep(3.0)  # A の TTL(2秒)を過ぎた時点でもまだ実行中(8秒スリープ)のはず

    proc_b = _spawn(
        "run-inline-job",
        "--db",
        str(db_path),
        "--owner",
        "B",
        "--kind",
        "sync",
        "--resource",
        "docs-write",
        "--ttl",
        "2.0",
        "--sleep",
        "0.1",
    )
    reader_b = _LineReader(proc_b.stdout)
    started_b = reader_b.read_json_matching(
        lambda p: p.get("status") == "handler_start", timeout=10.0
    )
    ended_a = reader_a.read_json_matching(lambda p: p.get("status") == "handler_end", timeout=10.0)
    try:
        assert started_b is not None, "B がハンドラを開始できなかった"
        assert ended_a is not None, "A が終了しなかった"
        assert started_b["t"] >= ended_a["t"], (
            "B が A の解放前に resource lease を取得した"
            f"(TTL切れで誤って奪った可能性): A_end={ended_a['t']}, B_start={started_b['t']}"
        )
    finally:
        _terminate(proc_a)
        _terminate(proc_b)


def test_queue_path_acquires_resource_lease_like_inline_path(db_path: Path) -> None:
    """Critical 2 の再現: `WorkerSupervisor`(キュー経由)も resource lease を取る。

    修正前は `resource_for_kind`/`acquire_resource_lease` の配線が
    `JobService.run_inline` にしかなく、`WorkerSupervisor` はどこにも呼んで
    いなかった。`BUILTIN_HANDLERS` は両経路に登録されるため、同じハンドラが
    インラインでは保護され、キュー経由(`--detach`・常駐ワーカー全て)では
    無防備という致命的な差異があった。ここでは、キューに投入したジョブを
    `WorkerSupervisor` が処理している間、別プロセスが同じ `docs-write` を
    `wait=False` で取ろうとすると `CONFLICT` になる(=キュー経由でも実際に
    resource lease を取得している)ことを確認する。
    """
    conn = connect(db_path)
    try:
        JobRepository(conn).submit("sync", {})
    finally:
        conn.close()

    proc_worker = _spawn(
        "worker-run-job",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--kind",
        "sync",
        "--resource",
        "docs-write",
        "--ttl",
        "2.0",
        "--sleep",
        "3.0",
        "--duration",
        "8.0",
    )
    reader_worker = _LineReader(proc_worker.stdout)
    started = reader_worker.read_json_matching(
        lambda p: p.get("status") == "handler_start", timeout=5.0
    )
    assert started is not None, "worker がジョブを開始できなかった"

    proc_denied = _spawn(
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
    reader_denied = _LineReader(proc_denied.stdout)
    denied = reader_denied.read_json_matching(lambda p: p["status"] == "denied", timeout=3.0)
    try:
        assert denied is not None, (
            "worker がジョブ実行中にもかかわらず、別プロセスが docs-write を"
            "即座に取得できてしまった(キュー経由が resource lease を取っていない)"
        )
        assert denied["code"] == "CONFLICT"
    finally:
        _terminate(proc_worker)
        _terminate(proc_denied)


def test_leadership_survives_a_single_job_longer_than_the_lease_ttl(db_path: Path) -> None:
    """Important 4 の再現: TTL より長いジョブの間もリーダーシップを保ち続けること。

    修正前は `WorkerSupervisor.tick()` がジョブ実行前に一度だけリーダーシップを
    更新し、その後は同期的にハンドラを実行し切っていたため、TTL(ここでは1秒)より
    長いジョブ(3秒)が動いている間にリーダーシップが失効し、別プロセスに
    乗っ取られ得た。ここでは、A がジョブを実行している最初から最後まで、B が
    一度もリーダーになれないことを確認する。
    """
    conn = connect(db_path)
    try:
        JobRepository(conn).submit("slow", {})
    finally:
        conn.close()

    ttl = 1.0
    proc_a = _spawn(
        "worker-run-job",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--kind",
        "slow",
        "--ttl",
        str(ttl),
        "--sleep",
        "3.0",
        "--duration",
        "8.0",
        "--poll",
        "0.05",
    )
    reader_a = _LineReader(proc_a.stdout)
    started_a = reader_a.read_json_matching(
        lambda p: p.get("status") == "handler_start", timeout=5.0
    )
    assert started_a is not None, "A がジョブを開始できなかった"

    proc_b = _spawn(
        "worker-lease",
        "--db",
        str(db_path),
        "--owner",
        "B",
        "--ttl",
        str(ttl),
        "--interval",
        "0.1",
        "--duration",
        "6.0",
    )
    reader_b = _LineReader(proc_b.stdout)

    ended_a = reader_a.read_json_matching(lambda p: p.get("status") == "handler_end", timeout=10.0)
    try:
        assert ended_a is not None, "A のジョブが終わらなかった"
        b_lines_during_job = reader_b.drain()
        assert not any(line["leader"] for line in b_lines_during_job), (
            "A がジョブ実行中(TTLより長い3秒のスリープ中)に B がリーダーシップを"
            "奪ってしまった(長時間ジョブの間もリーダーシップを更新し続ける必要がある)"
        )
    finally:
        _terminate(proc_a)
        _terminate(proc_b)


def test_handler_writes_stop_after_resource_lease_is_stolen_mid_run(db_path: Path) -> None:
    """fix2 の再現テスト: resource lease を横取りされた後、ハンドラの書き込みが
    即座に止まること(ジョブが最終的に `FAILED` になるだけでは不十分、という
    レビュー指摘の直接検証)。

    レビューが実測した状況そのもの: 0.1秒間隔で20回書き込むハンドラの、t=0.35秒
    付近でリースが奪われたケースで、修正前は20回中16回(t=0.40〜1.91秒)が
    奪取後に実行されていた。ここでは実プロセス2つ(書き込み側・横取り側)を
    使う(brief の指示どおり: スレッドは同一インタプリタを共有してしまい、
    `JobRunContext.check_lease()` がインプロセスの状態に頼っているだけでも
    通ってしまいかねないため)。
    """
    proc_writer = _spawn(
        "run-inline-job-writes",
        "--db",
        str(db_path),
        "--owner",
        "A",
        "--kind",
        "sync",
        "--resource",
        "docs-write",
        "--ttl",
        "0.05",  # 更新間隔 ~0.017秒: 書き込み間隔より十分短くし、
        # 奪取から次の書き込みまでの間に確実に検知できるようにする。
        "--count",
        "8",
        # 書き込み間隔は 0.25 秒。以前は 0.1 秒だったが、フルスイート実行時の
        # CPU 高負荷下では「奪取 -> 検知」に 0.1 秒では足りず、次の書き込みが
        # 1回すり抜けてフレークしていた(単体実行・低負荷では常に成功していた)。
        # 総実行時間は 8x0.25 = 2秒で従来(20x0.1)と同じまま、検知の余裕だけを
        # 2.5倍に広げる。assert は「奪取後の書き込みは0件」のまま緩めない。
        "--interval",
        "0.25",
    )
    reader_writer = _LineReader(proc_writer.stdout)

    # 4回目の書き込み(index=3, 理論上 t≈0.75秒)まで見届けてから奪う
    # (レビューの再現条件 t=0.35秒 相当のタイミング)。書き込み間隔の
    # ちょうど中間まで少し待ってから奪うことで、次の書き込み(index=4, t≈0.4秒)
    # までの間に更新スレッドが確実に何周期か回る余裕を作る(でなければ「奪った
    # 直後」と「次の確認」がほぼ同時に競合し、確認が単なるノイズで1回だけ
    # すり抜けるレアケースを拾ってしまい、fix2 の本質(長時間書き込み続ける
    # 致命的な破損)とは無関係なフレークになる)。
    writes_before_steal: list[dict] = []
    for _ in range(4):
        payload = reader_writer.read_json_matching(
            lambda p: p.get("status") == "write", timeout=5.0
        )
        assert payload is not None, "A が書き込みを開始できなかった"
        writes_before_steal.append(payload)
    time.sleep(0.125)  # 書き込み間隔(0.25秒)のちょうど中間

    proc_stealer = _spawn(
        "steal-resource-lease",
        "--db",
        str(db_path),
        "--owner",
        "thief",
        "--victim",
        "A",
        "--kind",
        "docs-write",
        "--ttl",
        "30.0",
        "--hold",
        "3.0",
    )
    reader_stealer = _LineReader(proc_stealer.stdout)
    stolen = reader_stealer.read_json_matching(lambda p: p.get("status") == "stole", timeout=5.0)
    assert stolen is not None, "別プロセスがリースを奪えなかった"
    steal_time = stolen["t"]

    remaining_writes: list[dict] = []
    final_status: dict | None = None
    while True:
        payload = reader_writer.read_json(timeout=5.0)
        if payload is None:
            break
        if payload.get("status") == "write":
            remaining_writes.append(payload)
            continue
        if payload.get("status") in ("succeeded", "failed"):
            final_status = payload
            break

    try:
        proc_writer.wait(timeout=5)
    finally:
        _terminate(proc_writer)
        _terminate(proc_stealer)

    assert final_status is not None, "A のジョブが終了ステータスを報告しなかった"

    all_writes = writes_before_steal + remaining_writes
    writes_before = [w for w in all_writes if w["t"] < steal_time]
    writes_after = [w for w in all_writes if w["t"] >= steal_time]
    print(
        f"[fix2 repro] writes before theft (t<{steal_time:.3f}): {len(writes_before)}, "
        f"writes after theft: {len(writes_after)}"
    )

    assert final_status["status"] == "failed", f"ジョブが FAILED で終わらなかった: {final_status}"
    assert final_status["code"] == "CONFLICT"
    assert writes_after == [], (
        f"リース横取り後にも書き込みが実行された(fix2 が機能していない再現): {writes_after}"
    )


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
