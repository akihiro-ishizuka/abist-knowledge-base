"""key_points: 要点の箇条書き。statement / metric を行頭マーカー付きで積む。

`step_explanation` との違いは「順を追って説明する」のではなく
**同格の要点を並べて見せる**こと。動画では章の締めに置く。
"""

from __future__ import annotations

from manim import DOWN, LEFT, RIGHT, Circle, FadeIn, Indicate, Scene, Text, VGroup

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_KEY,
    COLOR_METRIC,
    COLOR_WARN,
    card_title,
    content_width,
    fit_to_frame,
    resolve_font,
    scale_font,
    source_footer,
    wrapped_text,
)

#: 行頭マーカーの半径。
MARKER_RADIUS = 0.09


def _color_for(beat: dict, default: str) -> str:
    emphasis = beat.get("emphasis")
    if emphasis == "key":
        return COLOR_KEY
    if emphasis == "warn":
        return COLOR_WARN
    return default


def _point_row(beat: dict, font: str, size: float, body_width: float) -> VGroup:
    marker = Circle(radius=MARKER_RADIUS, color=_color_for(beat, COLOR_ACCENT), fill_opacity=1.0)
    if beat["type"] == "metric":
        unit = beat.get("unit") or ""
        body = VGroup(
            Text(f'{beat["label"]}', font=font, font_size=size, color=COLOR_BODY),
            Text(
                f'{beat["value"]}{unit}',
                font=font,
                font_size=size * 1.15,
                color=COLOR_METRIC,
                weight="BOLD",
            ),
        ).arrange(RIGHT, buff=0.3)
    else:
        body = wrapped_text(beat["text"], font, size, _color_for(beat, COLOR_BODY), body_width)
    return VGroup(marker, body).arrange(RIGHT, buff=0.3)


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    points = [b for b in spec["beats"] if b["type"] in ("statement", "metric")]
    size = scale_font(30, len(points), soft=3, hard=7)
    body_width = content_width() - 0.6

    rows = VGroup(*[_point_row(b, font, size, body_width) for b in points])
    if len(rows):
        rows.arrange(DOWN, aligned_edge=LEFT, buff=0.34)
    parts = [card_title(spec["title"], font, font_size=42), rows, source_footer(spec, font)]
    return fit_to_frame(VGroup(*parts).arrange(DOWN, buff=0.5))


def make_scene_classes(spec: dict):
    class KeyPointsAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            title, rows, footer = layout[0], layout[1], layout[2]
            self.play(FadeIn(title, shift=DOWN * 0.2), run_time=0.8)
            self.play(FadeIn(footer), run_time=0.4)
            beats = [b for b in spec["beats"] if b["type"] in ("statement", "metric")]
            for row, beat in zip(rows, beats):
                self.play(FadeIn(row, shift=RIGHT * 0.25), run_time=0.5)
                if beat.get("emphasis") in ("key", "warn"):
                    self.play(Indicate(row, scale_factor=1.08), run_time=0.4)
                self.wait(0.55)
            self.wait(1.2)

    class KeyPointsStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return KeyPointsAnim, KeyPointsStatic
