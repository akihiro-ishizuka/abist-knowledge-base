"""`WorkerSupervisor`(設計書 §10.1, §10.3)。

正の起動主体は `abist-kb worker run`。リーダー選出を試み、リーダーだけが
キューを消費する。heartbeat 5秒、lease 15秒(既定値。テストは実行時間短縮の
ため上書きする)。

歴史メモ: 初版設計では Web／TUI／MCP の長時間エントリも起動時に Supervisor
を開始する想定だったが、MCP-only cutover 後は `worker run` が担う。
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from abist_kb.domain.job import Job, JobState, ResourceRequirement
from abist_kb.infrastructure.jobs import events as events_mod
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.execution import run_job as _run_job_with_lease
from abist_kb.infrastructure.jobs.repository import JobRepository

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 5.0
"""設計書 §10.1 が定める既定の heartbeat 間隔。"""

WORKER_LEASE_TTL_SECONDS = 15.0
"""設計書 §10.1 が定める既定の lease 期限。"""


EmitFn = Callable[..., None]


@dataclass(slots=True)
class JobRunContext:
    """ジョブハンドラへ渡す実行コンテキスト。

    **リース生存確認の契約(重要、ハンドラ作成者は必読)**: `run_job`
    (`infrastructure.jobs.execution`)はバックグラウンドスレッドで resource
    lease / worker lease を TTL の1/3ごとに更新し続けるが、更新に失敗した
    (=他プロセスに奪われた)ことをハンドラの実行中に伝える手段は Python では
    安全な強制割り込みができないため存在しない。そのため検知は**協調的**
    (cooperative)であり、ハンドラ自身が `check_lease()` をポーリングする
    必要がある。

    - **複数回の副作用(ファイル書き込み・API呼び出し等)を繰り返すハンドラ**
      (バッチ内で1件ずつ書く sync、ツリーをファイル単位でミラーする git 連携、
      チャンクをループで書き込むインデクサ等)は、**各反復の間**(次の副作用を
      行う前)に必ず `check_lease()` を呼ぶこと。呼ばなければ、リースが奪われた
      後も検知されるまで副作用を出し続けてしまう(実際にレビューで、TTL内に
      横取りが起きたのに20回中16回の書き込みが横取り後に実行された事例がある)。
    - **一度きりの不可分な操作しか行わないハンドラ**は呼ぶ必要がない
      (`run_job` が `with` を抜ける際に最終確認するため、後述のバックストップで
      十分)。
    - `check_lease()` を一度も呼ばないハンドラでも、ジョブは最終的に
      `FAILED` として記録される(`run_job` がハンドラ終了後にバックグラウンド
      スレッドの失敗を検知して `AppError(CONFLICT)` を送出するため)。ただし
      それまでの副作用は止められない。
    """

    job: Job
    emit: EmitFn
    _check_lease: Callable[[], None] | None = None
    _finish_state: JobState | None = None
    _finish_result: dict[str, object] | None = None
    _finish_error: dict[str, object] | None = None

    def check_lease(self) -> None:
        """リース(resource lease / worker lease)が奪われていれば直ちに
        `AppError(ErrorCode.CONFLICT)` を送出する。

        属性1回分の読み取り程度で軽量なので、タイトなループ(1件ずつの書き込み
        ループ等)の中で毎回呼んでも支配的なコストにはならない。奪われていな
        ければ何もせずそのまま戻る。
        """
        if self._check_lease is not None:
            self._check_lease()

    def finish_as(
        self,
        state: JobState,
        *,
        result: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        """ハンドラが例外を出さずに終わっても `SUCCEEDED` 以外で終端させたい
        場合、または結果ペイロードを記録したい場合に呼ぶ(例: `sync all` で
        一部バッチが失敗したときの `PARTIAL`、`render_scene` の出力先情報)。

        `run_job`(`infrastructure.jobs.execution`)はハンドラが例外を出さずに
        戻ってきたとき既定で `state=SUCCEEDED`・`result=None` として `finish`
        するが、`SyncService.sync_all` のように「例外は投げないが完全成功でも
        ない」結果を返すハンドラや、`render_scene` のように呼び出し元へ返す
        べき結果(`visualization_id`/`output_dir` 等)を持つハンドラは、ここで
        宣言しないとジョブ行が実態と異なる `SUCCEEDED`/`result=None` のまま
        記録されてしまう(MCP・API・CLI 等、終了コードを見ない全ての消費者
        がそれを「クリーンな成功・結果なし」と誤読する)。
        `state=JobState.FAILED` を渡すことも可能(その場合は `error` を渡すこと
        を推奨する)。
        """
        self._finish_state = state
        self._finish_result = result
        self._finish_error = error


JobHandler = Callable[[JobRunContext], None]
"""ジョブ種別ごとのハンドラ。失敗は例外(`AppError` 推奨)で表す。

