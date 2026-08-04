"""画面8: 品質(§7.1)。整合性/重複/矛盾/メタデータ補完の監査4種(M7 task-2)を配線する。

いずれも「実行」ボタンで結果を表示するだけの読み取り中心の画面。
`backfill-metadata --apply` のような書込操作は §12 の確認作法(対象提示→確認→
`--yes`)を満たす専用 UI が要るため、この画面からは dry-run のみ実行できる
(`--apply` は CLI から行う)。
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell


def _render_result(container: ServiceContainer, result: dict[str, Any]) -> None:
    if "error" in result:
        ui.label(result["error"]["message"]).classes("text-sm text-red")
        return
    ui.label(f"監査実行ID: {result.get('run_id', result.get('mode'))}").classes("text-xs text-grey")
    totals = result.get("totals")
    if totals:
        ui.table(
            columns=[
                {"name": "key", "label": "項目", "field": "key"},
                {"name": "value", "label": "件数", "field": "value"},
            ],
            rows=[{"key": k, "value": v} for k, v in totals.items()],
        ).classes("w-full")


def render(container: ServiceContainer) -> None:
    with page_shell("/quality", title="品質"):
        screens.quality_stub(container)
        ui.label("品質監査").classes("text-md font-bold")
        ui.label("整合性・重複・矛盾・メタデータ補完(dry-run)を実行できます。").classes(
            "text-sm text-grey"
        )

        result_area = ui.column().classes("w-full")

        def _run(runner: Any) -> None:
            result_area.clear()
            result = runner(container)
            with result_area:
                _render_result(container, result)

        with ui.row():
            ui.button("整合性を検査", on_click=lambda: _run(screens.quality_run_integrity))
            ui.button("重複を検出", on_click=lambda: _run(screens.quality_run_duplicates))
            ui.button("矛盾候補を検出", on_click=lambda: _run(screens.quality_run_contradictions))


__all__ = ["render"]
