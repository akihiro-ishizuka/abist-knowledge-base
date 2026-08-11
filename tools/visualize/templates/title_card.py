"""title_card: 動画の表紙。大見出し + 補足 + 出典フッタ。

動画専用テンプレートの中で唯一「事実を主張しない」ことが前提のカード。
本文は `decorative: true` を許すが、出典があればフッタに出す。
"""

from __future__ import annotations

from manim import DOWN, FadeIn, Line, Scene, VGroup

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    card_title,
    content_width,
    fit_to_frame,
    hold_to,
    resolve_font,
    scale_font,
    source_footer,
    wrapped_text,
)


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    subtitles = [b for b in spec["beats"] if b["type"] == "statement"]
    size = scale_font(28, len(subtitles), soft=2, hard=5)

    title = card_title(spec["title"], font, font_size=54)
    rule = Line(
        [-content_width() / 2, 0, 0], [content_width() / 2, 0, 0], color=COLOR_ACCENT, stroke_width=3
    )
    parts: list = [title, rule]
    if subtitles:
        parts.append(
            VGroup(
                *[wrapped_text(b["text"], font, size, COLOR_BODY, content_width()) for b in subtitles]
            ).arrange(DOWN, buff=0.28)
        )
    parts.append(source_footer(spec, font))
    return fit_to_frame(VGroup(*parts).arrange(DOWN, buff=0.55))


def make_scene_classes(spec: dict):
    class TitleCardAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            self.play(FadeIn(layout[0], shift=DOWN * 0.25), run_time=1.0)
            self.play(FadeIn(layout[1]), run_time=0.4)
            for part in layout[2:]:
                self.play(FadeIn(part), run_time=0.6)
                self.wait(0.4)
            self.wait(1.2)
            hold_to(self, spec)

    class TitleCardStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return TitleCardAnim, TitleCardStatic
