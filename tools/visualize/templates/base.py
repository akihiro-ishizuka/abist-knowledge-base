"""テンプレート共通: フォント解決・配色・出典フッタ・レイアウトユーティリティ

LaTeX（Tex / MathTex）は使わない。テキストは必ず Pango 系（Text / MarkupText）。
"""

from __future__ import annotations

import functools
import os
from dataclasses import replace

from manim import DOWN, LEFT, Text, VGroup, config

from templates.layout import wrap_cjk
from templates.theme import DEFAULT_THEME, Theme, accent_for, get_theme

# 既定は Windows 10/11 標準搭載の日本語フォント。優先順位:
#   spec["font"] > 環境変数 KB_VISUALIZE_FONT > DEFAULT_FONT（無ければ FALLBACK_FONT）
DEFAULT_FONT = "Yu Gothic UI"
FALLBACK_FONT = "Meiryo"

# 配色。正本は `templates/theme.py`。ここの `COLOR_*` は既定テーマ（dark）の別名で、
# テーマを見ない既存テンプレートがそのまま動くように残している。
# テーマを反映したい箇所は `resolve_theme(spec)` を使う。
_DEFAULT_THEME = get_theme(DEFAULT_THEME)
COLOR_TITLE = _DEFAULT_THEME.title
COLOR_BODY = _DEFAULT_THEME.body
COLOR_ACCENT = _DEFAULT_THEME.accent
COLOR_METRIC = _DEFAULT_THEME.metric
COLOR_FOOTER = _DEFAULT_THEME.footer
COLOR_BOX = _DEFAULT_THEME.box
#: 区切り線・クラスタ枠など、主張させたくない罫線の色。
COLOR_RULE = _DEFAULT_THEME.rule
#: 条件分岐(ひし形)の枠色。
COLOR_DECISION = _DEFAULT_THEME.decision
#: emphasis="key" の強調色。
COLOR_KEY = _DEFAULT_THEME.key
#: emphasis="warn" の注意色。
COLOR_WARN = _DEFAULT_THEME.warn


def resolve_theme(spec: dict) -> Theme:
    """spec / 環境変数 / 既定 の順でテーマを決め、章別アクセントを反映する。

    `resolve_font` と同じ流儀。**色コードは受け付けない**（名前だけ）ので、
    エージェントが読めない配色を選ぶ余地が無い。
    """
    name = spec.get("theme") or os.environ.get("KB_VISUALIZE_THEME") or DEFAULT_THEME
    theme = get_theme(name)
    accent = accent_for(theme, spec.get("accent_index"))
    if accent == theme.accent:
        return theme
    return replace(theme, accent=accent, box=accent)

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
    refs = sorted(
        {
            f"{s['path'].replace(chr(92), '/').rsplit('/', 1)[-1]}:"
            f"{s['start_line']}-{s['end_line']}"
            for s in spec.get("sources", [])
        }
    )
    if not refs:
        # 装飾カードに内部検証用の文言を表示しない。
        return VGroup()
    body = "出典: " + " / ".join(refs)
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


def is_portrait() -> bool:
    """縦型フレーム（9:16）かどうか。

    `render_scene._frame_config` が `config.frame_width/height` を設定済みなので、
    テンプレートは spec を見ずに実際のフレーム形状から判断できる。
    """
    return config.frame_width < config.frame_height


def content_width(margin: float = 0.7) -> float:
    """本文に使える幅（フレーム単位）。縦型では自動的に狭くなる。"""
    return max(1.0, config.frame_width - margin * 2)


# 各テンプレートが使う折り返し幅。以前は 16:9 のフレーム幅（14.222）から逆算した
# 定数を各ファイルに直書きしていたため、縦型（フレーム幅 4.5）では指定幅が
# フレームより広くなり、`fit_to_frame` が**図全体**を潰していた。
# フレームから毎回引き直すことで、縦型でも文字サイズを保ったまま行が短くなる。


def title_width() -> float:
    """見出しの折り返し幅。"""
    return content_width(1.1)


def body_width() -> float:
    """本文・補足の折り返し幅。"""
    return content_width(0.8)


def table_width() -> float:
    """表・グリッド全体の最大幅。"""
    return content_width(0.6)


def node_width(preferred: float = 2.6) -> float:
    """フロー図・関係図の箱1つぶんの幅。

    縦型では横に2つ並べるだけでも溢れるので、フレーム幅から上限を掛け直す。
    """
    return max(1.4, min(preferred, content_width() / 2.2))


def safe_area_rect(margin_x: float = 0.6, margin_y: float = 0.5) -> tuple[float, float]:
    """セーフエリアの幅・高さ。字幕やフッタはこの内側に置く。"""
    return (
        max(1.0, config.frame_width - margin_x * 2),
        max(1.0, config.frame_height - margin_y * 2),
    )


def card_title(text: str, font: str, *, font_size: float = 48, color: str = COLOR_TITLE) -> Text:
    """カード系テンプレートの見出し。縦型では自動で一段小さくする。"""
    size = font_size * (0.78 if is_portrait() else 1.0)
    return wrapped_text(text, font, size, color, content_width(), weight="BOLD")


#: `hold_to` が待てる上限（秒）。暴走した指定でレンダリングが張り付かないための保険。
MAX_HOLD_SEC = 30.0


def hold_to(scene, spec: dict) -> None:
    """シーンの尺が `spec["min_duration_sec"]` に届くまで最終フレームを保持する。

    **これは内容の水増しではなく間の調整。** ナレーションが映像より長いと、
    音声を映像へ載せる段で末尾が切れる（あるいは映像が黒く伸びる）。
    尺は「読み上げに必要な長さ」から決まるので、映像側をそこへ合わせる。

    実経過は `scene.renderer.time`（既に再生したアニメーションの合計秒）を見る。
    """
    target = spec.get("min_duration_sec")
    if not isinstance(target, (int, float)) or isinstance(target, bool) or target <= 0:
        return
    elapsed = float(getattr(scene.renderer, "time", 0.0) or 0.0)
    remaining = min(float(target) - elapsed, MAX_HOLD_SEC)
    if remaining > 0.05:
        scene.wait(remaining)
