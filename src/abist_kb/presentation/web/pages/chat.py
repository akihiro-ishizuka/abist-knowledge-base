"""画面6: チャット(§7.1)。`ChatService` は M7 で到着するため配線のみ・スタブ表示。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell


def render(container: ServiceContainer) -> None:
    with page_shell("/chat", title="チャット"):
        data = screens.chat_stub(container)
        ui.label("チャットは M7 で実装予定です。").classes("text-md font-bold")
        ui.label(data["reason"]).classes("text-sm text-grey")
        ui.input("メッセージ").props("disable")
        ui.button("送信").props("disable")


__all__ = ["render"]
