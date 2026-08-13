"""chart 用テンプレート: 数値を棒・折れ線として見せる。

Manim の `Axes` / `DecimalNumber` は使わない —— 目盛りラベルが `MathTex` を通り、
base.py の LaTeX 不使用方針に反する。`comparison_v1` の隠しグラフ
（`_build_hours_layout`）でやっていた「手描きの矩形と Text」を一般化する。

build_final_layout(spec) は最終状態の Mobject ツリーを組むだけの純粋レイアウト関数。
"""

from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Create,
    Dot,
    FadeIn,
    GrowFromEdge,
    Line,
    Polygram,
    Rectangle,
    Scene,
    Text,
    VGroup,
)

from templates import choreography
from templates.base import (
    body_width,
    card_title,
    content_width,
    fit_to_frame,
    is_portrait,
    resolve_font,
    resolve_theme,
    scale_font,
    source_footer,
    wrapped_text,
)

#: 棒に使える最大の長さ（Manim の単位系）。
def bar_max_width() -> float:
    return content_width(1.6)


#: 1本の棒の高さ。
BAR_HEIGHT = 0.5
#: グループ棒のときの、系列内の棒同士の隙間。
BAR_GAP = 0.1
#: 折れ線グラフの描画領域の高さ。
LINE_CHART_HEIGHT = 3.2


def _format_value(value: float, unit: str) -> str:
    text = f"{value:g}"
    return f"{text}{unit}" if unit else text


def _max_value(series: list[dict]) -> float:
    values = [
        float(entry["value"]) for beat in series for entry in beat.get("values") or []
    ]
    return max(values) if values and max(values) > 0 else 1.0


def _bar_rows(spec: dict, series: list[dict], font: str, theme) -> VGroup:
    """系列ごとに、値の数だけ棒を並べる（bar / grouped_bar / progress 共通）。"""
    variant = (spec.get("chart") or {}).get("variant", "bar")
    peak = _max_value(series)
    width = bar_max_width()
    label_size = scale_font(24, len(series), soft=4, hard=8)
    rows = VGroup()

    for index, beat in enumerate(series):
        unit = beat.get("unit") or ""
        entries = beat.get("values") or []
        label = wrapped_text(
            beat["label"], font, label_size, theme.title, content_width() * 0.22, weight="BOLD"
        )
        bars = VGroup()
        for value_index, entry in enumerate(entries):
            value = float(entry["value"])
            color = theme.chart_colors[
                (index if variant == "line" else value_index) % len(theme.chart_colors)
            ]
            if variant == "progress" and value_index == 0:
                # 1本目を「枠（目標）」、2本目以降を「中身（実績）」として重ねる。
                color = theme.rule
            bar = Rectangle(
                width=max(0.08, width * value / peak),
                height=BAR_HEIGHT,
                stroke_width=2 if variant == "progress" and value_index == 0 else 0,
                color=color,
                fill_color=color,
                fill_opacity=0.12 if variant == "progress" and value_index == 0 else 0.9,
            )
            caption = Text(
                f"{entry['name']} {_format_value(value, unit)}",
                font=font,
                font_size=17,
                color=theme.footer if value_index == 0 else color,
            )
            row = VGroup(bar, caption.next_to(bar, RIGHT, buff=0.18))
            row.shift(LEFT * (row.get_left()[0] - bar.get_left()[0]))
            bars.add(row)
        bars.arrange(DOWN, buff=BAR_GAP, aligned_edge=LEFT)
        rows.add(VGroup(label, bars).arrange(RIGHT, buff=0.4, aligned_edge=UP))
    rows.arrange(DOWN, buff=0.5, aligned_edge=LEFT)
    return rows


