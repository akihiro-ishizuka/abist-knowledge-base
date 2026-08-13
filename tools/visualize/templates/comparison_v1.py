"""comparison 用テンプレート: 観点 x 対象のマトリクス表。

全セル（ヘッダ行込み）を一度の `arrange_in_grid` で組むのが列揃えの担保。
セルごとに `arrange` すると列幅が揃わない。

該当する comparison_item が無いセルは空白ではなく「—」を描く。空白にすると
「その観点では両者が揃っている」と誤読されるため、情報が無いことを明示する。

build_final_layout(spec) は最終状態の Mobject ツリーを組むだけの純粋レイアウト関数。
"""

from __future__ import annotations

import re

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Create,
    FadeIn,
    GrowFromEdge,
    Line,
    Rectangle,
    Scene,
    Text,
    VGroup,
)

from templates.base import (
    COLOR_BODY,
    COLOR_FOOTER,
    COLOR_KEY,
    COLOR_METRIC,
    COLOR_RULE,
    COLOR_TITLE,
    COLOR_WARN,
    body_width,
    fit_to_frame,
    hold_to,
    resolve_font,
    scale_font,
    source_footer,
    table_width,
    title_width,
    wrapped_text,
)
from templates import choreography  # noqa: E402  (base の後に読む)

#: 列と列のあいだの隙間(Manim の単位系)。列境界の罫線はこの中央に引く。
COL_GAP = 0.5
#: 該当なしのセルに描く記号。
EMPTY_CELL = "—"


def _numeric_hours(items: list[dict]) -> dict[str, tuple[float, float]] | None:
    """計画/実績の時間表なら、映像向けの進捗バーへ切り替える。"""
    aspects = set(_distinct(items, "aspect"))
    if aspects != {"計画", "実績"}:
        return None
    result: dict[str, tuple[float, float]] = {}
    for side in _distinct(items, "side"):
        values: dict[str, float] = {}
        for item in items:
            if item.get("side") != side:
                continue
            match = re.search(r"\d+(?:\.\d+)?", str(item.get("text", "")))
            if match:
                values[str(item["aspect"])] = float(match.group())
        if set(values) != {"計画", "実績"}:
            return None
        result[side] = (values["計画"], values["実績"])
    return result or None


def _build_hours_layout(spec: dict, values: dict[str, tuple[float, float]]) -> VGroup:
    """案件ごとの計画を輪郭、実績を塗りとして重ねた横棒グラフ。"""
    font = resolve_font(spec)
    max_value = max(planned for planned, _actual in values.values())
    bar_max_width = 8.6
    rows = VGroup()
    for side, (planned, actual) in values.items():
        name = Text(side, font=font, font_size=27, color=COLOR_TITLE, weight="BOLD")
        name.set_width(min(name.width, 1.65)) if name.width > 1.65 else None

        planned_width = max(0.4, bar_max_width * planned / max_value)
        actual_width = max(0.08, bar_max_width * actual / max_value)
        track = Rectangle(
            width=planned_width,
            height=0.62,
            color=COLOR_RULE,
            stroke_width=3,
            fill_opacity=0.08,
        )
        fill_color = COLOR_KEY if actual <= planned else COLOR_WARN
        fill = Rectangle(
            width=actual_width,
            height=0.44,
            color=fill_color,
            stroke_width=0,
            fill_color=fill_color,
            fill_opacity=0.9,
        )
        fill.align_to(track, LEFT).move_to([fill.get_center()[0], track.get_center()[1], 0])
        plan_text = Text(f"計画 {planned:g}h", font=font, font_size=17, color=COLOR_FOOTER).next_to(
            track, RIGHT, buff=0.18
        )
        actual_text = (
            Text(f"実績 {actual:g}h", font=font, font_size=21, color=fill_color, weight="BOLD")
            .next_to(fill, UP, buff=0.07)
            .align_to(fill, LEFT)
        )
        bars = VGroup(track, fill, plan_text, actual_text)
        row = VGroup(name, bars).arrange(RIGHT, buff=0.45, aligned_edge=DOWN)
        rows.add(row)
    rows.arrange(DOWN, buff=0.7, aligned_edge=LEFT)

    total_plan = sum(planned for planned, _actual in values.values())
    total_actual = sum(actual for _planned, actual in values.values())
    rate = 100 * total_actual / total_plan if total_plan else 0
    total = VGroup(
        Text("3案件 合計", font=font, font_size=23, color=COLOR_BODY),
        Text(
            f"{total_actual:g} / {total_plan:g}h",
            font=font,
            font_size=32,
            color=COLOR_METRIC,
            weight="BOLD",
        ),
        Text(f"消化率 {rate:.0f}%", font=font, font_size=22, color=COLOR_FOOTER),
    ).arrange(RIGHT, buff=0.35)

    title = wrapped_text(spec["title"], font, 40, COLOR_TITLE, title_width())
    chart = VGroup(rows, total).arrange(DOWN, buff=0.7, aligned_edge=LEFT)
    layout = VGroup(title, chart, source_footer(spec, font)).arrange(DOWN, buff=0.65)
    return fit_to_frame(layout)


