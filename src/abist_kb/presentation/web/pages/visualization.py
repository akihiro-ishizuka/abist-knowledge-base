"""画面7: 可視化(§7.1)。`SceneSpec`/Manim レンダリングは M7 で到着するため配線のみ・スタブ。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell


def render(container: ServiceContainer) -> None:
    with page_shell("/visualization", title="可視化"):
        data = screens.visualization_stub(container)
        ui.label("可視化は M7 で実装予定です。").classes("text-md font-bold")
        ui.label(data["reason"]).classes("text-sm text-grey")
        ui.textarea("SceneSpec (JSON)").props("disable")
        ui.button("レンダリング").props("disable")


__all__ = ["render"]
