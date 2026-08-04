"""画面5: 検索(§7.1)。ハイブリッド検索、スコア内訳、出典行、関連文書。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell


def render(container: ServiceContainer) -> None:
    with page_shell("/search", title="検索"):
        query_input = ui.input("検索クエリ")
        results_column = ui.column().classes("w-full mt-4")

        def do_search() -> None:
            results_column.clear()
            outcome = screens.search(container, query_input.value or "")
            with results_column:
                if "error" in outcome:
                    ui.label(outcome["error"]["message"]).classes("text-danger")
                    return
                for hit in outcome.get("results", []):
                    with ui.card():
                        ui.label(hit.get("path", "")).classes("font-bold")
                        ui.label(f"score: {hit.get('score')}")
                        ui.label(hit.get("snippet", ""))
                if outcome.get("note"):
                    ui.label(outcome["note"]).classes("text-sm text-grey")

        ui.button("検索", on_click=do_search)
        query_input.on("keydown.enter", lambda _e: do_search())


__all__ = ["render"]
