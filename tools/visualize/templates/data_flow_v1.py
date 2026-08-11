"""flow 用テンプレート: flow_step の箱と transition の矢印でデータフローを示す

build_final_layout(spec) は最終状態の Mobject ツリーを組むだけの純粋レイアウト関数。
"""
from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    TAU,
    UP,
    Arrow,
    Create,
    CurvedArrow,
    FadeIn,
    GrowArrow,
    Scene,
    BackgroundRectangle,
    Polygon,
    SurroundingRectangle,
    Text,
    VGroup,
    VMobject,
)

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_BOX,
    COLOR_DECISION,
    COLOR_KEY,
    COLOR_WARN,
    COLOR_METRIC,
    COLOR_TITLE,
    fit_to_frame,
    resolve_font,
    source_footer,
    wrapped_text,
)
from templates.layout import (
    EDGE_ADJACENT,
    EDGE_BACK,
    EDGE_CROSS_BAND_BACK,
    EDGE_SKIP_FORWARD,
    assign_ranks,
    classify_edge,
    layout_grid,
)

MAX_STEPS_PER_ROW = 4
#: 箱1つの最大幅(Manim の単位系)。4個 + buff 1.1x3 が frame_width 14.222 に収まる値。
#: 以前はテキストを組んでから囲っていたため、description が長いと箱が際限なく
#: 横に伸び、MAX_STEPS_PER_ROW の折り返しが破綻していた。
BOX_MAX_WIDTH = 2.6
#: 図の下に積む statement / metric の折り返し幅。
EXTRA_MAX_WIDTH = 12.6
#: タイトルの折り返し幅。
TITLE_MAX_WIDTH = 12.0


def _step_box(beat: dict, font: str) -> VGroup:
    inner_width = BOX_MAX_WIDTH - 0.5  # 枠の buff 0.25 を左右で引く
    parts = [wrapped_text(beat["label"], font, 26, COLOR_BODY, inner_width)]
    if beat.get("description"):
        parts.append(wrapped_text(beat["description"], font, 16, COLOR_BODY, inner_width))
    inner = VGroup(*parts).arrange(DOWN, buff=0.15)
    if inner.width > inner_width:
        # 折り返しても収まらない(禁則やごく長い英単語)場合の保険。
        inner.scale_to_fit_width(inner_width)
    color, width = _emphasis_style(beat)
    box = SurroundingRectangle(
        inner, corner_radius=0.12, buff=0.25, color=color, stroke_width=width
    )
    return VGroup(box, inner)


def _edge_label(edge: VMobject, label: str, font: str) -> VGroup:
    """矢印の中点にラベルを置く。

    `LabeledArrow` は使わない -- `Label` が str を `MathTex` へ変換するため、
    base.py の「LaTeX(Tex/MathTex)は使わない」方針に抵触する。
    線と重ならないよう背景を敷いてから中点へ寄せる。
    """
    text = Text(label, font=font, font_size=15, color=COLOR_ACCENT)
    try:
        anchor_point = edge.point_from_proportion(0.5)
    except Exception:  # noqa: BLE001 - 折れ線(VGroup)など弧でない場合
        anchor_point = edge.get_center()
    text.move_to(anchor_point)
    backdrop = BackgroundRectangle(text, fill_opacity=0.85, buff=0.05)
    return VGroup(backdrop, text)


def _emphasis_style(beat: dict) -> tuple[str, float]:
    """emphasis から枠色と線幅を決める(既定は従来どおり)。"""
    emphasis = beat.get("emphasis")
    if emphasis == "key":
        return COLOR_KEY, 6.0
    if emphasis == "warn":
        return COLOR_WARN, 6.0
    return COLOR_BOX, 2.0


def _decision_node(beat: dict, font: str) -> VGroup:
    """条件分岐のひし形。分岐先はラベル付き transition で表す。"""
    inner = wrapped_text(beat["label"], font, 24, COLOR_BODY, BOX_MAX_WIDTH - 0.5)
    half_w = inner.width * 0.85 + 0.35
    half_h = inner.height * 1.15 + 0.30
    center = inner.get_center()
    color, width = _emphasis_style(beat)
    diamond = Polygon(
        center + UP * half_h,
        center + RIGHT * half_w,
        center + DOWN * half_h,
        center + LEFT * half_w,
        color=COLOR_DECISION if color == COLOR_BOX else color,
        stroke_width=width,
    )
    return VGroup(diamond, inner)


