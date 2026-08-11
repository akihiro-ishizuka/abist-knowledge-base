"""テンプレート共通: フォント解決・配色・出典フッタ・レイアウトユーティリティ

LaTeX（Tex / MathTex）は使わない。テキストは必ず Pango 系（Text / MarkupText）。
"""
from __future__ import annotations

import os

import functools

from manim import DOWN, LEFT, VGroup, Text, config

from templates.layout import display_width, wrap_cjk

# 既定は Windows 10/11 標準搭載の日本語フォント。優先順位:
#   spec["font"] > 環境変数 KB_VISUALIZE_FONT > DEFAULT_FONT（無ければ FALLBACK_FONT）
DEFAULT_FONT = "Yu Gothic UI"
FALLBACK_FONT = "Meiryo"

# 配色（テンプレート間で統一する）
COLOR_TITLE = "#ffffff"
COLOR_BODY = "#e6e6e6"
COLOR_ACCENT = "#5b9bd5"
COLOR_METRIC = "#f0c987"
COLOR_FOOTER = "#9a9a9a"
COLOR_BOX = "#5b9bd5"
#: 区切り線・クラスタ枠など、主張させたくない罫線の色。
COLOR_RULE = "#4a4a4a"
#: 条件分岐(ひし形)の枠色。
COLOR_DECISION = "#c9a0dc"
#: emphasis="key" の強調色。
COLOR_KEY = "#f0c987"
#: emphasis="warn" の注意色。
COLOR_WARN = "#e06c75"

#: 出典フッタの1行あたりの最大桁数(半角換算)。全角で約52文字。
FOOTER_MAX_COLS = 104


def _installed_fonts() -> set[str]:
    try:
        import manimpango

        return set(manimpango.list_fonts())
    except Exception:
        return set()


def resolve_font(spec: dict) -> str:
    """spec / 環境変数 / 既定 の順でフォント名を決める。

    要求フォントが見つからなくても Pango 側のフォールバックで描画は続くため、
    ここでは名前の解決だけを行い、失敗にはしない。
    """
    requested = spec.get("font") or os.environ.get("KB_VISUALIZE_FONT") or DEFAULT_FONT
    fonts = _installed_fonts()
    if fonts and requested not in fonts and FALLBACK_FONT in fonts:
        return FALLBACK_FONT
    return requested


@functools.lru_cache(maxsize=32)
def _em_width(font: str, font_size: float) -> float:
    """全角1文字の実描画幅(Manim の単位系)。

    折り返し桁数をフォントサイズから経験則の定数で決めると環境差で破綻するので、
    実際に1文字だけ組んで測る。(font, size) ごとに1回で済むのでコストは無視できる。
    """
    return Text("あ", font=font, font_size=font_size).width


def max_cols_for(font: str, font_size: float, max_width_units: float) -> int:
    """指定幅に収まる半角桁数を返す(`wrap_cjk` の max_cols に渡す)。"""
    half_em = _em_width(font, font_size) / 2
    if half_em <= 0:
        return 8
    return max(8, int(max_width_units / half_em))


def wrapped_text(
    body: str, font: str, font_size: float, color: str, max_width_units: float, **kwargs
) -> Text:
    """`max_width_units` に収まるよう折り返した `Text` を返す。

    Manim の `Text(width=...)` は折り返しではなく縮小なので、改行は自前で入れる。
    """
    cols = max_cols_for(font, font_size, max_width_units)
    return Text(
        "\n".join(wrap_cjk(body, cols)),
        font=font,
        font_size=font_size,
        color=color,
        line_spacing=0.7,
        **kwargs,
    )


def source_footer(spec: dict, font: str) -> VGroup:
    """出典フッタ。sources の path:行範囲 を列挙する（重複は畳む）。

    全件を1行に詰めると、出典3件でも約110文字になり `fit_to_frame` が
    **レイアウト全体**を1/3程度に縮小してしまう(出典必須ポリシーを守ったまま
    図が読めなくなる最大の要因だった)。件数は減らさず、折り返して高さへ逃がす。
    フッタが広すぎる場合もフッタ単体だけを縮め、本文は巻き込まない。
    """
    refs = sorted({f'{s["path"]}:{s["start_line"]}-{s["end_line"]}' for s in spec.get("sources", [])})
    body = "出典: " + " / ".join(refs) if refs else "出典なし（装飾のみ）"
    lines = wrap_cjk(body, FOOTER_MAX_COLS)
    group = VGroup(
        *[Text(line, font=font, font_size=16, color=COLOR_FOOTER) for line in lines]
    ).arrange(DOWN, aligned_edge=LEFT, buff=0.08)
    max_width = config.frame_width - 1.2
    if group.width > max_width:
        group.scale_to_fit_width(max_width)
    return group


def scale_font(base_size: float, count: int, *, soft: int, hard: int) -> float:
    """要素数から本文のフォントサイズを決める。

    `fit_to_frame` はレイアウト**全体**を等比縮小するため、要素が増えると
    タイトルやフッタまで巻き込んで潰れる。本文だけを先に縮めておくことで、
    最後の砦である `fit_to_frame` が発動しにくくなる。

    count <= soft なら base_size のまま、hard で 0.7 倍まで線形に落とす。
    """
    if count <= soft or hard <= soft:
        return base_size
    ratio = min(1.0, (count - soft) / (hard - soft))
    return base_size * (1.0 - 0.3 * ratio)


def fit_to_frame(group: VGroup, margin: float = 0.6) -> VGroup:
    """フレームからはみ出す場合だけ縮小する（拡大はしない）"""
    max_width = config.frame_width - margin * 2
    max_height = config.frame_height - margin * 2
    if group.width > max_width:
        group.scale_to_fit_width(max_width)
    if group.height > max_height:
        group.scale_to_fit_height(max_height)
    return group


def stack(*mobjects, buff: float = 0.5) -> VGroup:
    return VGroup(*mobjects).arrange(DOWN, buff=buff)
