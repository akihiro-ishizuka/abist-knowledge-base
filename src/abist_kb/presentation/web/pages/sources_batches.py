"""画面2: ソース・バッチ(§7.1)。CRUD は既存 CLI と同じ `SourceService`/`BatchService`。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from ._confirm import confirm_dialog
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

        result_area = ui.column().classes("w-full")

        def make_run_handler(batch: dict):
            async def handler() -> None:
                detail_lines = [f"対象: バッチ {batch['name']}"]
                if batch.get("output_dir"):
                    detail_lines.append(f"出力先: {batch['output_dir']}")
                if not await confirm_dialog("実行しますか?", detail="\n".join(detail_lines)):
                    return

                outcome = screens.batch_run(container, batch["id"])
                result_area.clear()
                with result_area:
                    if "error" in outcome:
                        error = outcome["error"]
                        ui.label(f"エラー: {error['code']}").classes("text-danger")
                        ui.label(error["message"]).classes("text-danger")
                    else:
                        job = outcome["job"]
                        ui.label(f"実行完了: state={job['state']}")

            return handler

        ui.table(columns=batch_columns, rows=batches, row_key="id")
        for batch in batches:
            ui.button(f"{batch['name']} を実行", on_click=make_run_handler(batch))


__all__ = ["render"]
