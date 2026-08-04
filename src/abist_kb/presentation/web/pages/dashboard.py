"""画面1: ダッシュボード(§7.1)。文書数、索引鮮度、直近ジョブ、警告。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.console.theme import SemanticToken
from abist_kb.presentation.web.theme import badge_label, token_color
from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell


def render(container: ServiceContainer) -> None:
    with page_shell("/", title="ダッシュボード"):
        data = screens.dashboard(container)

        with ui.row().classes("gap-4"):
            with ui.card():
                ui.label("文書数").classes("text-sm text-grey")
                ui.label(str(data["document_count"])).classes("text-2xl font-bold")
            with ui.card():
                ui.label("ソース別").classes("text-sm text-grey")
                for source, count in data["document_count_by_source"].items():
                    ui.label(f"{source}: {count}")

        ui.label("索引状態").classes("text-md font-bold mt-4")
        with ui.row().classes("gap-4"):
            for corpus, status in data["index_corpora"].items():
                with ui.card():
                    ui.label(corpus)
                    if status.get("available"):
                        ui.badge(
                            badge_label(SemanticToken.SUCCESS, "利用可能"),
                            color=token_color(SemanticToken.SUCCESS),
                        )
                        ui.label(f"文書 {status.get('documents', 0)} 件")
                    else:
                        ui.badge(
                            badge_label(SemanticToken.MUTED, "未構築"),
                            color=token_color(SemanticToken.MUTED),
                        )

        if data["warnings"]:
            ui.label("警告").classes("text-md font-bold mt-4 text-warning")
            for warning in data["warnings"]:
                token = SemanticToken(warning["state_token"])
                with ui.row().classes("items-center gap-2"):
                    ui.badge(badge_label(token, warning["state"]), color=token_color(token))
                    ui.label(f"{warning['kind']} ({warning['job_id']})")
                    ui.link("詳細", f"/jobs/{warning['job_id']}")

        ui.label("直近のジョブ").classes("text-md font-bold mt-4")
        columns = [
            {"name": "kind", "label": "種別", "field": "kind"},
            {"name": "state", "label": "状態", "field": "state"},
            {"name": "created_at", "label": "作成日時", "field": "created_at"},
        ]
        ui.table(columns=columns, rows=data["recent_jobs"], row_key="id")


__all__ = ["render"]
