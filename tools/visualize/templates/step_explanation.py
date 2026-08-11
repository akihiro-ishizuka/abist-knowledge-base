"""explain 用テンプレート: statement / metric / transition を順に提示する

build_final_layout(spec) は最終状態の Mobject ツリーを組むだけの純粋レイアウト関数。
Anim シーンはそれを beat 順に登場させ、Static シーン（png 用）はそのまま add する。
"""
from __future__ import annotations

from manim import DOWN, LEFT, RIGHT, UP, Arrow, FadeIn, GrowArrow, Indicate, Scene, Text, VGroup

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_METRIC,
    COLOR_TITLE,
    fit_to_frame,
    hold_to,
    resolve_font,
    source_footer,
    wrapped_text,
)
from templates.layout import display_width

#: 本文の折り返し幅(Manim の単位系)。frame_width 14.222 から左右マージンを引いた値。
BODY_MAX_WIDTH = 12.6
#: タイトルの折り返し幅。
TITLE_MAX_WIDTH = 12.0


def _beat_mobject(beat: dict, font: str):
    if beat["type"] == "statement":
        return wrapped_text(beat["text"], font, 28, COLOR_BODY, BODY_MAX_WIDTH)
    if beat["type"] == "metric":
        unit = beat.get("unit") or ""
        label = Text(f'{beat["label"]}:', font=font, font_size=28, color=COLOR_BODY)
        value = Text(
            f'{beat["value"]}{unit}', font=font, font_size=32, color=COLOR_METRIC, weight="BOLD"
        )
        return VGroup(label, value).arrange(RIGHT, buff=0.3)
    if beat["type"] == "transition":
        # 文字列の "→" ではなく実際の矢印を組む(GrowArrow で演出できる)。
        left = Text(beat["from"], font=font, font_size=26, color=COLOR_ACCENT)
        right = Text(beat["to"], font=font, font_size=26, color=COLOR_ACCENT)
        arrow = Arrow(LEFT * 0.35, RIGHT * 0.35, buff=0.0, color=COLOR_ACCENT, stroke_width=3)
        return VGroup(left, arrow, right).arrange(RIGHT, buff=0.22)
    raise ValueError(f'未知の beat type: {beat["type"]}')


def _dwell_seconds(beat: dict) -> float:
    """beat の文字量から「読む時間」を見積もる。

    従来は 0.6 秒ずつ現れて終わりで、読む時間がゼロだった。SceneSpec に何も
    足さずに尺が内容へ追従するよう、表示幅から算出する。
    """
    if beat["type"] == "statement":
        body = beat.get("text", "")
    elif beat["type"] == "metric":
        body = f'{beat.get("label", "")}{beat.get("value", "")}{beat.get("unit") or ""}'
    else:
        body = f'{beat.get("from", "")}{beat.get("to", "")}'
    return min(3.0, 0.4 + display_width(body) * 0.035)


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    title = wrapped_text(spec["title"], font, 40, COLOR_TITLE, TITLE_MAX_WIDTH)
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
            self.play(FadeIn(title, shift=DOWN * 0.2), run_time=0.9)
            # 出典は最初から見えているほうが出典必須ポリシーと整合する。
            self.play(FadeIn(footer), run_time=0.5)
            for beat, item in zip(spec["beats"], items, strict=True):
                if beat["type"] == "transition":
                    # 実矢印なので伸ばす演出ができる(左右のラベルは同時に出す)。
                    self.play(
                        FadeIn(item[0]), GrowArrow(item[1]), FadeIn(item[2]), run_time=0.5
                    )
                elif beat["type"] == "metric":
                    self.play(FadeIn(item, shift=UP * 0.2), run_time=0.5)
                    self.play(Indicate(item[1], scale_factor=1.1), run_time=0.4)
                else:
                    self.play(FadeIn(item, shift=UP * 0.25), run_time=0.5)
                self.wait(_dwell_seconds(beat))
            self.wait(1.5)
            hold_to(self, spec)

    class StepExplanationStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return StepExplanationAnim, StepExplanationStatic
