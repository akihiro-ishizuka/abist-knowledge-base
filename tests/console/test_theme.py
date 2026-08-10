from rich.theme import Theme

from abist_kb.presentation.console.theme import TOKEN_STYLES, SemanticToken, build_theme


def test_all_tokens_exist():
    assert {t.value for t in SemanticToken} == {
        "success",
        "warning",
        "danger",
        "info",
        "muted",
    }


def test_token_styles_match_design_table():
    expected = {
        SemanticToken.SUCCESS: ("bold green", "✓"),
        SemanticToken.WARNING: ("bold yellow", "!"),
        SemanticToken.DANGER: ("bold red", "×"),
        SemanticToken.INFO: ("blue", "i"),
        SemanticToken.MUTED: ("dim", "-"),
    }
    for token, (style, symbol) in expected.items():
        assert TOKEN_STYLES[token].rich_style == style
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
