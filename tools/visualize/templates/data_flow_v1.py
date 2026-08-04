"""flow 用テンプレート: flow_step の箱と transition の矢印でデータフローを示す

build_final_layout(spec) は最終状態の Mobject ツリーを組むだけの純粋レイアウト関数。
"""
from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Arrow,
    Create,
    FadeIn,
    GrowArrow,
    Scene,
    SurroundingRectangle,
    Text,
    VGroup,
)

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_BOX,
    COLOR_METRIC,
    COLOR_TITLE,
    fit_to_frame,
    resolve_font,
    source_footer,
)

MAX_STEPS_PER_ROW = 4


def _step_box(beat: dict, font: str) -> VGroup:
    label = Text(beat["label"], font=font, font_size=26, color=COLOR_BODY)
    parts = [label]
    if beat.get("description"):
        parts.append(Text(beat["description"], font=font, font_size=16, color=COLOR_BODY))
    inner = VGroup(*parts).arrange(DOWN, buff=0.15)
    box = SurroundingRectangle(inner, corner_radius=0.12, buff=0.25, color=COLOR_BOX)
    return VGroup(box, inner)


def _extra_mobject(beat: dict, font: str):
    if beat["type"] == "statement":
        return Text(beat["text"], font=font, font_size=24, color=COLOR_BODY)
    unit = beat.get("unit") or ""
    return VGroup(
        Text(f'{beat["label"]}:', font=font, font_size=24, color=COLOR_BODY),
        Text(f'{beat["value"]}{unit}', font=font, font_size=28, color=COLOR_METRIC, weight="BOLD"),
    ).arrange(RIGHT, buff=0.3)


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    steps = [b for b in spec["beats"] if b["type"] == "flow_step"]
    transitions = [b for b in spec["beats"] if b["type"] == "transition"]
    extras = [b for b in spec["beats"] if b["type"] in ("statement", "metric")]

    boxes: dict[str, VGroup] = {}
    rows = VGroup()
    for i in range(0, len(steps), MAX_STEPS_PER_ROW):
        row = VGroup()
        for beat in steps[i : i + MAX_STEPS_PER_ROW]:
            box = _step_box(beat, font)
            boxes[beat["label"]] = box
            row.add(box)
        row.arrange(RIGHT, buff=1.1)
        rows.add(row)
    rows.arrange(DOWN, buff=0.9, aligned_edge=LEFT)

    # 矢印は箱の配置が確定してから引く（後方向きの transition もそのまま描ける）
    arrows = VGroup()
    for t in transitions:
        src = boxes.get(t["from"])
        dst = boxes.get(t["to"])
        if src is None or dst is None:
            continue
        if abs(src.get_center()[1] - dst.get_center()[1]) < 1e-6:
            start, end = src.get_right(), dst.get_left()
        else:
            start, end = src.get_bottom(), dst.get_top()
        arrows.add(Arrow(start, end, buff=0.12, color=COLOR_ACCENT, stroke_width=4))

    diagram = VGroup(rows, arrows)
    title = Text(spec["title"], font=font, font_size=40, color=COLOR_TITLE)
    parts = [title, diagram]
    if extras:
        parts.append(VGroup(*[_extra_mobject(b, font) for b in extras]).arrange(DOWN, aligned_edge=LEFT, buff=0.3))
    parts.append(source_footer(spec, font))
    layout = VGroup(*parts).arrange(DOWN, buff=0.6)
    return fit_to_frame(layout)


def make_scene_classes(spec: dict):
    class DataFlowAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            title = layout[0]
            diagram = layout[1]
            rest = layout[2:]
            rows, arrows = diagram
            self.play(FadeIn(title), run_time=0.8)
            for row in rows:
                for box in row:
                    self.play(FadeIn(box, shift=UP * 0.2), run_time=0.5)
            for arrow in arrows:
                self.play(GrowArrow(arrow), run_time=0.4)
            for part in rest:
                self.play(FadeIn(part), run_time=0.5)
            self.wait(1.5)

    class DataFlowStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return DataFlowAnim, DataFlowStatic
