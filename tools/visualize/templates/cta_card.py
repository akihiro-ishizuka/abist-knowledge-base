"""cta_card: 次にやってほしいことを1画面で示す（社内向けの行動喚起）。

外部公開を前提にしないので「チャンネル登録」の類は扱わない。想定は
「詳しくは docs/ を参照」「不明点は #channel へ」といった社内導線。
"""

from __future__ import annotations

from manim import DOWN, FadeIn, RoundedRectangle, Scene, VGroup

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

#: 枠の内側余白。
BOX_PADDING = 0.4


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    actions = [b for b in spec["beats"] if b["type"] == "statement"]
    size = scale_font(30, len(actions), soft=2, hard=5)
    width = content_width() - BOX_PADDING * 2

    lines = VGroup(*[wrapped_text(b["text"], font, size, COLOR_BODY, width) for b in actions])
    if len(lines):
        lines.arrange(DOWN, buff=0.3)
    box = RoundedRectangle(
        corner_radius=0.15,
        width=max(lines.width + BOX_PADDING * 2, 3.0),
        height=max(lines.height + BOX_PADDING * 2, 1.2),
        color=COLOR_ACCENT,
        stroke_width=3,
    )
    boxed = VGroup(box, lines)
    lines.move_to(box.get_center())
    parts = [card_title(spec["title"], font, font_size=42), boxed, source_footer(spec, font)]
    return fit_to_frame(VGroup(*parts).arrange(DOWN, buff=0.6))


def make_scene_classes(spec: dict):
    class CtaCardAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            for part in layout:
                self.play(FadeIn(part, shift=DOWN * 0.2), run_time=0.6)
                self.wait(0.5)
            self.wait(1.4)
            hold_to(self, spec)

    class CtaCardStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return CtaCardAnim, CtaCardStatic
