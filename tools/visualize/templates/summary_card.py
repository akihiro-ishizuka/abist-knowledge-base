"""summary_card: まとめ。チェックマーク付きで結論を並べる。

`key_points` と描画は近いが、**動画の締め**という役割を明示するために分ける
（章立て・チャプター生成が role で分岐するため、テンプレートも別にしておく）。
"""

from __future__ import annotations

from manim import DOWN, LEFT, RIGHT, FadeIn, Scene, Text, VGroup

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_METRIC,
    card_title,
    content_width,
    fit_to_frame,
    resolve_font,
    scale_font,
    source_footer,
    wrapped_text,
)

#: チェックマーク（LaTeX を使わないので文字で描く）。
CHECK_MARK = "✓"


def _row(beat: dict, font: str, size: float, body_width: float) -> VGroup:
    mark = Text(CHECK_MARK, font=font, font_size=size, color=COLOR_ACCENT, weight="BOLD")
    if beat["type"] == "metric":
        unit = beat.get("unit") or ""
        body = VGroup(
            Text(beat["label"], font=font, font_size=size, color=COLOR_BODY),
            Text(
                f'{beat["value"]}{unit}',
                font=font,
                font_size=size * 1.15,
                color=COLOR_METRIC,
                weight="BOLD",
            ),
        ).arrange(RIGHT, buff=0.3)
    else:
        body = wrapped_text(beat["text"], font, size, COLOR_BODY, body_width)
    return VGroup(mark, body).arrange(RIGHT, buff=0.3)


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    items = [b for b in spec["beats"] if b["type"] in ("statement", "metric")]
    size = scale_font(30, len(items), soft=3, hard=7)
    body_width = content_width() - 0.8

    rows = VGroup(*[_row(b, font, size, body_width) for b in items])
    if len(rows):
        rows.arrange(DOWN, aligned_edge=LEFT, buff=0.32)
    parts = [card_title(spec["title"], font, font_size=44), rows, source_footer(spec, font)]
    return fit_to_frame(VGroup(*parts).arrange(DOWN, buff=0.5))


def make_scene_classes(spec: dict):
    class SummaryCardAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            self.play(FadeIn(layout[0], shift=DOWN * 0.2), run_time=0.8)
            for row in layout[1]:
                self.play(FadeIn(row, shift=RIGHT * 0.2), run_time=0.45)
                self.wait(0.5)
            self.play(FadeIn(layout[2]), run_time=0.4)
            self.wait(1.5)

    class SummaryCardStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return SummaryCardAnim, SummaryCardStatic