**複数回の副作用を行うハンドラは `JobRunContext.check_lease()` の契約を守る
こと**(`JobRunContext` のクラスdocstring参照)。各反復の間で呼ばなければ、
リースを奪われた後も検知されるまで副作用を出し続けてしまう。
"""


class WorkerSupervisor:
    """リーダー選出とキュー消費を担う。1プロセスにつき1インスタンス。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        owner_id: str | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        lease_ttl: float = WORKER_LEASE_TTL_SECONDS,
        handlers: dict[str, JobHandler] | None = None,
        resource_for_kind: dict[str, ResourceRequirement] | None = None,
        event_bus: events_mod.EventBus | None = None,
    ) -> None:
        self.owner_id = owner_id or str(uuid.uuid4())
        self._conn = conn
        self._heartbeat_interval = heartbeat_interval
        self._lease_ttl = lease_ttl
        self._handlers = dict(handlers or {})
        self._resource_for_kind = dict(resource_for_kind or {})
        self._event_bus = event_bus or events_mod.EventBus()
        self._repo = JobRepository(conn)
        self._is_leader = False
        self._stop_requested = False
        self._last_error: BaseException | None = None

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def last_error(self) -> BaseException | None:
        """直近の `tick()` が失敗していればその例外、成功していれば `None`。

        運用監視側が「ワーカーが繰り返し失敗している」ことを1箇所の状態として
        読むために使う想定(繰り返しログを無制限に流し込まないようにするため、
        ログ自体は `run_forever` 側で間引く)。
        """
        return self._last_error

    @property
    def events(self) -> events_mod.EventBus:
        return self._event_bus

    def register(self, kind: str, handler: JobHandler) -> None:
        self._handlers[kind] = handler

    def request_stop(self) -> None:
        self._stop_requested = True

    def _become_or_stay_leader(self) -> None:
        if self._is_leader:
            self._is_leader = leases.renew_worker_lease(
                self._conn, self.owner_id, ttl_seconds=self._lease_ttl
            )
        if not self._is_leader:
            self._is_leader = leases.try_acquire_worker_lease(
                self._conn, self.owner_id, ttl_seconds=self._lease_ttl
            )

    def tick(self) -> bool:
        """1周分の処理を行う。何らかの作業をした場合は `True` を返す。"""
        did_work = False
        self._become_or_stay_leader()
        if not self._is_leader:
            return did_work

        recovered = self._repo.recover_interrupted()
        if recovered:
            did_work = True
            logger.warning("interrupted_jobs_recovered", extra={"job_ids": recovered})

        job = self._repo.claim(
            self.owner_id, ttl_seconds=self._lease_ttl, resource_for_kind=self._resource_for_kind
        )
        if job is None:
            return did_work

        did_work = True
        self._run_job(job)
        return did_work

    def _run_job(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._repo.finish(
                job.id,
                state=JobState.FAILED,
                error={
                    "code": "UNKNOWN_JOB_KIND",
                    "message": f"未登録のジョブ種別です: {job.kind}",
                },
            )
            return

        resource = self._resource_for_kind.get(job.kind)
        # 実際の実行(resource lease 取得・自動更新・emit配線・finish)は
        # `JobService.run_inline` と共有する `execution.run_job` に委譲する
        # (コードレビュー Critical 2: 以前はここに同じロジックが重複しており、
        # インライン実行だけが resource lease を取得していた)。
        # `renew_worker_lease=True`: ハンドラ実行中もリーダーシップ
        # (`worker_leases`)を更新し続ける(Important 4)。
        # `reraise=False`: ハンドラのバグでもワーカー自体は落とさない。
        _run_job_with_lease(
            self._conn,
            self._repo,
            self._event_bus,
            job,
            handler,
            owner_id=self.owner_id,
            ttl_seconds=self._lease_ttl,
            resource=resource,
            renew_worker_lease=True,
            reraise=False,
        )

    def run_forever(self, *, poll_interval: float = 1.0, max_backoff_seconds: float = 30.0) -> None:
        """`tick()` をループする(`<cli> worker run` の実体)。

        ここで捕捉するのは `AppError` に限らない(`Exception` 全般)。ホストの
        接続設定に問題があるなど `tick()` が毎回同じ理由で失敗し続ける場合でも、
        ワーカーはあくまでバックグラウンドの1構成要素であり、その異常を理由に
        `worker run` プロセス全体を巻き込んで落としたり、ログを無制限に吐き続けて
        端末を使用不能にしたりしてはならない(歴史メモ: 旧 TUI でスレッド境界を
        越えた SQLite 接続共有が原因で `sqlite3.ProgrammingError` が毎tick発生し、
        無間隔でログが
        流れ続けて画面もキー入力も応答不能になった不具合の再発防止)。
        `KeyboardInterrupt`/`SystemExit` は `Exception` のサブクラスではないため
        素通しする。

        連続失敗時は指数バックオフ(`poll_interval` を基準に最大
        `max_backoff_seconds` まで)し、ログも初回とその後は間引いて出す
        (毎tickではなく30回に1回)ことで、原因調査に必要な情報は残しつつ
        フラッディングを防ぐ。
        """
        self._stop_requested = False
        consecutive_failures = 0
        while not self._stop_requested:
            try:
                did_work = self.tick()
            except Exception as exc:  # noqa: BLE001 - ホストUIを巻き込まないための最終防衛線
                consecutive_failures += 1
                self._last_error = exc
                if consecutive_failures == 1 or consecutive_failures % 30 == 0:
                    logger.warning(
                        "worker_tick_failed",
                        exc_info=True,
                        extra={"consecutive_failures": consecutive_failures},
                    )
                did_work = False
            else:
                consecutive_failures = 0
                self._last_error = None
            if not did_work:
                sleep_seconds = poll_interval
                if consecutive_failures:
                    sleep_seconds = min(
                        poll_interval * (2 ** (consecutive_failures - 1)), max_backoff_seconds
                    )
                time.sleep(sleep_seconds)


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "WORKER_LEASE_TTL_SECONDS",
    "JobHandler",
    "JobRunContext",
    "WorkerSupervisor",
]