def _line_chart(spec: dict, series: list[dict], font: str, theme) -> VGroup:
    """折れ線。軸は自前の直線2本で描く（Axes は LaTeX を引くので使わない）。"""
    peak = _max_value(series)
    width = bar_max_width()
    height = LINE_CHART_HEIGHT * (0.7 if is_portrait() else 1.0)
    baseline = Line(LEFT * width / 2, RIGHT * width / 2, color=theme.rule, stroke_width=2)
    axis = Line(
        LEFT * width / 2, LEFT * width / 2 + UP * height, color=theme.rule, stroke_width=2
    )
    chart = VGroup(baseline, axis)

    names: list[str] = []
    for index, beat in enumerate(series):
        entries = beat.get("values") or []
        if not entries:
            continue
        color = theme.chart_colors[index % len(theme.chart_colors)]
        step = width / max(1, len(entries) - 1) if len(entries) > 1 else 0.0
        points = []
        for value_index, entry in enumerate(entries):
            x = -width / 2 + step * value_index
            y = height * float(entry["value"]) / peak
            points.append([x, y, 0])
            if index == 0:
                names.append(str(entry["name"]))
        if len(points) >= 2:
            chart.add(Polygram([points], color=color, stroke_width=4, fill_opacity=0.0))
        for point in points:
            chart.add(Dot(point, radius=0.055, color=color))
        chart.add(
            Text(beat["label"], font=font, font_size=18, color=color).next_to(
                points[-1], RIGHT, buff=0.15
            )
        )

    if names:
        step = width / max(1, len(names) - 1) if len(names) > 1 else 0.0
        for value_index, name in enumerate(names):
            tick = Text(name, font=font, font_size=15, color=theme.footer)
            tick.next_to([-width / 2 + step * value_index, 0, 0], DOWN, buff=0.15)
            chart.add(tick)
    # 軸と目盛りの位置決めのため、原点を左下に置いてから中央へ寄せる。
    chart.shift(UP * height / 2)
    return chart


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    theme = resolve_theme(spec)
    series = [b for b in spec["beats"] if b["type"] == "chart_series"]
    statements = [b for b in spec["beats"] if b["type"] == "statement"]
    variant = (spec.get("chart") or {}).get("variant", "bar")

    body = (
        _line_chart(spec, series, font, theme)
        if variant == "line"
        else _bar_rows(spec, series, font, theme)
    )

    parts: list = [card_title(spec["title"], font, font_size=38, color=theme.title)]
    value_label = (spec.get("chart") or {}).get("value_label")
    if value_label:
        parts.append(Text(str(value_label), font=font, font_size=19, color=theme.footer))
    parts.append(body)
    for beat in statements:
        parts.append(wrapped_text(beat["text"], font, 22, theme.body, body_width()))
    parts.append(source_footer(spec, font))
    layout = VGroup(*parts).arrange(DOWN, buff=0.5)
    return fit_to_frame(layout)


def make_scene_classes(spec: dict):
    series = [b for b in spec["beats"] if b["type"] == "chart_series"]
    variant = (spec.get("chart") or {}).get("variant", "bar")

    class ChartAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            clock = choreography.BeatClock(self)
            choreography.enter_scene(self, spec)

            head = layout[0]
            body_index = 2 if len(layout) > 2 and not isinstance(layout[1], VGroup) else 1
            self.play(FadeIn(head, shift=DOWN * 0.2), run_time=0.8)
            for part in layout[1:body_index]:
                self.play(FadeIn(part), run_time=0.35)

            body = layout[body_index]
            if variant == "line":
                # 軸を先に引いてから線を描き起こす（データが伸びる感じを出す）。
                self.play(Create(body[0]), Create(body[1]), run_time=0.6)
                for part in body[2:]:
                    clock.mark()
                    self.play(Create(part) if isinstance(part, Polygram) else FadeIn(part),
                              run_time=0.4)
            else:
                for row in body:
                    clock.mark()
                    label, bars = row
                    self.play(FadeIn(label, shift=RIGHT * 0.15), run_time=0.35)
                    for bar_row in bars:
                        # 棒は左端から伸ばす（数字が育つのを見せる）。
                        self.play(
                            GrowFromEdge(bar_row[0], LEFT),
                            FadeIn(bar_row[1], shift=RIGHT * 0.1),
                            run_time=0.55,
                        )
                    self.wait(0.25)

            for part in layout[body_index + 1 :]:
                self.play(FadeIn(part), run_time=0.4)
            self.wait(1.2)
            choreography.finish(self, spec, items=list(layout[body_index]), clock=clock)

    class ChartStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return ChartAnim, ChartStatic
