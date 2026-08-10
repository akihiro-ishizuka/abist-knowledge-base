"""セマンティックトークン(設計書 §6.1)。

色だけで状態を伝えないため、各トークンは必ず記号を伴う。
CLI(Rich)の Console テーマはここで定義したスタイルだけを登録する。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rich.theme import Theme


class SemanticToken(StrEnum):
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"
    INFO = "info"
    MUTED = "muted"


@dataclass(frozen=True, slots=True)
class TokenStyle:
    rich_style: str
    symbol: str


TOKEN_STYLES: dict[SemanticToken, TokenStyle] = {
    SemanticToken.SUCCESS: TokenStyle("bold green", "✓"),
    SemanticToken.WARNING: TokenStyle("bold yellow", "!"),
    SemanticToken.DANGER: TokenStyle("bold red", "×"),
    SemanticToken.INFO: TokenStyle("blue", "i"),
    SemanticToken.MUTED: TokenStyle("dim", "-"),
}


def build_theme() -> Theme:
    """Rich Console 用のテーマ。"""
    return Theme({token.value: style.rich_style for token, style in TOKEN_STYLES.items()})