def _edge_mobject(
    kind: str,
    src: VGroup,
    dst: VGroup,
    src_pos: tuple[int, int],
    dst_pos: tuple[int, int],
) -> VMobject:
    """位置関係の分類に応じた矢印を返す。

    - adjacent: 隣接。従来どおりの水平直線(既存 spec の見た目を保つ)
    - skip_forward: 前方へ飛ばす。箱の上を弧で越える
    - back: 同じ行の後戻り。箱の下を弧で戻る
    - wrap_down: 次の行へ折り返す。右へ出て下がり左へ入る折れ線
    - cross_band_back: 行をまたぐ後戻り。左マージン側を大きな弧で回す
    """
    if kind == EDGE_ADJACENT:
        return Arrow(
            src.get_right(), dst.get_left(), buff=0.12, color=COLOR_ACCENT, stroke_width=4
        )

    span = abs(dst_pos[1] - src_pos[1])
    if kind == EDGE_SKIP_FORWARD:
        angle = -TAU / 6 if span <= 2 else -TAU / 8
        return CurvedArrow(
            src.get_top(), dst.get_top(), angle=angle, color=COLOR_ACCENT, stroke_width=3
        )
    if kind == EDGE_BACK:
        # 下側に膨らませる。主フローではないので細く・少し薄く描く。
        angle = -TAU / 5 if span <= 2 else -TAU / 7
        arrow = CurvedArrow(
            src.get_bottom(), dst.get_bottom(), angle=angle, color=COLOR_ACCENT, stroke_width=3
        )
        arrow.set_stroke(opacity=0.75)
        return arrow
    if kind == EDGE_CROSS_BAND_BACK:
        arrow = CurvedArrow(
            src.get_left(), dst.get_left(), angle=TAU / 4, color=COLOR_ACCENT, stroke_width=3
        )
        arrow.set_stroke(opacity=0.75)
        return arrow

    # wrap_down: 右へ出る -> 段間の中央まで下がる -> 次の行の左端の手前まで戻る -> 入る。
    # 折れ線本体は VMobject(矢尻を持てない)なので、最終セグメントだけ Arrow を重ねる。
    start, end = src.get_right(), dst.get_left()
    # 最終セグメントは矢尻が視認できる長さを確保する(短すぎると tip が潰れる)。
    out_x, in_x = start[0] + 0.4, end[0] - 0.9
    mid_y = (start[1] + end[1]) / 2
    path = VMobject(color=COLOR_ACCENT, stroke_width=3)
    path.set_points_as_corners(
        [
            start,
            [out_x, start[1], 0],
            [out_x, mid_y, 0],
            [in_x, mid_y, 0],
            [in_x, end[1], 0],
        ]
    )
    tip = Arrow(
        [in_x, end[1], 0],
        end,
        buff=0.12,
        color=COLOR_ACCENT,
        stroke_width=3,
        tip_length=0.22,
    )
    return VGroup(path, tip)


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
    steps = [b for b in spec["beats"] if b["type"] in ("flow_step", "decision")]
    transitions = [b for b in spec["beats"] if b["type"] == "transition"]
    extras = [b for b in spec["beats"] if b["type"] in ("statement", "metric")]

    boxes: dict[str, VGroup] = {}
    labels: list[str] = []
    for beat in steps:
        label = beat["label"]
        if label in boxes:
            # 検証層(scene_spec の duplicate_label)で弾かれているはずだが、
            # scene.py 経由の再描画は validate_scene_spec を通らないため、
            # ここでも黙って上書きさせない。
            raise ValueError(
                f'flow_step の label "{label}" が重複しています。'
                "SceneSpec 側で一意にしてください"
                "(scene-spec.json を手編集した場合はここで検出されます)"
            )
        boxes[label] = (
            _decision_node(beat, font) if beat["type"] == "decision" else _step_box(beat, font)
        )
        labels.append(label)

    # transition のグラフ構造から列(rank)を決める。beat 順に4個ずつ折り返す従来方式は
    # グラフを一切見ないため、decision の分岐先が横一列に並んで分岐に見えなかった。
    #
    # **直線フロー A->B->C->D->E では rank が 0,1,2,3,4 と1個ずつ増え、layout_grid が
    # (0,0),(0,1),(0,2),(0,3),(1,0) を返す = 従来の MAX_STEPS_PER_ROW=4 と同一の配置**
    # になる(配置互換はこの性質に依る)。分岐・合流・循環・孤立ノードを含む spec の
    # 配置は変わる。
    edges = [(t["from"], t["to"]) for t in transitions]
    ranks = assign_ranks(labels, edges)
    placement = layout_grid(labels, ranks, max_ranks_per_band=MAX_STEPS_PER_ROW)

    # label -> (band, col)。矢印の描き分け(classify_edge)に使う。
    positions: dict[str, tuple[int, int]] = {
        label: (band, col) for label, (band, col, _slot) in placement.items()
    }

    # band -> col -> [(slot, label)] に畳んでから Mobject を組む。
    bands: dict[int, dict[int, list[tuple[int, str]]]] = {}
    for label in labels:
        band, col, slot = placement[label]
        bands.setdefault(band, {}).setdefault(col, []).append((slot, label))

    rows = VGroup()
    for band in sorted(bands):
        row = VGroup()
        for col in sorted(bands[band]):
            column = VGroup(*[boxes[label] for _slot, label in sorted(bands[band][col])])
            # 同じ rank の複数ノード(=分岐先)は縦に積む。
            column.arrange(DOWN, buff=0.5)
            row.add(column)
        row.arrange(RIGHT, buff=1.1, aligned_edge=UP)
        rows.add(row)
    rows.arrange(DOWN, buff=0.9, aligned_edge=LEFT)

    # 矢印は箱の配置が確定してから引く。位置関係で描き方を変えないと、同じ行の
    # 後戻り(4番目 -> 2番目)が間の箱を貫通し、行をまたぐ矢印は右端から左端へ
    # 斜めに全幅を横切ってしまう。
    arrows = VGroup()
    for t in transitions:
        src = boxes.get(t["from"])
        dst = boxes.get(t["to"])
        if src is None or dst is None:
            continue
        src_pos, dst_pos = positions[t["from"]], positions[t["to"]]
        edge = _edge_mobject(classify_edge(src_pos, dst_pos), src, dst, src_pos, dst_pos)
        label = t.get("label")
        if label:
            arrows.add(VGroup(edge, _edge_label(edge, label, font)))
        else:
            arrows.add(edge)

    diagram = VGroup(rows, arrows)
    title = wrapped_text(spec["title"], font, 40, COLOR_TITLE, TITLE_MAX_WIDTH)
    parts = [title, diagram]
    if extras:
        parts.append(VGroup(*[_extra_mobject(b, font) for b in extras]).arrange(DOWN, aligned_edge=LEFT, buff=0.3))
    parts.append(source_footer(spec, font))
    layout = VGroup(*parts).arrange(DOWN, buff=0.6)
    return fit_to_frame(layout)


def _is_edge_group(group: VGroup) -> bool:
    """`VGroup(edge, label)` かどうか(折れ線の `VGroup(path, tip)` と区別する)。"""
    return isinstance(group[1], VGroup) and len(group[1]) == 2 and isinstance(group[1][1], Text)


def _edge_animation(edge: VMobject):
    return GrowArrow(edge) if isinstance(edge, Arrow) else Create(edge)


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
                # GrowArrow は Arrow 専用。曲線・折れ線は Create で描き起こす。
                # ラベル付きの矢印は VGroup(edge, label) なので、矢印を出してから
                # ラベルをフェードインさせる。
                if isinstance(arrow, VGroup) and len(arrow) == 2 and _is_edge_group(arrow):
                    edge, label = arrow
                    self.play(_edge_animation(edge), run_time=0.4)
                    self.play(FadeIn(label), run_time=0.25)
                else:
                    self.play(_edge_animation(arrow), run_time=0.4)
            for part in rest:
                self.play(FadeIn(part), run_time=0.5)
            self.wait(1.5)

    class DataFlowStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return DataFlowAnim, DataFlowStatic
