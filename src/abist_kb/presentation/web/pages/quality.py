"""画面8: 品質(§7.1)。整合性/重複/矛盾/検索評価の監査4種は M7(Task 7.2)で到着するためスタブ。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell


def render(container: ServiceContainer) -> None:
    with page_shell("/quality", title="品質"):
        data = screens.quality_stub(container)
        ui.label("品質監査は M7 で実装予定です。").classes("text-md font-bold")
        ui.label(data["reason"]).classes("text-sm text-grey")


__all__ = ["render"]
