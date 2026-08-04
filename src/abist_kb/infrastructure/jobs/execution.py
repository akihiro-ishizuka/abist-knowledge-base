"""ジョブ実行の共通経路(設計書 §10.1, §10.2, §10.3)。

`JobService.run_inline`(インライン実行)と `WorkerSupervisor`(キュー経由実行)は
どちらもここで定義する `run_job` を経由してハンドラを呼ぶ。以前はこのロジック
(emit の配線・resource lease の取得・成功/失敗時の `finish`)が2箇所に重複しており、
キュー経由の実行(`--detach` と全ての常駐ワーカー)だけが resource lease を
一切取得しないという致命的な差異を生んでいた(コードレビュー Critical 2)。
以後は本モジュールが唯一の実行経路であり、修正はここ1箇所で両方に効く。

**リースの自動更新(コードレビュー Critical 1 / Important 4)**:
以前は `acquire_resource_lease` が取得時に一度だけ `expires_at` を設定し、以後は
ハンドラが `emit()` を呼んだときに(ジョブの heartbeat だけを)更新していた。
このため ``(a)`` ハンドラが `emit()` を呼ばずに長時間動く(あるいは進捗報告の
間隔が TTL より長い)と、他プロセスが同じリソースを「空いている」と誤認して
同時に取得してしまい、``(b)`` 長時間ジョブを実行中の Supervisor はリーダー
シップ(`worker_leases`)自体を更新できず、TTL を超えると別プロセスに
乗っ取られる。どちらも「ハンドラが自発的に何かを呼ぶこと」に生存確認を依存
させているのが根本原因であり、根本原因が同じなので修正も1つにまとめる:
`run_job` は専用のバックグラウンドスレッドを起動し、ハンドラの実行中ずっと
TTL の1/3ごとに、ジョブの heartbeat・(あれば)resource lease・
(Supervisor 経由なら)worker lease を独立して更新し続ける。ハンドラが
`emit()` を一度も呼ばなくても、このスレッドがある限りリースは生き続ける。

更新が失敗した場合(=他プロセスに奪われた後、という意味)は握り潰さず、
`with` を抜けるときに `AppError(ErrorCode.CONFLICT)` として送出する。これにより
「リースを失ったジョブがそのまま成功したことにされる」事態を防ぐ
(呼び出し側は他の失敗と同様、ジョブを `failed` として記録する)。
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.job import (
    Job,
    JobState,
    ProgressEvent,
    ResourceRequirement,
    Severity,
)
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs import events as events_mod
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.repository import JobRepository

logger = logging.getLogger(__name__)

RENEWAL_FRACTION = 1.0 / 3.0
"""TTL のうちどれだけ進むごとに更新するか。1/3 ごとに更新すれば、1回分の更新が
(スレッド遅延やGCポーズなどで)たまたま遅れても TTL 内に次の更新が間に合う。"""

_MIN_RENEWAL_INTERVAL_SECONDS = 0.01
"""極端に小さい TTL を渡すテストでもビジーループにならないための下限。"""

RenewFn = Callable[[sqlite3.Connection], bool]
"""専用接続を受け取り、更新できたら `True`、既に他者に奪われていたら `False` を返す。"""


@contextlib.contextmanager
def _no_resource_lease() -> Iterator[None]:
    yield None


def _connection_db_path(conn: sqlite3.Connection) -> Path:
    """既存の接続が開いているファイルパスを取得する(`PRAGMA database_list` 経由)。

    バックグラウンド更新スレッドは呼び出し元の `conn` をそのまま使えない
    (Python の `sqlite3` はデフォルトで生成スレッド以外からの利用を禁じる、
    `check_same_thread` 違反になる)ため、同じDBファイルへ専用の接続を別途開く。
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2])


