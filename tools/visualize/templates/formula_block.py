"""formula_block: 数式・計算式の提示。**既定は Text 近似で LaTeX を使わない**。

base.py の「LaTeX を使わない」方針をここでも守る。TeX 依存を持ち込むと
`.venv-visualize` の再現性（requirements-visualize.txt でピン留めした 33 件）が
崩れ、Windows では TeX Live の導入まで要求することになる。
上付き・下付き・分数は Unicode の記号で近似する。
"""

from __future__ import annotations

from manim import DOWN, FadeIn, Scene, Text, VGroup

from templates.base import (
    COLOR_BODY,
    COLOR_FOOTER,
    COLOR_METRIC,
    card_title,
    content_width,
    fit_to_frame,
    hold_to,
    resolve_font,
    scale_font,
    source_footer,
    wrapped_text,
)

#: ASCII の演算子を読みやすい記号へ置き換える（LaTeX を使わないための近似）。
OPERATOR_MAP = {
    "<=": "≤",
    ">=": "≥",
    "!=": "≠",
    "->": "→",
    "*": "×",
    "/": "÷",
}


def approximate(text: str) -> str:
    """式を Unicode 記号へ近似する（元の文字列は壊さない範囲で）。"""
    result = text
    for source, target in OPERATOR_MAP.items():
        result = result.replace(source, target)
    return result


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    formulas = [b for b in spec["beats"] if b["type"] == "formula"]
    size = scale_font(40, len(formulas), soft=1, hard=4)

    blocks = VGroup()
    for beat in formulas:
        parts: list = [
            Text(approximate(beat["text"]), font=font, font_size=size, color=COLOR_METRIC)
        ]
        if beat.get("caption"):
            parts.append(
                wrapped_text(beat["caption"], font, size * 0.5, COLOR_BODY, content_width())
            )
        blocks.add(VGroup(*parts).arrange(DOWN, buff=0.22))
    if len(blocks):
        blocks.arrange(DOWN, buff=0.5)
    max_width = content_width()
    if blocks.width > max_width:
        blocks.scale_to_fit_width(max_width)

    note = Text("※ 数式は記号近似で表示しています", font=font, font_size=16, color=COLOR_FOOTER)
    layout = VGroup(
        card_title(spec["title"], font, font_size=38), blocks, note, source_footer(spec, font)
    ).arrange(DOWN, buff=0.45)
    return fit_to_frame(layout)


def make_scene_classes(spec: dict):
    class FormulaBlockAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            self.play(FadeIn(layout[0], shift=DOWN * 0.2), run_time=0.7)
            for block in layout[1]:
                self.play(FadeIn(block, scale=0.9), run_time=0.8)
                self.wait(1.2)
            for part in layout[2:]:
                self.play(FadeIn(part), run_time=0.4)
            self.wait(1.2)
            hold_to(self, spec)

    class FormulaBlockStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return FormulaBlockAnim, FormulaBlockStatic
