"""配色テーマ（Manim 非依存）。

これまで配色は `base.py` の定数1組だけで、動画ごとの表情を変える手段が無かった。
とはいえ**色を自由に書かせない**: 台本を書くのはエージェントなので、色コードを
受け付けると読めない配色が普通に出てくる。選べるのは名前だけにする。

`accent_cycle` は章ごとにアクセント色を回すためのもの。同じ色が最後まで続くと、
章が変わったことが画面から伝わらない。

`domain.scene_spec.THEME_VALUES` がこの辞書のキーの写し（別 venv のため import
できない）。ずれると検証を通った spec が描画で落ちるので、整合テストで縛る。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    """1つの配色。テンプレートはここからしか色を取らない。"""

    name: str
    background: str
    title: str
    body: str
    accent: str
    metric: str
    footer: str
    box: str
    rule: str
    decision: str
    key: str
    warn: str
    #: 章ごとに回すアクセント色。1本の動画の中で章の変わり目を色で示す。
    accent_cycle: tuple[str, ...]
    #: チャートの系列色（隣り合う系列が識別できる順に並べる）。
    chart_colors: tuple[str, ...]


#: 既定。**現行の `base.py` の値そのまま**（見た目を変えずにテーマ機構だけ入れる）。
_DARK = Theme(
    name="dark",
    background="#000000",
    title="#ffffff",
    body="#e6e6e6",
    accent="#5b9bd5",
    metric="#f0c987",
    footer="#9a9a9a",
    box="#5b9bd5",
    rule="#4a4a4a",
    decision="#c9a0dc",
    key="#f0c987",
    warn="#e06c75",
    accent_cycle=("#5b9bd5", "#7fc8a9", "#f0c987", "#c9a0dc"),
    chart_colors=("#5b9bd5", "#f0c987", "#7fc8a9", "#c9a0dc", "#e06c75", "#9aa7b1"),
)

#: 落ち着いた紺。報告・進捗の長尺向け（暗い青地で目が疲れにくい）。
_NAVY = Theme(
    name="navy",
    background="#0d1b2a",
    title="#f2f6fa",
    body="#dbe4ec",
    accent="#7cc4f0",
    metric="#ffd88a",
    footer="#8fa3b5",
    box="#7cc4f0",
    rule="#2c4257",
    decision="#b6a8e6",
    key="#ffd88a",
    warn="#ef8f88",
    accent_cycle=("#7cc4f0", "#8fd6b4", "#ffd88a", "#b6a8e6"),
    chart_colors=("#7cc4f0", "#ffd88a", "#8fd6b4", "#b6a8e6", "#ef8f88", "#8fa3b5"),
)

#: 暖色寄り。研修・チュートリアルの取っつきやすさ向け。
_WARM = Theme(
    name="warm",
    background="#1a1512",
    title="#fdf6ee",
    body="#ece0d3",
    accent="#e8a35c",
    metric="#f2d06b",
    footer="#a89684",
    box="#e8a35c",
    rule="#4a3c31",
    decision="#d0a3c8",
    key="#f2d06b",
    warn="#e0705f",
    accent_cycle=("#e8a35c", "#9ec9a0", "#f2d06b", "#d0a3c8"),
    chart_colors=("#e8a35c", "#f2d06b", "#9ec9a0", "#d0a3c8", "#e0705f", "#a89684"),
)

THEMES: dict[str, Theme] = {"dark": _DARK, "navy": _NAVY, "warm": _WARM}
DEFAULT_THEME = "dark"


def get_theme(name: str | None) -> Theme:
    """名前からテーマを引く（未知の名前は既定へ落とす）。"""
    return THEMES.get(str(name or ""), THEMES[DEFAULT_THEME])


def accent_for(theme: Theme, accent_index: int | None) -> str:
    """章番号からアクセント色を選ぶ（指定が無ければテーマの基本色）。"""
    if not isinstance(accent_index, int) or isinstance(accent_index, bool) or accent_index < 0:
        return theme.accent
    cycle = theme.accent_cycle or (theme.accent,)
    return cycle[accent_index % len(cycle)]


__all__ = ["DEFAULT_THEME", "THEMES", "Theme", "accent_for", "get_theme"]
