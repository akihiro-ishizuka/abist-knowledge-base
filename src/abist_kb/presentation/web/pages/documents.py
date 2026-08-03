"""画面4: 文書(§7.1)。コーパス／状態／ソースの絞り込み、Markdown詳細、メタデータ。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

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
            ui.label(data["error"]["message"]).classes("text-danger")
            return

        record = data["document"]
        ui.label(f"path: {record['path']}").classes("text-sm text-grey")
        ui.label(f"source: {record.get('source')} / status: {record.get('status')}")

        if data["body_missing"]:
            ui.label("本文ファイルが見つかりません(索引/DBの記録とファイルの不一致)。")
        elif data["body_html"] is not None:
            # サニタイズ済みHTML(`viewmodels/markdown_render.py`)のみ描画する(§12)。
            ui.html(data["body_html"])


__all__ = ["render_detail", "render_list"]
