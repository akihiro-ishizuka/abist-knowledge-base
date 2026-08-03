"""進捗表示(設計書 §6.2, §10)。

- `RICH`: `rich.progress.Progress` によるライブなバー/スピナー。終了時に
  静的な最終行へ置き換える(`transient=True` + 終了サマリ行)。
- `PLAIN`: 開始時に説明の1行、終了時に完了/失敗/経過のサマリ1行のみ。
  `\\r` もアニメーションも使わない。
- `JSON` または `quiet=True`: 何も出力しない(カウンタは内部で追跡し続ける)。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.presenter import Presenter


class ProgressHandle:
    """完了数・失敗数・総数を追跡するハンドル。

    既定実装は描画を行わない(JSON/quiet 用)。RICH 用の描画は
    `_RichProgressHandle` がフックを上書きして行う。
    """

    def __init__(self, total: int | None = None) -> None:
        self._completed = 0
        self._failed = 0
        self._total = total
        self._current_item: str | None = None

    @property
    def completed(self) -> int:
        return self._completed

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def total(self) -> int | None:
        return self._total

    def advance(self, n: int = 1, *, item: str | None = None) -> None:
        self._completed += n
        if item is not None:
            self._current_item = item
        self._on_advance()

    def set_total(self, n: int) -> None:
        self._total = n
        self._on_set_total()

    def fail(self, item: str | None = None) -> None:
        self._failed += 1
        if item is not None:
            self._current_item = item
        self._on_fail()

    def _on_advance(self) -> None:
        """フック: サブクラスがライブ表示を更新するために上書きする。"""

    def _on_set_total(self) -> None:
        """フック: サブクラスがライブ表示を更新するために上書きする。"""

    def _on_fail(self) -> None:
        """フック: サブクラスがライブ表示を更新するために上書きする。"""


class _RichProgressHandle(ProgressHandle):
    """`rich.progress.Progress` と連動して描画を更新するハンドル。"""

    def __init__(self, progress: Progress, task_id: Any, total: int | None) -> None:
        super().__init__(total)
        self._progress = progress
        self._task_id = task_id

    def _visual_completed(self) -> int:
        return self._completed + self._failed

    def _refresh(self) -> None:
        # 設計書 §6.2: 全体・現在項目・完了数・失敗数・経過時間・残り時間を表示する。
        # 現在項目は task の `current_item` フィールドとして流し込み、専用の
        # TextColumn で表示する。`total=None` は Rich 側で no-op(既存値を保持)。
        self._progress.update(
            self._task_id,
            completed=self._visual_completed(),
            total=self._total,
            current_item=self._current_item or "",
        )

    def _on_advance(self) -> None:
        self._refresh()

    def _on_set_total(self) -> None:
        self._refresh()

    def _on_fail(self) -> None:
        self._refresh()


def _plain_summary(handle: ProgressHandle, elapsed_seconds: float) -> str:
    return f"完了 {handle.completed}件 / 失敗 {handle.failed}件 / 経過 {elapsed_seconds:.1f}秒"


@contextmanager
def progress_scope(
    presenter: Presenter, *, description: str, total: int | None = None
) -> Iterator[ProgressHandle]:
    """進捗表示のコンテキストマネージャ。出力モードごとに描画方法を切り替える。"""
    if presenter.is_json or presenter.quiet:
        # JSON/quiet では進捗を一切出さない。カウンタだけは提供する。
        yield ProgressHandle(total)
        return

    if presenter.mode is OutputMode.PLAIN:
        handle = ProgressHandle(total)
        started = time.monotonic()
        presenter.info(description)
        try:
            yield handle
        finally:
            elapsed = time.monotonic() - started
            summary = _plain_summary(handle, elapsed)
            if handle.failed:
                presenter.warning(summary)
            else:
                presenter.success(summary)
        return

    # RICH: ライブなバー(総数あり)またはスピナー(総数不明)。
    # 設計書 §6.2 の「全体・現在項目・完了数・失敗数・経過時間・残り時間」に対応し、
    # 現在項目(advance/fail の item)を専用列として表示する。
    columns: list[Any] = [
        TextColumn("[progress.description]{task.description}"),
        SpinnerColumn() if total is None else BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        TextColumn("{task.fields[current_item]}"),
    ]
    progress = Progress(*columns, console=presenter.console, transient=True)
    started = time.monotonic()
    progress.start()
    task_id = progress.add_task(description, total=total, current_item="")
    handle = _RichProgressHandle(progress, task_id, total)
    try:
        yield handle
    finally:
        progress.stop()
        elapsed = time.monotonic() - started
        summary = f"{description}: {_plain_summary(handle, elapsed)}"
        if handle.failed:
            presenter.warning(summary)
        else:
            presenter.success(summary)
