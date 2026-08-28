"""chapter_card: 章扉。章番号 + 章タイトル + ねらい。

`chapter_index` / `chapter_total` は任意フィールド（SceneSpec 1.0 は未知フィールドを
拒否しないので、動画側だけが使う情報をここへ載せられる）。
"""

from __future__ import annotations

from manim import DOWN, LEFT, FadeIn, Line, Scene, Text, VGroup

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    card_title,
    content_width,
    fit_to_frame,
    hold_to,
    resolve_font,
    source_footer,
    wrapped_text,
)


def _chapter_label(spec: dict) -> str | None:
    index = spec.get("chapter_index")
    if not isinstance(index, int):
        return None
    total = spec.get("chapter_total")
    return f"第 {index} 章 / 全 {total} 章" if isinstance(total, int) else f"第 {index} 章"


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    notes = [b for b in spec["beats"] if b["type"] == "statement"]

    parts: list = []
    label = _chapter_label(spec)
    if label:
        parts.append(Text(label, font=font, font_size=24, color=COLOR_ACCENT))
    parts.append(card_title(spec["title"], font, font_size=46))
    parts.append(
        Line(
            [-content_width() / 2, 0, 0],
            [content_width() / 2, 0, 0],
            color=COLOR_ACCENT,
            stroke_width=2,
        )
    )
    if notes:
        parts.append(
            VGroup(
                *[wrapped_text(b["text"], font, 26, COLOR_BODY, content_width()) for b in notes]
            ).arrange(DOWN, aligned_edge=LEFT, buff=0.24)
        )
    parts.append(source_footer(spec, font))
    return fit_to_frame(VGroup(*parts).arrange(DOWN, buff=0.5))


def make_scene_classes(spec: dict):
    class ChapterCardAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            for part in layout:
                self.play(FadeIn(part, shift=DOWN * 0.15), run_time=0.5)
                self.wait(0.3)
            self.wait(1.0)
            hold_to(self, spec)

    class ChapterCardStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return ChapterCardAnim, ChapterCardStatic
