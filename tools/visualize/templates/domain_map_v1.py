"""domain 用テンプレート: グループ（クラスタ）付きの静的な関係図。

`manim.Graph` + networkx は採らない:
  (a) `Graph(labels=True)` の既定が `MathTex` で base.py の LaTeX 不使用方針に反する
  (b) spring レイアウトは乱数依存で再現性が壊れる。manifest に成果物の sha256 を
      記録している以上、同じ spec が同じ絵にならないのは受け入れがたい
  (c) 日本語ラベルの重なりを制御できない

代わりに data_flow_v1 の箱＋矢印を「グループ列」へ決定的に拡張する。

build_final_layout(spec) は最終状態の Mobject ツリーを組むだけの純粋レイアウト関数。
"""

from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Arrow,
    BackgroundRectangle,
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
    COLOR_FOOTER,
    COLOR_KEY,
    COLOR_METRIC,
    COLOR_RULE,
    COLOR_TITLE,
    COLOR_WARN,
    body_width,
    fit_to_frame,
    node_width,
    resolve_font,
    scale_font,
    source_footer,
    title_width,
    wrapped_text,
)
from templates import choreography  # noqa: E402  (base の後に読む)

#: クラスタを横に並べるときの間隔。
CLUSTER_BUFF = 1.3
#: 1行に並べるクラスタ数の上限（超えたら折り返す）。
MAX_CLUSTERS_PER_ROW = 4
#: group を持たない要素をまとめる内部キー（枠を描かない）。
_UNGROUPED = "\x00ungrouped"


def _emphasis_style(beat: dict) -> tuple[str, float]:
    emphasis = beat.get("emphasis")
    if emphasis == "key":
        return COLOR_KEY, 6.0
    if emphasis == "warn":
        return COLOR_WARN, 6.0
    return COLOR_BOX, 2.0


def _entity_box(beat: dict, font: str, label_size: float) -> VGroup:
    inner_width = node_width() - 0.5
    parts = [wrapped_text(beat["name"], font, label_size, COLOR_BODY, inner_width)]
    if beat.get("description"):
        parts.append(
            wrapped_text(beat["description"], font, label_size * 0.62, COLOR_BODY, inner_width)
        )
    inner = VGroup(*parts).arrange(DOWN, buff=0.12)
    if inner.width > inner_width:
        inner.scale_to_fit_width(inner_width)
    color, width = _emphasis_style(beat)
    box = SurroundingRectangle(
        inner, corner_radius=0.12, buff=0.22, color=color, stroke_width=width
    )
    return VGroup(box, inner)


def _extra_mobject(beat: dict, font: str):
    if beat["type"] == "statement":
        return wrapped_text(beat["text"], font, 24, COLOR_BODY, body_width())
    unit = beat.get("unit") or ""
    label = Text(f"{beat['label']}:", font=font, font_size=24, color=COLOR_BODY)
    value = Text(
        f"{beat['value']}{unit}", font=font, font_size=28, color=COLOR_METRIC, weight="BOLD"
    )
    return VGroup(label, value).arrange(RIGHT, buff=0.3)


