"""テンプレート共通: フォント解決・配色・出典フッタ・レイアウトユーティリティ

LaTeX（Tex / MathTex）は使わない。テキストは必ず Pango 系（Text / MarkupText）。
"""
from __future__ import annotations

import os

from manim import DOWN, VGroup, Text, config

# 既定は Windows 10/11 標準搭載の日本語フォント。優先順位:
#   spec["font"] > 環境変数 KB_VISUALIZE_FONT > DEFAULT_FONT（無ければ FALLBACK_FONT）
DEFAULT_FONT = "Yu Gothic UI"
FALLBACK_FONT = "Meiryo"

# 配色（テンプレート間で統一する）
COLOR_TITLE = "#ffffff"
COLOR_BODY = "#e6e6e6"
COLOR_ACCENT = "#5b9bd5"
COLOR_METRIC = "#f0c987"
COLOR_FOOTER = "#9a9a9a"
COLOR_BOX = "#5b9bd5"


def _installed_fonts() -> set[str]:
    try:
        import manimpango

        return set(manimpango.list_fonts())
    except Exception:
        return set()


def resolve_font(spec: dict) -> str:
    """spec / 環境変数 / 既定 の順でフォント名を決める。

    要求フォントが見つからなくても Pango 側のフォールバックで描画は続くため、
    ここでは名前の解決だけを行い、失敗にはしない。
    """
    requested = spec.get("font") or os.environ.get("KB_VISUALIZE_FONT") or DEFAULT_FONT
    fonts = _installed_fonts()
    if fonts and requested not in fonts and FALLBACK_FONT in fonts:
        return FALLBACK_FONT
    return requested


def source_footer(spec: dict, font: str) -> Text:
    """出典フッタ。sources の path:行範囲 を列挙する（重複は畳む）"""
    refs = sorted({f'{s["path"]}:{s["start_line"]}-{s["end_line"]}' for s in spec.get("sources", [])})
    text = "出典: " + " / ".join(refs) if refs else "出典なし（装飾のみ）"
    return Text(text, font=font, font_size=16, color=COLOR_FOOTER)


def fit_to_frame(group: VGroup, margin: float = 0.6) -> VGroup:
    """フレームからはみ出す場合だけ縮小する（拡大はしない）"""
    max_width = config.frame_width - margin * 2
    max_height = config.frame_height - margin * 2
    if group.width > max_width:
        group.scale_to_fit_width(max_width)
    if group.height > max_height:
        group.scale_to_fit_height(max_height)
    return group


def stack(*mobjects, buff: float = 0.5) -> VGroup:
    return VGroup(*mobjects).arrange(DOWN, buff=buff)
