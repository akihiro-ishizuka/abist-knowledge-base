"""NiceGUI 用テーマ(設計書 §6.1)。ライト/ダーク両テーマ、色+記号を必ず併記する。

`presentation/console/theme.py::TOKEN_STYLES` の `web_hex`/`symbol` を
そのまま使う — 色の値はここで再定義しない(design/plans/M6-M10-remaining.md
Task 6.2: 「Web の hex 値で実装。色だけで状態を伝えない」)。
"""

from __future__ import annotations

from abist_kb.presentation.console.theme import TOKEN_STYLES, SemanticToken

#: NiceGUI の `ui.dark_mode()` が参照するライト/ダーク共通の primary 色。
#: セマンティックトークン `primary` の web hex をそのまま使う。
PRIMARY_COLOR = TOKEN_STYLES[SemanticToken.PRIMARY].web_hex


def badge_label(token: SemanticToken, text: str) -> str:
    """記号+ラベルの文字列(色だけで状態を伝えないための最低限のフォールバック)。

    `記号 テキスト` の形式。呼び出し側(NiceGUI の `ui.badge`/`ui.chip` 等)は
    これをラベルとして表示しつつ、`token_color(token)` を背景色に使う。
    """
    style = TOKEN_STYLES[token]
    return f"{style.symbol} {text}"


def token_color(token: SemanticToken) -> str:
    """トークンに対応する web hex(NiceGUI の `color=` 引数へそのまま渡せる)。"""
    return TOKEN_STYLES[token].web_hex


def apply_theme(dark: bool) -> None:
    """NiceGUI の `ui.dark_mode` と primary 色を設定する(ライト/ダーク両対応)。

    NiceGUI 未インストール環境でも import 時点では失敗しないよう、
    `ui` への依存はこの関数内に閉じ込める(呼び出し時にのみ import する)。
    """
    from nicegui import ui

    ui.colors(primary=PRIMARY_COLOR)
    ui.dark_mode(dark)


__all__ = ["PRIMARY_COLOR", "apply_theme", "badge_label", "token_color"]
