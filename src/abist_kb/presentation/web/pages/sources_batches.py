"""画面2: ソース・バッチ(§7.1)。CRUD は既存 CLI と同じ `SourceService`/`BatchService`。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell


def render(container: ServiceContainer) -> None:
    with page_shell("/sources", title="ソース・バッチ"):
        ui.label("ソース").classes("text-md font-bold")
        sources = screens.sources_list(container)["sources"]
        source_columns = [
            {"name": "display_name", "label": "名前", "field": "display_name"},
            {"name": "type", "label": "種別", "field": "type"},
            {"name": "enabled", "label": "有効", "field": "enabled"},
        ]
        ui.table(columns=source_columns, rows=sources, row_key="id")

        ui.label("バッチ").classes("text-md font-bold mt-4")
        batches = screens.batches_list(container)["batches"]
        batch_columns = [
            {"name": "name", "label": "名前", "field": "name"},
            {"name": "type", "label": "種別", "field": "type"},
            {"name": "enabled", "label": "有効", "field": "enabled"},
        ]

        result_label = ui.label("")

        def make_run_handler(batch_id: str):
            def handler() -> None:
                outcome = screens.batch_run(container, batch_id)
                if "error" in outcome:
                    result_label.text = f"失敗: {outcome['error']['message']}"
                else:
                    job = outcome["job"]
                    result_label.text = f"実行完了: state={job['state']}"

            return handler

        ui.table(columns=batch_columns, rows=batches, row_key="id")
        for batch in batches:
            ui.button(f"{batch['name']} を実行", on_click=make_run_handler(batch["id"]))


__all__ = ["render"]
