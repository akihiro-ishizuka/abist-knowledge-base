"""explain 用テンプレート: statement / metric / transition を順に提示する

build_final_layout(spec) は最終状態の Mobject ツリーを組むだけの純粋レイアウト関数。
Anim シーンはそれを beat 順に登場させ、Static シーン（png 用）はそのまま add する。
"""
from __future__ import annotations

from manim import DOWN, LEFT, RIGHT, UP, FadeIn, Scene, Text, VGroup

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_METRIC,
    COLOR_TITLE,
    fit_to_frame,
    resolve_font,
    source_footer,
)


def _beat_mobject(beat: dict, font: str):
    if beat["type"] == "statement":
        return Text(beat["text"], font=font, font_size=28, color=COLOR_BODY)
    if beat["type"] == "metric":
        unit = beat.get("unit") or ""
        label = Text(f'{beat["label"]}:', font=font, font_size=28, color=COLOR_BODY)
        value = Text(f'{beat["value"]}{unit}', font=font, font_size=32, color=COLOR_METRIC, weight="BOLD")
        return VGroup(label, value).arrange(RIGHT, buff=0.3)
    if beat["type"] == "transition":
        return Text(f'{beat["from"]} → {beat["to"]}', font=font, font_size=26, color=COLOR_ACCENT)
    raise ValueError(f'未知の beat type: {beat["type"]}')


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    title = Text(spec["title"], font=font, font_size=40, color=COLOR_TITLE)
    items = VGroup(*[_beat_mobject(b, font) for b in spec["beats"]]).arrange(
        DOWN, aligned_edge=LEFT, buff=0.35
    )
    footer = source_footer(spec, font)
    layout = VGroup(title, items, footer).arrange(DOWN, buff=0.6)
    return fit_to_frame(layout)


def make_scene_classes(spec: dict):
    class StepExplanationAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            title, items, footer = layout
            self.play(FadeIn(title), run_time=0.8)
            for item in items:
                self.play(FadeIn(item, shift=UP * 0.2), run_time=0.6)
            self.play(FadeIn(footer), run_time=0.5)
            self.wait(1.5)

    class StepExplanationStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return StepExplanationAnim, StepExplanationStatic
