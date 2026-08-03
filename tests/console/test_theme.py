from rich.theme import Theme

from abist_kb.presentation.console.theme import TOKEN_STYLES, SemanticToken, build_theme


def test_all_seven_tokens_exist():
    assert {t.value for t in SemanticToken} == {
        "primary",
        "success",
        "warning",
        "danger",
        "info",
        "muted",
        "accent",
    }


def test_token_styles_match_design_table():
    expected = {
        SemanticToken.PRIMARY: ("bold cyan", "#0891B2", "●"),
        SemanticToken.SUCCESS: ("bold green", "#15803D", "✓"),
        SemanticToken.WARNING: ("bold yellow", "#B45309", "!"),
        SemanticToken.DANGER: ("bold red", "#B91C1C", "×"),
        SemanticToken.INFO: ("blue", "#1D4ED8", "i"),
        SemanticToken.MUTED: ("dim", "#64748B", "-"),
        SemanticToken.ACCENT: ("magenta", "#A21CAF", "◆"),
    }
    for token, (style, web, symbol) in expected.items():
        assert TOKEN_STYLES[token].rich_style == style
        assert TOKEN_STYLES[token].web_hex == web
        assert TOKEN_STYLES[token].symbol == symbol


def test_every_token_has_a_symbol_so_colour_is_never_the_only_signal():
    for token in SemanticToken:
        assert TOKEN_STYLES[token].symbol
        assert len(TOKEN_STYLES[token].symbol) == 1


def test_build_theme_registers_every_token_name():
    theme = build_theme()
    assert isinstance(theme, Theme)
    for token in SemanticToken:
        assert token.value in theme.styles