class _LeaseRenewalThread:
    """`with` の間ずっと、TTLの1/3ごとに `renew_fns` を専用接続で呼び続ける。

    いずれかの更新関数が `False`(=奪われた)を返す、または `AppError` を送出したら
    それ以上の更新を諦めて停止し、`raise_if_failed()` が呼ばれた時点でその失敗を
    送出できるよう保持しておく。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        ttl_seconds: float,
        renew_fns: list[RenewFn],
    ) -> None:
        self._db_path = _connection_db_path(conn)
        self._ttl = ttl_seconds
        self._interval = max(ttl_seconds * RENEWAL_FRACTION, _MIN_RENEWAL_INTERVAL_SECONDS)
        self._renew_fns = renew_fns
        self._stop_event = threading.Event()
        self._error: AppError | None = None
        self._thread = threading.Thread(target=self._run, name="lease-renewal", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        # ttl 以上待っても終わらないのは異常だが、テスト起因のハングでCIを
        # 無限に止めないよう、余裕を持たせた上限で必ず戻る。
        self._thread.join(timeout=max(self._ttl * 4, 5.0))

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error

    def check_alive(self) -> None:
        """ハンドラが協調的に呼ぶ用: 既に更新が失敗していれば直ちに送出する。

        `self._error` への代入(このスレッド)と読み取り(ハンドラを実行する
        呼び出し元スレッド)がスレッドをまたぐが、CPythonのGILの下では単純な
        属性の代入/参照は不可分であり、ロック無しでも「化けた値」を読むことは
        ない(高々1回の更新周期分だけ検知が遅れうるだけで、それは
        `RENEWAL_FRACTION` の設計上織り込み済み)。
        """
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        conn = connect(self._db_path)
        try:
            while not self._stop_event.wait(self._interval):
                if not self._renew_all(conn):
                    return
        finally:
            conn.close()

    def _renew_all(self, conn: sqlite3.Connection) -> bool:
        for renew_fn in self._renew_fns:
            try:
                ok = renew_fn(conn)
            except AppError as exc:
                self._error = exc
                return False
            if not ok:
                self._error = AppError(
                    code=ErrorCode.CONFLICT,
                    message=(
                        "実行中のジョブのリース更新に失敗しました"
                        "(他のプロセスにリースを奪われた可能性があります)。"
                    ),
                    retryable=False,
                    exit_code=ExitCode.CONFLICT,
                )
                return False
        return True


@contextmanager
def _lease_renewal(
    conn: sqlite3.Connection, *, ttl_seconds: float, renew_fns: list[RenewFn]
) -> Iterator[_LeaseRenewalThread]:
    """`with` の間、`renew_fns` をバックグラウンドスレッドで更新し続ける。

    `with` の本体(ハンドラ呼び出し)が例外を出さずに終わった場合のみ、更新が
    一度でも失敗していれば `AppError` を送出する。本体が独自の例外を出した場合は
    そちらをそのまま伝える(リース喪失より先に起きた本来の失敗を隠さないため)。

    呼び出し元へ `_LeaseRenewalThread` 自体を渡す(`check_alive` 経由で
    `JobRunContext.check_lease()` を配線するため、fix2: ハンドラ実行中に協調的に
    生存確認できるようにする)。
    """
    renewer = _LeaseRenewalThread(conn, ttl_seconds=ttl_seconds, renew_fns=renew_fns)
    renewer.start()
    try:
        yield renewer
    except BaseException:
        renewer.stop()
        raise
    else:
        renewer.stop()
        renewer.raise_if_failed()


def _build_renew_fns(
    *,
    job_id: str,
    owner_id: str,
    ttl_seconds: float,
    resource_key_value: str | None,
    renew_worker_lease: bool,
) -> list[RenewFn]:
    fns: list[RenewFn] = [
        lambda c: JobRepository(c).renew_heartbeat(job_id, owner_id, ttl_seconds=ttl_seconds)
    ]
    if resource_key_value is not None:
        fns.append(
            lambda c: leases.renew_resource_lease(
                c, resource_key_value, owner_id, ttl_seconds=ttl_seconds
            )
        )
    if renew_worker_lease:
        fns.append(lambda c: leases.renew_worker_lease(c, owner_id, ttl_seconds=ttl_seconds))
    return fns


def run_job(
    conn: sqlite3.Connection,
    repo: JobRepository,
    event_bus: events_mod.EventBus,
    job: Job,
    handler: Callable[..., None],
    *,
    owner_id: str,
    ttl_seconds: float,
    resource: ResourceRequirement | None,
    renew_worker_lease: bool = False,
    reraise: bool = True,
) -> Job:
    """resource lease の取得・自動更新・emit配線・`finish` をまとめて行う。

    `JobService.run_inline` と `WorkerSupervisor` の両方から呼ばれる唯一の
    実行経路(重複させないための共通化、コードレビュー Critical 2)。

    `renew_worker_lease=True` を渡すと、ハンドラ実行中も `worker_leases` の
    リーダーシップを TTL の1/3ごとに更新し続ける(コードレビュー Important 4:
    `WorkerSupervisor.tick()` は以前、ジョブ実行前に一度だけリーダーシップを
    更新した後は同期的にジョブを実行し切っており、TTL より長いジョブの間に
    リーダーシップが失効しうる不具合があった)。`JobService.run_inline` は
    リーダー選出に参加しないため常に `False` を渡す。

    `reraise=True`(既定、`JobService.run_inline` 用)はハンドラ・リース喪失に
    よる失敗を `finish(FAILED)` の記録後にそのまま再送出する(CLIの終了コードに
    直結させるため)。`reraise=False`(`WorkerSupervisor` 用)は記録するだけで
    再送出しない(ハンドラのバグでワーカー自体を落とさないため)。

    **fix2: リース喪失は「事後」にしか検知できない場合がある**。バックグラウンド
    更新スレッドがリース喪失を検知しても、実行中のハンドラを Python から安全に
    強制中断する手段は無いため、`run_job` はハンドラが戻ってくるまで
    `AppError(CONFLICT)` を送出できない。複数回の副作用(1件ずつの書き込み等)を
    繰り返すハンドラは、この「事後検知」だけに頼ると横取り後も副作用を出し
    続けてしまう。そのためハンドラには `JobRunContext.check_lease()` という
    協調的なポーリング手段を渡す(このメソッド自身のdocstring参照)。各反復の
    間で呼ぶことで、事後検知を待たずに早期に打ち切れる。ここでの事後検知は、
    `check_lease()` を呼ばない(または呼べない)ハンドラのためのバックストップ
    として引き続き機能する。
    """
    from abist_kb.infrastructure.jobs.supervisor import JobRunContext  # 循環import回避

    def emit(
        *,
        phase: str,
        current: int | None = None,
        total: int | None = None,
        message: str = "",
        severity: Severity = Severity.INFO,
        item: str | None = None,
    ) -> None:
        event = ProgressEvent(
            job_id=job.id,
            phase=phase,
            current=current,
            total=total,
            message=message,
            severity=severity,
            item=item,
        )
        events_mod.append_event(conn, event)
        event_bus.publish(event)

    resource_cm = (
        leases.acquire_resource_lease(
            conn,
            resource[0],
            key=resource[1],
            owner_id=owner_id,
            ttl_seconds=ttl_seconds,
            job_id=job.id,
        )
        if resource is not None
        else _no_resource_lease()
    )

    run_ctx = JobRunContext(job=job, emit=emit)
    try:
        with resource_cm as resource_key_value:
            renew_fns = _build_renew_fns(
                job_id=job.id,
                owner_id=owner_id,
                ttl_seconds=ttl_seconds,
                resource_key_value=resource_key_value,
                renew_worker_lease=renew_worker_lease,
            )
            with _lease_renewal(conn, ttl_seconds=ttl_seconds, renew_fns=renew_fns) as renewer:
                run_ctx._check_lease = renewer.check_alive
                handler(run_ctx)
    except AppError as exc:
        repo.finish(job.id, state=JobState.FAILED, error=exc.to_dict())
        if reraise:
            raise
    except Exception as exc:  # ハンドラのバグ等
        if not reraise:
            logger.exception("job_handler_failed", extra={"job_id": job.id, "kind": job.kind})
        repo.finish(job.id, state=JobState.FAILED, error={"code": "FAILURE", "message": str(exc)})
        if reraise:
            raise
    else:
        # ハンドラは例外を出さずに戻ったが、`JobRunContext.finish_as()` で
        # SUCCEEDED 以外の終端状態(例: PARTIAL)や結果ペイロードを宣言している
        # 場合がある(`SyncService.sync_all` の partial-failure 方針、
        # `render_scene` ジョブの出力先情報参照)。
        state = run_ctx._finish_state or JobState.SUCCEEDED
        repo.finish(
            job.id, state=state, result=run_ctx._finish_result, error=run_ctx._finish_error
        )
    result = repo.get(job.id)
    assert result is not None  # 直前に finish した行なので必ず存在する
    return result


__all__ = ["RENEWAL_FRACTION", "RenewFn", "run_job"]
