"""画面4: 文書(§7.1)。コーパス／状態／ソースの絞り込み、Markdown詳細、メタデータ。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from ._confirm import confirm_dialog
from .layout import page_shell


def render_list(container: ServiceContainer) -> None:
    with page_shell("/documents", title="文書"):
        data = screens.documents_list(container)
        columns = [
            {"name": "path", "label": "パス", "field": "path"},
            {"name": "source", "label": "ソース", "field": "source"},
            {"name": "sync_status", "label": "同期状態", "field": "sync_status"},
            {"name": "status", "label": "状態", "field": "status"},
        ]
        rows = data["documents"]
        table = ui.table(columns=columns, rows=rows, row_key="path")

        def open_selected(event) -> None:  # type: ignore[no-untyped-def]
            selected = event.args
            if selected:
                path = selected[0]["path"] if isinstance(selected, list) else selected["path"]
                ui.navigate.to(f"/documents/{path}")

        table.on("rowClick", open_selected)


def render_detail(container: ServiceContainer, path: str) -> None:
    with page_shell("/documents", title=f"文書: {path}"):
        data = screens.document_detail(container, path)
        if "error" in data:
            error = data["error"]
            ui.label(f"エラー: {error['code']}").classes("text-danger")
            ui.label(error["message"]).classes("text-danger")
            return

        record = data["document"]
        result_area = ui.column().classes("w-full")

        def show_result(outcome: dict, success_message: str) -> bool:
            result_area.clear()
            with result_area:
                if "error" in outcome:
                    error = outcome["error"]
                    ui.label(f"エラー: {error['code']}").classes("text-danger")
                    ui.label(error["message"]).classes("text-danger")
                    return False
                ui.label(success_message).classes("text-positive")
                return True

        ui.label("読み取り専用メタデータ").classes("text-sm font-bold text-grey")
        with ui.card().classes("w-full bg-grey-2"):
            for key, value in record.items():
                if key not in {"status", "document_type"}:
                    ui.label(f"{key}: {value}").classes("text-sm text-grey-8")

        ui.label("編集可能メタデータ").classes("text-sm font-bold")
        status = ui.input("status", value=str(record.get("status") or "")).mark(
            "document-status"
        )
        document_type = ui.input(
            "document_type", value=str(record.get("document_type") or "")
        ).mark("document-type")

        def save_metadata() -> None:
            outcome = screens.document_update_metadata(
                container,
                path,
                {
                    "status": status.value or None,
                    "document_type": document_type.value or None,
                },
            )
            if show_result(outcome, "メタデータを更新しました。"):
                ui.navigate.to(f"/documents/{path}")

        async def delete_document() -> None:
            if not await confirm_dialog(
                "削除しますか?",
                detail=f"対象パス: {record['path']}",
            ):
                return
            outcome = screens.document_delete(container, path, confirmed=True)
            if show_result(outcome, "文書を削除しました。"):
                ui.navigate.to("/documents")

        with ui.row().classes("gap-2"):
            ui.button("メタデータを保存", on_click=save_metadata)
            ui.button("文書を削除", on_click=delete_document, color="negative")

        if data["body_missing"]:
            ui.label("本文ファイルが見つかりません(索引/DBの記録とファイルの不一致)。")
        elif data["body_html"] is not None:
            # サニタイズ済みHTML(`viewmodels/markdown_render.py`)のみ描画する(§12)。
            ui.html(data["body_html"])


__all__ = ["render_detail", "render_list"]
