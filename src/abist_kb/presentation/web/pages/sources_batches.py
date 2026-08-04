"""画面2: ソース・バッチ(§7.1)。CRUD は既存 CLI と同じ `SourceService`/`BatchService`。"""

from __future__ import annotations

import json
from typing import Any

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from ._confirm import confirm_dialog
from .layout import page_shell


def render(container: ServiceContainer) -> None:
    with page_shell("/sources", title="ソース・バッチ"):
        result_area = ui.column().classes("w-full")

        def show_result(outcome: dict[str, Any], success_message: str) -> bool:
            result_area.clear()
            with result_area:
                if "error" in outcome:
                    error = outcome["error"]
                    ui.label(f"エラー: {error['code']}").classes("text-danger")
                    ui.label(error["message"]).classes("text-danger")
                    return False
                if "ok" in outcome:
                    status = "成功" if outcome["ok"] else "失敗"
                    ui.label(f"接続テスト: {status}")
                    ui.label(str(outcome.get("detail", "")))
                    return bool(outcome["ok"])
                ui.label(success_message)
                return True

        def selected(table: Any, kind: str) -> dict[str, Any] | None:
            if table.selected:
                return table.selected[0]
            show_result(
                {
                    "error": {
                        "code": "INVALID_INPUT",
                        "message": f"{kind}を選択してください。",
                    }
                },
                "",
            )
            return None

        ui.label("ソース").classes("text-md font-bold")
        sources = screens.sources_list(container)["sources"]
        source_columns = [
            {"name": "display_name", "label": "名前", "field": "display_name"},
            {"name": "type", "label": "種別", "field": "type"},
            {"name": "enabled", "label": "有効", "field": "enabled"},
        ]
        source_table = ui.table(
            columns=source_columns,
            rows=sources,
            row_key="id",
            selection="single",
        ).mark("sources-table")

        def open_source_dialog(source: dict[str, Any] | None = None) -> None:
            with ui.dialog() as dialog, ui.card().classes("min-w-96"):
                ui.label("ソース編集" if source else "ソース追加").classes("text-lg font-bold")
                display_name = ui.input(
                    "表示名", value=str(source.get("display_name", "")) if source else ""
                ).mark("source-display-name")
                source_type = ui.select(
                    ["esa", "web", "git"],
                    label="種別",
                    value=str(source.get("type", "web")) if source else "web",
                )
                output_dir = ui.input(
                    "出力先", value=str(source.get("output_dir", "")) if source else ""
                ).mark("source-output-dir")
                connection = ui.textarea(
                    "接続設定 (JSON)",
                    value=json.dumps(source.get("connection") or {}, ensure_ascii=False)
                    if source
                    else "{}",
                )
                enabled = ui.switch(
                    "有効", value=bool(source.get("enabled", True)) if source else True
                )

                def save() -> None:
                    try:
                        parsed_connection = json.loads(connection.value or "{}")
                        if not isinstance(parsed_connection, dict):
                            raise ValueError("JSON オブジェクトを指定してください。")
                    except (json.JSONDecodeError, ValueError) as exc:
                        show_result(
                            {
                                "error": {
                                    "code": "INVALID_INPUT",
                                    "message": f"接続設定が不正です: {exc}",
                                }
                            },
                            "",
                        )
                        return
                    if source:
                        outcome = screens.source_edit(
                            container,
                            source["id"],
                            {
                                "display_name": display_name.value,
                                "type": source_type.value,
                                "output_dir": output_dir.value,
                                "connection": parsed_connection,
                                "enabled": enabled.value,
                            },
                        )
                        success = show_result(outcome, "ソースを更新しました。")
                        if success:
                            source_table.update_rows(screens.sources_list(container)["sources"])
                    else:
                        outcome = screens.source_add(
                            container,
                            display_name=display_name.value,
                            type=source_type.value,
                            output_dir=output_dir.value,
                            connection=parsed_connection,
                            enabled=enabled.value,
                        )
                        success = show_result(outcome, "ソースを追加しました。")
                        if success:
                            source_table.add_row(outcome["source"])
                    if success:
                        dialog.close()

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("キャンセル", on_click=dialog.close)
                    ui.button("保存", on_click=save)
            dialog.open()

        async def remove_source() -> None:
            source = selected(source_table, "ソース")
            if source is None:
                return
            detail = (
                f"対象: ソース {source['display_name']}\n"
                f"出力先: {source.get('output_dir') or '未設定'}"
            )
            if not await confirm_dialog("削除しますか?", detail=detail):
                return
            outcome = screens.source_remove(container, source["id"], confirmed=True)
            if show_result(outcome, "ソースを削除しました。"):
                source_table.remove_row(source)

        def edit_source() -> None:
            source = selected(source_table, "ソース")
            if source is not None:
                open_source_dialog(source)

        def test_source_connection() -> None:
            source = selected(source_table, "ソース")
            if source is not None:
                show_result(
                    screens.source_test_connection(container, source["id"]),
                    "接続テストが完了しました。",
                )

        with ui.row().classes("gap-2"):
            ui.button("ソース追加", on_click=lambda: open_source_dialog())
            ui.button("ソース編集", on_click=edit_source)
            ui.button("ソース削除", on_click=remove_source, color="negative")
            ui.button("接続テスト", on_click=test_source_connection)

        ui.label("バッチ").classes("text-md font-bold mt-4")
        batches = screens.batches_list(container)["batches"]
        batch_columns = [
            {"name": "name", "label": "名前", "field": "name"},
            {"name": "type", "label": "種別", "field": "type"},
            {"name": "enabled", "label": "有効", "field": "enabled"},
        ]

        batch_table = ui.table(
            columns=batch_columns,
            rows=batches,
            row_key="id",
            selection="single",
        ).mark("batches-table")

        def open_batch_dialog(batch: dict[str, Any] | None = None) -> None:
            with ui.dialog() as dialog, ui.card().classes("min-w-96"):
                ui.label("バッチ編集" if batch else "バッチ追加").classes("text-lg font-bold")
                name = ui.input(
                    "名前", value=str(batch.get("name", "")) if batch else ""
                ).mark("batch-name")
                batch_type = ui.select(
                    ["esa", "web", "git"],
                    label="種別",
                    value=str(batch.get("type", "web")) if batch else "web",
                )
                output_dir = ui.input(
                    "出力先", value=str(batch.get("output_dir") or "") if batch else ""
                )
                items = ui.textarea(
                    "対象一覧 (JSON)",
                    value=json.dumps(batch.get("items") or [], ensure_ascii=False)
                    if batch
                    else "[]",
                ).mark("batch-items")
                enabled = ui.switch(
                    "有効", value=bool(batch.get("enabled", True)) if batch else True
                )

                def save() -> None:
                    try:
                        parsed_items = json.loads(items.value or "[]")
                        if not isinstance(parsed_items, list):
                            raise ValueError("JSON 配列を指定してください。")
                        if not all(isinstance(item, dict) for item in parsed_items):
                            raise ValueError(
                                "対象一覧の各要素は JSON オブジェクトを指定してください。"
                            )
                    except (json.JSONDecodeError, ValueError) as exc:
                        show_result(
                            {
                                "error": {
                                    "code": "INVALID_INPUT",
                                    "message": f"対象一覧が不正です: {exc}",
                                }
                            },
                            "",
                        )
                        return
                    if batch:
                        outcome = screens.batch_edit(
                            container,
                            batch["id"],
                            {
                                "name": name.value,
                                "type": batch_type.value,
                                "output_dir": output_dir.value or None,
                                "items": parsed_items,
                                "enabled": enabled.value,
                            },
                        )
                        success = show_result(outcome, "バッチを更新しました。")
                        if success:
                            batch_table.update_rows(screens.batches_list(container)["batches"])
                    else:
                        outcome = screens.batch_add(
                            container,
                            name=name.value,
                            type=batch_type.value,
                            output_dir=output_dir.value or None,
                            items=parsed_items,
                            enabled=enabled.value,
                        )
                        success = show_result(outcome, "バッチを追加しました。")
                        if success:
                            batch_table.add_row(outcome["batch"])
                    if success:
                        dialog.close()

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("キャンセル", on_click=dialog.close)
                    ui.button("保存", on_click=save)
            dialog.open()

        def edit_batch() -> None:
            batch = selected(batch_table, "バッチ")
            if batch is not None:
                open_batch_dialog(batch)

        async def remove_batch() -> None:
            batch = selected(batch_table, "バッチ")
            if batch is None:
                return
            detail = (
                f"対象: バッチ {batch['name']}\n"
                f"出力先: {batch.get('output_dir') or '未設定'}"
            )
            if not await confirm_dialog("削除しますか?", detail=detail):
                return
            outcome = screens.batch_remove(container, batch["id"], confirmed=True)
            if show_result(outcome, "バッチを削除しました。"):
                batch_table.remove_row(batch)

        async def run_batch() -> None:
            batch = selected(batch_table, "バッチ")
            if batch is None:
                return
            detail = (
                f"対象: バッチ {batch['name']}\n"
                f"出力先: {batch.get('output_dir') or '未設定'}"
            )
            if not await confirm_dialog("実行しますか?", detail=detail, danger=False):
                return
            outcome = screens.batch_run(container, batch["id"])
            if "job" in outcome:
                job = outcome["job"]
                show_result(outcome, f"実行完了: state={job['state']}")
            else:
                show_result(outcome, "")

        with ui.row().classes("gap-2"):
            ui.button("バッチ追加", on_click=lambda: open_batch_dialog())
            ui.button("バッチ編集", on_click=edit_batch)
            ui.button("バッチ削除", on_click=remove_batch, color="negative")
            ui.button("バッチ実行", on_click=run_batch)


__all__ = ["render"]