def _relation_mobject(src: VGroup, dst: VGroup, label: str | None, font: str) -> VGroup:
    """要素間の矢印。アンカーは位置関係で選ぶ（data_flow_v1 と同じ流儀）。"""
    # アンカーは「どちらの方向により離れているか」で選ぶ。y 差だけで判定すると、
    # 横に大きく離れた要素同士で上下アンカーが選ばれ、矢印が他の箱を横切る。
    dx = dst.get_center()[0] - src.get_center()[0]
    dy = dst.get_center()[1] - src.get_center()[1]
    if abs(dx) >= abs(dy):
        horizontal = True
        start, end = (
            (src.get_right(), dst.get_left()) if dx >= 0 else (src.get_left(), dst.get_right())
        )
    else:
        horizontal = False
        start, end = (
            (src.get_top(), dst.get_bottom()) if dy >= 0 else (src.get_bottom(), dst.get_top())
        )
    arrow = Arrow(start, end, buff=0.1, color=COLOR_ACCENT, stroke_width=3)
    if not label:
        return VGroup(arrow)
    text = Text(label, font=font, font_size=14, color=COLOR_ACCENT)
    # ラベルは矢印の中点から法線方向へ逃がす。中点に重ねると、短い矢印
    # (同じクラスタ内の上下関係など)が背景板の下に完全に隠れてしまう。
    offset = UP * 0.38 if horizontal else RIGHT * 0.55
    text.move_to(arrow.get_center() + offset)
    backdrop = BackgroundRectangle(text, fill_opacity=0.85, buff=0.04)
    return VGroup(arrow, VGroup(backdrop, text))


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    entities = [b for b in spec["beats"] if b["type"] == "domain_entity"]
    relations = [b for b in spec["beats"] if b["type"] == "domain_relation"]
    extras = [b for b in spec["beats"] if b["type"] in ("statement", "metric")]

    label_size = scale_font(24, len(entities), soft=5, hard=10)

    # グループは初出順。group を持たない要素は末尾の無名クラスタへ（枠を描かない）。
    group_order: list[str] = []
    members: dict[str, list[dict]] = {}
    for beat in entities:
        key = beat["group"] if isinstance(beat.get("group"), str) else _UNGROUPED
        if key not in members:
            members[key] = []
            group_order.append(key)
        members[key].append(beat)

    boxes: dict[str, VGroup] = {}
    clusters = VGroup()
    for key in group_order:
        stack = VGroup()
        for beat in members[key]:
            box = _entity_box(beat, font, label_size)
            boxes[beat["name"]] = box
            stack.add(box)
        # 同じクラスタ内の要素同士にも関係を引けるよう、矢印が見える間隔を確保する
        stack.arrange(DOWN, buff=0.5 if len(stack) == 4 else 0.9)
        if key == _UNGROUPED:
            clusters.add(VGroup(stack))
            continue
        frame = SurroundingRectangle(
            stack, corner_radius=0.15, buff=0.3, color=COLOR_RULE, stroke_width=2
        )
        name = Text(key, font=font, font_size=18, color=COLOR_FOOTER)
        name.next_to(frame, UP, buff=0.08).align_to(frame, LEFT)
        clusters.add(VGroup(frame, name, stack))

    # クラスタは横並び、MAX_CLUSTERS_PER_ROW ごとに折り返す。
    rows = VGroup()
    for start in range(0, len(clusters), MAX_CLUSTERS_PER_ROW):
        row = VGroup(*clusters[start : start + MAX_CLUSTERS_PER_ROW])
        # 高さの違うクラスタは中央を揃える。起点を上端へ寄せると、下段ノードへ
        # 向かう扇状の矢印が途中の箱を横切るため。
        row.arrange(RIGHT, buff=CLUSTER_BUFF)
        rows.add(row)
    rows.arrange(DOWN, buff=0.9, aligned_edge=LEFT)

    # 矢印は箱の配置が確定してから引く。
    edges = VGroup()
    for beat in relations:
        src = boxes.get(beat["from"])
        dst = boxes.get(beat["to"])
        if src is None or dst is None:
            continue
        edges.add(_relation_mobject(src, dst, beat.get("label"), font))

    diagram = VGroup(rows, edges)
    title = wrapped_text(spec["title"], font, 40, COLOR_TITLE, title_width())
    parts: list = [title, diagram]
    if extras:
        extra_group = VGroup(*[_extra_mobject(b, font) for b in extras])
        parts.append(extra_group.arrange(DOWN, aligned_edge=LEFT, buff=0.3))
    parts.append(source_footer(spec, font))
    layout = VGroup(*parts).arrange(DOWN, buff=0.6)
    return fit_to_frame(layout)


def make_scene_classes(spec: dict):
    class DomainMapAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            title = layout[0]
            diagram = layout[1]
            rest = layout[2:]
            rows, edges = diagram
            all_boxes = [
                box
                for row in rows
                for cluster in row
                for box in (cluster[2] if len(cluster) == 3 else cluster[0])
            ]
            budget = choreography.budget_for(
                spec, beat_count=len(all_boxes), edge_count=len(edges)
            )
            clock = choreography.BeatClock(self)

            choreography.enter_scene(self, spec)
            self.play(FadeIn(title, shift=DOWN * 0.2), run_time=0.9)
            # 「場(グループ)を作る -> 住人(要素)を置く -> 関係を引く」の順。
            # 構造図の理解順序と一致する。
            for row in rows:
                for cluster in row:
                    if len(cluster) == 3:
                        frame, name, stack = cluster
                        self.play(Create(frame), FadeIn(name), run_time=0.4)
                    else:
                        stack = cluster[0]
                    # 同じクラスタの住人はまとめて出す（塊として見せる）。
                    choreography.stagger_in(self, list(stack), run_time=0.7)
            for edge in edges:
                clock.mark()
                animations = [GrowArrow(edge[0])]
                animations += [FadeIn(part) for part in edge[1:]]
                self.play(*animations, run_time=0.4)
                # 関係線に光を通して、どこと繋がったかを見せる。
                choreography.flow_pulse(self, edge, color=COLOR_ACCENT, budget=budget)
            for part in rest:
                self.play(FadeIn(part), run_time=0.5)
            self.wait(1.5)
            choreography.finish(self, spec, items=all_boxes, clock=clock)

    class DomainMapStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return DomainMapAnim, DomainMapStatic
