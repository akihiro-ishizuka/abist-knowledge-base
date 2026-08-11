"""quote_card: 原文の引用。**必ず出典が要る**カード。

`quote` beat は「KB の原文をそのまま見せる」ためのもので、要約でも言い換えでも
ない。したがって `decorative` は認めず、`source_refs` を必須にする。
"""

from __future__ import annotations

from manim import DOWN, LEFT, RIGHT, FadeIn, Line, Scene, Text, VGroup, Write

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_FOOTER,
    card_title,
    content_width,
    fit_to_frame,
    resolve_font,
    scale_font,
    source_footer,
    wrapped_text,
)

#: 引用符（LaTeX を使わないので全角の鉤括弧を使う）。
QUOTE_OPEN = "「"
QUOTE_CLOSE = "」"


def _quote_block(beat: dict, font: str, size: float, width: float) -> VGroup:
    body = wrapped_text(
        f'{QUOTE_OPEN}{beat["text"]}{QUOTE_CLOSE}', font, size, COLOR_BODY, width - 0.5
    )
    bar = Line(
        body.get_top() + LEFT * 0.3, body.get_bottom() + LEFT * 0.3, color=COLOR_ACCENT, stroke_width=5
    )
    parts: list = [VGroup(bar, body)]
    if beat.get("attribution"):
        parts.append(
            Text(f'— {beat["attribution"]}', font=font, font_size=size * 0.62, color=COLOR_FOOTER)
        )
    return VGroup(*parts).arrange(DOWN, aligned_edge=RIGHT, buff=0.24)


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    quotes = [b for b in spec["beats"] if b["type"] == "quote"]
    size = scale_font(32, len(quotes), soft=1, hard=3)
    width = content_width()

    blocks = VGroup(*[_quote_block(b, font, size, width) for b in quotes])
    if len(blocks):
        blocks.arrange(DOWN, aligned_edge=LEFT, buff=0.5)
    parts = [card_title(spec["title"], font, font_size=38), blocks, source_footer(spec, font)]
    return fit_to_frame(VGroup(*parts).arrange(DOWN, buff=0.55))


def make_scene_classes(spec: dict):
    class QuoteCardAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            self.play(FadeIn(layout[0], shift=DOWN * 0.2), run_time=0.7)
            for block in layout[1]:
                self.play(Write(block), run_time=1.1)
                self.wait(1.0)
            self.play(FadeIn(layout[2]), run_time=0.4)
            self.wait(1.3)

    class QuoteCardStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return QuoteCardAnim, QuoteCardStatic