def _distinct(items: list[dict], key: str) -> list[str]:
    """初出順の重複なし。列・行の並び順は SceneSpec の記述順に従う。"""
    seen: list[str] = []
    for item in items:
        value = item.get(key)
        if isinstance(value, str) and value not in seen:
            seen.append(value)
    return seen


def _emphasis_color(beat: dict | None, default: str) -> str:
    if beat is None:
        return default
    emphasis = beat.get("emphasis")
    if emphasis == "key":
        return COLOR_KEY
    if emphasis == "warn":
        return COLOR_WARN
    return default


def _extra_mobject(beat: dict, font: str):
    if beat["type"] == "statement":
        return wrapped_text(beat["text"], font, 24, COLOR_BODY, body_width())
    unit = beat.get("unit") or ""
    return VGroup(
        Text(f"{beat['label']}:", font=font, font_size=24, color=COLOR_BODY),
        Text(f"{beat['value']}{unit}", font=font, font_size=28, color=COLOR_METRIC, weight="BOLD"),
    ).arrange(RIGHT, buff=0.3)


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    items = [b for b in spec["beats"] if b["type"] == "comparison_item"]
    numeric = _numeric_hours(items)
    if numeric is not None:
        return _build_hours_layout(spec, numeric)
    extras = [b for b in spec["beats"] if b["type"] in ("statement", "metric")]

    sides = _distinct(items, "side")
    aspects = _distinct(items, "aspect")
    by_cell = {(b["aspect"], b["side"]): b for b in items}

    n_cols = 1 + len(sides)
    n_rows = 1 + len(aspects)
    cell_size = scale_font(22, len(aspects) * len(sides), soft=6, hard=18)
    head_size = scale_font(24, len(sides), soft=2, hard=3)
    # 列幅は表全体を等分し、そこから折り返し桁数を逆算する。
    col_width = table_width() / n_cols - COL_GAP

    def _fit(mobject):
        """列幅を絶対に超えさせない。

        `wrapped_text` は折り返すだけなので、切れ目の少ない文字列
        （"REQ-UI-001〜005（レガシー節）" のような英数字主体の語）は
        col_width を超えたまま残りうる。超えると列が横に重なり、
        列境界に引く縦罫線が本文を貫通する。
        """
        if mobject.width > col_width:
            mobject.scale_to_fit_width(col_width)
        return mobject

    cells: list[VGroup | Text] = []
    # ヘッダ行: 左上は空、以降が side の見出し。
    cells.append(Text("", font=font, font_size=head_size))
    for side in sides:
        cells.append(_fit(wrapped_text(side, font, head_size, COLOR_TITLE, col_width)))
    # 本体: 行見出し + 各 side のセル。
    for aspect in aspects:
        cells.append(_fit(wrapped_text(aspect, font, cell_size, COLOR_TITLE, col_width)))
        for side in sides:
            beat = by_cell.get((aspect, side))
            if beat is None:
                cells.append(Text(EMPTY_CELL, font=font, font_size=cell_size, color=COLOR_FOOTER))
            else:
                cells.append(
                    _fit(
                        wrapped_text(
                            beat["text"],
                            font,
                            cell_size,
                            _emphasis_color(beat, COLOR_BODY),
                            col_width,
                        )
                    )
                )

    # 列幅を明示する。自動幅だと列の実幅がセル内容で決まり、列境界が不定になって
    # 縦罫線が本文を貫通しうる(実際に発生した)。固定幅なら境界を厳密に計算できる。
    aspect_width = col_width * 0.8
    side_width = (table_width() - aspect_width - COL_GAP * len(sides)) / len(sides)
    col_widths = [aspect_width] + [side_width] * len(sides)

    grid = VGroup(*cells)
    grid.arrange_in_grid(
        rows=n_rows,
        cols=n_cols,
        col_alignments="l" + "c" * len(sides),
        col_widths=col_widths,
        buff=(COL_GAP, 0.32),
        flow_order="rd",
    )

    header = VGroup(*cells[:n_cols])
    body = VGroup(*cells[n_cols:])

    # 罫線: ヘッダ下の横線1本と、列間の縦線。
    rules = VGroup()
    top = header.get_bottom()[1] - 0.16
    rules.add(
        Line(
            [grid.get_left()[0], top, 0],
            [grid.get_right()[0], top, 0],
            color=COLOR_RULE,
            stroke_width=2,
        )
    )
    # 列境界は固定幅から厳密に求める(セル geometry からの推定はしない)。
    # 各列の占有幅の境界 = 左端 + 累積幅 + 隙間の中央。
    x = grid.get_left()[0]
    for index, width in enumerate(col_widths[:-1]):
        x += width + COL_GAP / 2
        rules.add(
            Line(
                [x, top, 0],
                [x, grid.get_bottom()[1] - 0.1, 0],
                color=COLOR_RULE,
                stroke_width=1.5,
            )
        )
        x += COL_GAP / 2
        del index

    table = VGroup(header, rules, body)
    if table.width > table_width():
        table.scale_to_fit_width(table_width())

    title = wrapped_text(spec["title"], font, 40, COLOR_TITLE, title_width())
    parts: list = [title, table]
    if extras:
        parts.append(
            VGroup(*[_extra_mobject(b, font) for b in extras]).arrange(
                DOWN, aligned_edge=LEFT, buff=0.3
            )
        )
    parts.append(source_footer(spec, font))
    layout = VGroup(*parts).arrange(DOWN, buff=0.6)
    return fit_to_frame(layout)


