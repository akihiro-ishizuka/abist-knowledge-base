"""code_block: コード片の提示。等幅フォントで原文をそのまま出す。

`Code` mobject は使わない —— pygments のテーマ依存で日本語コメントの描画が崩れ、
行番号の付き方も Manim のバージョンで揺れるため。等幅 `Text` に落として
**行数と桁数を自分で制御する**ほうが再現性が高い。
"""

from __future__ import annotations

from manim import DOWN, LEFT, FadeIn, Rectangle, Scene, Text, VGroup

from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_FOOTER,
    card_title,
    content_width,
    fit_to_frame,
    resolve_font,
    source_footer,
)

#: 等幅フォントの候補（Windows 標準搭載を優先）。
MONO_FONTS = ("Consolas", "MS Gothic", "Courier New")
#: 1画面に載せる最大行数。超えた分は末尾を省略記号で示す。
MAX_CODE_LINES = 16
#: 1行の最大桁数（半角換算）。
MAX_CODE_COLS = 72
#: 省略された行があることを示す記号。
ELLIPSIS_LINE = "..."


def resolve_mono_font() -> str:
    """等幅フォントを1つ選ぶ（見つからなければ先頭候補のまま Pango に任せる）。"""
    try:
        import manimpango

        installed = set(manimpango.list_fonts())
    except Exception:
        return MONO_FONTS[0]
    for name in MONO_FONTS:
        if name in installed:
            return name
    return MONO_FONTS[0]


def clip_code(text: str) -> tuple[list[str], bool]:
    """コードを行数・桁数の上限へ収める（切った事実を返す）。"""
    lines = text.replace("\t", "    ").splitlines() or [""]
    truncated = len(lines) > MAX_CODE_LINES
    kept = [line[:MAX_CODE_COLS] for line in lines[:MAX_CODE_LINES]]
    truncated = truncated or any(len(line) > MAX_CODE_COLS for line in lines[:MAX_CODE_LINES])
    return kept, truncated


def build_final_layout(spec: dict) -> VGroup:
    font = resolve_font(spec)
    mono = resolve_mono_font()
    blocks = [b for b in spec["beats"] if b["type"] == "code"]

    rendered = VGroup()
    for beat in blocks:
        lines, truncated = clip_code(beat["text"])
        if truncated:
            lines = [*lines, ELLIPSIS_LINE]
        body = Text(
            "\n".join(lines), font=mono, font_size=20, color=COLOR_BODY, line_spacing=0.6
        )
        max_width = content_width() - 0.6
        if body.width > max_width:
            body.scale_to_fit_width(max_width)
        frame = Rectangle(
            width=body.width + 0.5,
            height=body.height + 0.4,
            color=COLOR_ACCENT,
            stroke_width=2,
            fill_opacity=0.06,
        )
        body.move_to(frame.get_center())
        parts: list = [VGroup(frame, body)]
        if beat.get("language"):
            parts.insert(0, Text(beat["language"], font=font, font_size=18, color=COLOR_FOOTER))
        rendered.add(VGroup(*parts).arrange(DOWN, aligned_edge=LEFT, buff=0.12))
    if len(rendered):
        rendered.arrange(DOWN, buff=0.4)

    layout = VGroup(
        card_title(spec["title"], font, font_size=36), rendered, source_footer(spec, font)
    ).arrange(DOWN, buff=0.45)
    return fit_to_frame(layout)


def make_scene_classes(spec: dict):
    class CodeBlockAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            self.play(FadeIn(layout[0], shift=DOWN * 0.2), run_time=0.7)
            for block in layout[1]:
                self.play(FadeIn(block), run_time=0.7)
                self.wait(1.2)
            self.play(FadeIn(layout[2]), run_time=0.4)
            self.wait(1.2)

    class CodeBlockStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return CodeBlockAnim, CodeBlockStatic
