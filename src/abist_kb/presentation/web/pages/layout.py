"""9画面共通のナビゲーション枠(設計書 §7.1)。

左ナビで9領域(ダッシュボード/ソース・バッチ/ジョブ/文書/検索/チャット/
可視化/品質/設定)を提供する。Textual TUI(§7.2)も同じ9領域を左ナビで
提供する設計であり、ここでの並び順・ラベルが将来の TUI 側の基準になる。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from nicegui import ui

from abist_kb.presentation.web.theme import apply_theme

NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("/", "ダッシュボード", "dashboard"),
    ("/sources", "ソース・バッチ", "source"),
    ("/jobs", "ジョブ", "work"),
    ("/documents", "文書", "description"),
    ("/search", "検索", "search"),
    ("/chat", "チャット", "chat"),
    ("/visualization", "可視化", "movie"),
    ("/quality", "品質", "verified"),
    ("/settings", "設定・診断", "settings"),
)


@contextmanager
def page_shell(active_path: str, *, title: str) -> Iterator[None]:
    """左ナビ付きの共通レイアウト。呼び出し側はこの `with` の中へ画面本体を書く。"""
    apply_theme(dark=False)
    with ui.header().classes("items-center justify-between"):
        ui.label(f"ABIST Knowledge Base — {title}").classes("text-lg font-bold")
        dark_toggle = ui.switch("ダーク")
        dark_toggle.on_value_change(lambda e: ui.dark_mode().set_value(e.value))

    with ui.left_drawer(fixed=True).classes("bg-slate-50") as drawer:
        drawer.props("width=220")
        for path, label, icon in NAV_ITEMS:
            with ui.row().classes("items-center"):
                ui.icon(icon)
                link = ui.link(label, path)
                if path == active_path:
                    link.classes("font-bold text-primary")

    with ui.column().classes("w-full p-4"):
        yield


__all__ = ["NAV_ITEMS", "page_shell"]