def make_scene_classes(spec: dict):
    class ComparisonAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            items = [b for b in spec["beats"] if b["type"] == "comparison_item"]
            if _numeric_hours(items) is not None:
                title, chart, footer = layout
                rows, total = chart
                self.play(FadeIn(title, shift=DOWN * 0.2), run_time=0.8)
                for row in rows:
                    name, bars = row
                    track, fill, plan_text, actual_text = bars
                    self.play(FadeIn(name), Create(track), FadeIn(plan_text), run_time=0.45)
                    self.play(
                        GrowFromEdge(fill, LEFT),
                        FadeIn(actual_text, shift=RIGHT * 0.12),
                        run_time=0.7,
                    )
                self.play(FadeIn(total, shift=UP * 0.15), FadeIn(footer), run_time=0.65)
                self.wait(1.0)
                hold_to(self, spec)
                return
            title = layout[0]
            table = layout[1]
            rest = layout[2:]
            header, rules, body = table
            sides = _distinct([b for b in spec["beats"] if b["type"] == "comparison_item"], "side")
            n_cols = 1 + len(sides)

            table_rows = [
                VGroup(*body[start : start + n_cols]) for start in range(0, len(body), n_cols)
            ]
            budget = choreography.budget_for(spec, beat_count=len(table_rows))
            clock = choreography.BeatClock(self)

            choreography.enter_scene(self, spec)
            self.play(FadeIn(title, shift=DOWN * 0.2), run_time=0.9)
            self.play(FadeIn(header, shift=DOWN * 0.15), run_time=0.6)
            self.play(*[Create(rule) for rule in rules], run_time=0.5)
            # 行(観点)単位で全列を同時に出す。列単位だと視線が比較にならない。
            for row in table_rows:
                clock.mark()
                self.play(FadeIn(row, shift=RIGHT * 0.15), run_time=0.5)
                # 今比べている観点だけを立たせる。
                choreography.focus(self, table_rows, row, budget=budget)
                self.wait(0.5)
            for part in rest:
                self.play(FadeIn(part), run_time=0.5)
            self.wait(1.5)
            choreography.finish(self, spec, items=table_rows, clock=clock)

    class ComparisonStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return ComparisonAnim, ComparisonStatic
