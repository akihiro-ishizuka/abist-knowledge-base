"""NiceGUI の破壊的操作向け確認ダイアログ。"""

from __future__ import annotations

from nicegui import ui


async def confirm_dialog(message: str, *, detail: str = "", danger: bool = True) -> bool:
    """対象の詳細を先に示し、明示的な「はい」の場合だけ ``True`` を返す。"""
    with ui.dialog() as dialog, ui.card().classes("min-w-96"):
        if detail:
            ui.label(detail).classes("whitespace-pre-line text-sm")
        ui.label(message).classes("text-base font-bold")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("いいえ", on_click=lambda: dialog.submit(False)).props("autofocus")
            ui.button(
                "はい",
                on_click=lambda: dialog.submit(True),
                color="negative" if danger else "primary",
            )
    return await dialog is True


__all__ = ["confirm_dialog"]
