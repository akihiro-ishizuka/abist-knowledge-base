"""画面9: 設定・診断(§7.1)。パス、モデル、ffmpeg・Manim・FTS5診断。

ファイル名は組み込み `settings` モジュールとの衝突を避けるため `settings_page` とする。
"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.console.theme import SemanticToken
from abist_kb.presentation.web.theme import badge_label, token_color
from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell


def _badge(ok: bool) -> None:
    token = SemanticToken.SUCCESS if ok else SemanticToken.DANGER
    ui.badge(badge_label(token, "OK" if ok else "未検出"), color=token_color(token))


def render(container: ServiceContainer) -> None:
    with page_shell("/settings", title="設定・診断"):
        data = screens.settings_diagnostics(container)

        ui.label("パス").classes("text-md font-bold")
        for key, value in data["paths"].items():
            ui.label(f"{key}: {value}")

        ui.label(f"埋め込みモデル: {data['embedding_model']}").classes("mt-2")

        ui.label("診断").classes("text-md font-bold mt-4")
        diag = data["diagnostics"]
        with ui.row().classes("items-center gap-2"):
            ui.label("FTS5")
            _badge(diag["fts5_available"])
        with ui.row().classes("items-center gap-2"):
            ui.label("ffmpeg")
            _badge(diag["ffmpeg_available"])
        with ui.row().classes("items-center gap-2"):
            ui.label("Manim")
            _badge(diag["manim_available"])


__all__ = ["render"]
