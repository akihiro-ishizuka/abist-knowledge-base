"""画面6: チャット(§7.1)。`ChatService`(M7 task-1)へ配線する。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell


def render(container: ServiceContainer) -> None:
    with page_shell("/chat", title="チャット"):
        data = screens.chat_stub(container)
        if not data.get("available"):
            ui.label("チャットは利用できません。").classes("text-md font-bold")
            ui.label(data.get("reason", "")).classes("text-sm text-grey")
            return

        state: dict[str, str | None] = {"conversation_id": None}
        log = ui.column().classes("w-full gap-2").mark("chat-log")

        def _append(role: str, text: str) -> None:
            with log:
                ui.label(f"{role}: {text}").classes("text-sm")

        def _append_warnings(warnings: list[str]) -> None:
            for warning in warnings:
                with log:
                    ui.label(f"⚠ {warning}").classes("text-sm text-orange")

        def _send() -> None:
            question = message_input.value
            if not question:
                return
            if state["conversation_id"] is None:
                started = screens.chat_start(container)
                if "error" in started:
                    _append("エラー", started["error"]["message"])
                    return
                state["conversation_id"] = started["conversation_id"]
            _append("あなた", question)
            message_input.value = ""
            result = screens.chat_ask(
                container, conversation_id=state["conversation_id"], question=question
            )
            if "error" in result:
                _append("エラー", result["error"]["message"])
                return
            _append("アシスタント", result["text"])
            # 検証に失敗した引用は黙って落とさず、そのまま提示する(§7.1, task-1)。
            _append_warnings(result["citation_warnings"])

        with ui.row().classes("w-full"):
            message_input = ui.input("メッセージ").classes("flex-grow").mark("chat-input")
            ui.button("送信", on_click=_send).mark("chat-send")


__all__ = ["render"]
