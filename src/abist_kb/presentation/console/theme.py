"""セマンティックトークン(設計書 §6.1)。

色だけで状態を伝えないため、各トークンは必ず記号を伴う。
CLI(Rich)は rich_style を使う。web_hex は旧 Web 面向けの名残フィールド。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rich.theme import Theme


class SemanticToken(StrEnum):
    PRIMARY = "primary"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"
    INFO = "info"
    MUTED = "muted"
    ACCENT = "accent"


@dataclass(frozen=True, slots=True)
class TokenStyle:
    rich_style: str
    web_hex: str
    symbol: str


TOKEN_STYLES: dict[SemanticToken, TokenStyle] = {
    SemanticToken.PRIMARY: TokenStyle("bold cyan", "#0891B2", "●"),
    SemanticToken.SUCCESS: TokenStyle("bold green", "#15803D", "✓"),
    SemanticToken.WARNING: TokenStyle("bold yellow", "#B45309", "!"),
    SemanticToken.DANGER: TokenStyle("bold red", "#B91C1C", "×"),
    SemanticToken.INFO: TokenStyle("blue", "#1D4ED8", "i"),
    SemanticToken.MUTED: TokenStyle("dim", "#64748B", "-"),
    SemanticToken.ACCENT: TokenStyle("magenta", "#A21CAF", "◆"),
}


def build_theme() -> Theme:
    """Rich Console 用のテーマ。"""
    return Theme({token.value: style.rich_style for token, style in TOKEN_STYLES.items()})
