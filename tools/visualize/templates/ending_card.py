"""ending_card: エンドカード。出典一覧と社内限定の注意書きを最後に出す。

**出典を最後にまとめて見せる面**であり、`public_candidate=false` のときは
「社外公開しないこと」を書ける（文言は spec 側が statement として渡す）。
"""

from __future__ import annotations

from manim import DOWN, LEFT, FadeIn, Line, Scene, Text, VGroup

from templates.layout import shows_source_heading
from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_FOOTER,
    COLOR_WARN,
    card_title,
    content_width,
    fit_to_frame,
    hold_to,
    resolve_font,
    scale_font,
    wrapped_text,
)

#: エンドカードでは出典を本文サイズで読ませる（フッタの 16pt では小さすぎる）。
SOURCE_FONT_SIZE = 20


def _source_lines(spec: dict, font: str) -> VGroup:
    refs = sorted(
        {
            f"{s['path'].replace(chr(92), '/').rsplit('/', 1)[-1]}:"
            f"{s['start_line']}-{s['end_line']}"
            for s in spec.get("sources", [])
        }
    )
    if not refs:
        return VGroup()
    return VGroup(
        *[wrapped_text(ref, font, SOURCE_FONT_SIZE, COLOR_FOOTER, content_width()) for ref in refs]
    ).arrange(DOWN, aligned_edge=LEFT, buff=0.12)


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    notes = [b for b in spec["beats"] if b["type"] == "statement"]
    size = scale_font(26, len(notes), soft=2, hard=5)

    parts: list = [card_title(spec["title"], font, font_size=40)]
    if notes:
        parts.append(
            VGroup(
                *[
                    wrapped_text(
                        b["text"],
                        font,
                        size,
                        COLOR_WARN if b.get("emphasis") == "warn" else COLOR_BODY,
                        content_width(),
                    )
                    for b in notes
                ]
            ).arrange(DOWN, buff=0.24)
        )
    if shows_source_heading(spec.get("sources")):
        parts.append(
            Line(
                [-content_width() / 2, 0, 0],
                [content_width() / 2, 0, 0],
                color=COLOR_ACCENT,
                stroke_width=2,
            )
        )
        parts.append(Text("出典", font=font, font_size=22, color=COLOR_ACCENT))
        parts.append(_source_lines(spec, font))
    return fit_to_frame(VGroup(*parts).arrange(DOWN, buff=0.35))


def make_scene_classes(spec: dict):
    class EndingCardAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            self.play(FadeIn(layout[0], shift=DOWN * 0.2), run_time=0.8)
            for part in layout[1:]:
                self.play(FadeIn(part), run_time=0.45)
                self.wait(0.3)
            self.wait(2.0)
            hold_to(self, spec)

    class EndingCardStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return EndingCardAnim, EndingCardStatic
