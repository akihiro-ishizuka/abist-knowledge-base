"""thumbnail_card: サムネイル用の1枚絵（PNG 専用の想定）。

これまでサムネイルは動画から適当な時刻のフレームを切り出すだけで、**設計されて
いなかった**。一覧に並んだときに何の動画か分かることがサムネイルの仕事なので、
本文レイアウトとは別に「大きな題字 + アクセント + 出典なし」の専用面を持つ。

出典フッタは載せない: サムネイルは動画の内容そのものではなく看板で、小さく表示
されると出典は判読できない。事実の主張もしないので `decorative` 前提。
"""

from __future__ import annotations

from manim import DOWN, LEFT, Rectangle, Scene, Text, VGroup, config

from templates.base import (
    content_width,
    fit_to_frame,
    is_portrait,
    resolve_font,
    resolve_theme,
    wrapped_text,
)

#: 題字の基準サイズ。一覧で縮小されても読める大きさ。
TITLE_FONT_SIZE = 72
#: 副題のサイズ。
SUBTITLE_FONT_SIZE = 30
#: アクセントバーの太さ。
ACCENT_BAR_HEIGHT = 0.12


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    theme = resolve_theme(spec)
    statements = [b for b in spec["beats"] if b["type"] == "statement"]

    size = TITLE_FONT_SIZE * (0.72 if is_portrait() else 1.0)
    title = wrapped_text(
        spec["title"], font, size, theme.title, content_width(1.0), weight="BOLD"
    )
    accent = Rectangle(
        width=min(title.width, content_width(1.0)),
        height=ACCENT_BAR_HEIGHT,
        stroke_width=0,
        fill_color=theme.accent,
        fill_opacity=1.0,
    )

    parts: list = [title, accent]
    for beat in statements[:2]:
        parts.append(
            wrapped_text(beat["text"], font, SUBTITLE_FONT_SIZE, theme.body, content_width(1.0))
        )
    layout = VGroup(*parts).arrange(DOWN, buff=0.45, aligned_edge=LEFT)
    return fit_to_frame(layout, margin=0.9)


def make_scene_classes(spec: dict):
    class ThumbnailAnim(Scene):
        def construct(self):
            self.add(build_final_layout(spec))
            self.wait(0.1)

    class ThumbnailStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return ThumbnailAnim, ThumbnailStatic
