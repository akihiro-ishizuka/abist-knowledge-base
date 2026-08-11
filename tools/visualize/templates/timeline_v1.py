"""timeline 用テンプレート: 縦スパインに timeline_point を上から順に並べる。

横軸型を採らない理由: 16:9 のフレームは 14.222x8 単位しかなく、日本語の本文
（「抽出→提案→編集→出力を単一アプリで完結」級）を上下交互に振っても隣と衝突する。
KB の実材料は議事録の「決まったこと」で1行に収まらないため、幅を本文へ全振りできる
縦型が正しい。上から下へ進む向きは日本語の議事録の読み順とも一致する。

build_final_layout(spec) は最終状態の Mobject ツリーを組むだけの純粋レイアウト関数。
"""

from __future__ import annotations

from manim import DOWN, LEFT, RIGHT, UP, Create, Dot, FadeIn, Line, Scene, Text, VGroup

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_KEY,
    COLOR_METRIC,
    COLOR_TITLE,
    COLOR_WARN,
    fit_to_frame,
    hold_to,
    resolve_font,
    scale_font,
    source_footer,
    wrapped_text,
)

#: 本文列の折り返し幅（Manim の単位系）。日付列とスパインの分を差し引いた値。
BODY_MAX_WIDTH = 9.6
#: 図の下に積む statement / metric の折り返し幅。
EXTRA_MAX_WIDTH = 12.6
#: タイトルの折り返し幅。
TITLE_MAX_WIDTH = 12.0
#: 点の半径。
DOT_RADIUS = 0.07


def _emphasis_color(beat: dict, default: str) -> str:
    emphasis = beat.get("emphasis")
    if emphasis == "key":
        return COLOR_KEY
    if emphasis == "warn":
        return COLOR_WARN
    return default


def _extra_mobject(beat: dict, font: str):
    if beat["type"] == "statement":
        return wrapped_text(beat["text"], font, 24, COLOR_BODY, EXTRA_MAX_WIDTH)
    unit = beat.get("unit") or ""
    return VGroup(
        Text(f'{beat["label"]}:', font=font, font_size=24, color=COLOR_BODY),
        Text(f'{beat["value"]}{unit}', font=font, font_size=28, color=COLOR_METRIC, weight="BOLD"),
    ).arrange(RIGHT, buff=0.3)


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    points = [b for b in spec["beats"] if b["type"] == "timeline_point"]
    extras = [b for b in spec["beats"] if b["type"] in ("statement", "metric")]

    n = len(points)
    label_size = scale_font(26, n, soft=4, hard=10)
    desc_size = scale_font(18, n, soft=4, hard=10)
    row_buff = 0.38 if n <= 6 else 0.24

    # 日付列は右寄せ、本文列は左寄せの2列グリッドで組む。arrange では
    # 日付の桁数がばらつくと本文の左端が揃わないため arrange_in_grid を使う。
    cells: list[VGroup] = []
    bodies: list[VGroup] = []
    for beat in points:
        at_text = Text(
            beat["at"], font=font, font_size=label_size, color=_emphasis_color(beat, COLOR_ACCENT)
        )
        parts = [
            wrapped_text(
                beat["label"], font, label_size, _emphasis_color(beat, COLOR_BODY), BODY_MAX_WIDTH
            )
        ]
        if beat.get("description"):
            parts.append(
                wrapped_text(beat["description"], font, desc_size, COLOR_BODY, BODY_MAX_WIDTH)
            )
        body = VGroup(*parts).arrange(DOWN, aligned_edge=LEFT, buff=0.12)
        bodies.append(body)
        cells.extend([at_text, body])

    rows = VGroup(*cells)
    rows.arrange_in_grid(
        rows=n, cols=2, col_alignments="rl", buff=(0.55, row_buff), flow_order="rd"
    )

    # スパインは日付列の右端と本文列の左端の中間に立てる。
    spine_x = (
        max(cells[i].get_right()[0] for i in range(0, len(cells), 2))
        + min(cells[i].get_left()[0] for i in range(1, len(cells), 2))
    ) / 2

    dots = VGroup()
    for body in bodies:
        dot = Dot(radius=DOT_RADIUS, color=COLOR_ACCENT)
        dot.move_to([spine_x, body.get_center()[1], 0])
        dots.add(dot)

    segments = VGroup()
    for i in range(len(dots) - 1):
        segments.add(
            Line(
                dots[i].get_bottom(),
                dots[i + 1].get_top(),
                color=COLOR_ACCENT,
                stroke_width=2,
            )
        )

    timeline = VGroup(segments, dots, rows)
    title = wrapped_text(spec["title"], font, 40, COLOR_TITLE, TITLE_MAX_WIDTH)
    parts_top: list = [title, timeline]
    if extras:
        parts_top.append(
            VGroup(*[_extra_mobject(b, font) for b in extras]).arrange(
                DOWN, aligned_edge=LEFT, buff=0.3
            )
        )
    parts_top.append(source_footer(spec, font))
    layout = VGroup(*parts_top).arrange(DOWN, buff=0.6)
    return fit_to_frame(layout)


def make_scene_classes(spec: dict):
    class TimelineAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            title = layout[0]
            timeline = layout[1]
            rest = layout[2:]
            segments, dots, rows = timeline
            self.play(FadeIn(title, shift=DOWN * 0.2), run_time=0.9)
            # 区間線を1本ずつ伸ばしてから次の点を出すことで「時間が進む」感覚を作る。
            # timeline は3種の中で唯一アニメーションが意味を持つ kind。
            for i in range(len(dots)):
                if i:
                    self.play(Create(segments[i - 1]), run_time=0.3)
                at_text, body = rows[i * 2], rows[i * 2 + 1]
                self.play(
                    FadeIn(dots[i], scale=0.5),
                    FadeIn(at_text, shift=RIGHT * 0.2),
                    FadeIn(body, shift=RIGHT * 0.25),
                    run_time=0.45,
                )
                self.wait(0.35)
            for part in rest:
                self.play(FadeIn(part), run_time=0.5)
            self.wait(1.5)
            hold_to(self, spec)

    class TimelineStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return TimelineAnim, TimelineStatic
