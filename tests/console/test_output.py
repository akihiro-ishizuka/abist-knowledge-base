import io

import pytest

from abist_kb.presentation.console.output import (
    ColorMode,
    OutputMode,
    resolve_color_system,
    resolve_output_mode,
)


class FakeStream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("requested", "tty", "env", "expected"),
    [
        ("auto", True, {}, OutputMode.RICH),
        ("auto", False, {}, OutputMode.PLAIN),
        ("rich", False, {}, OutputMode.RICH),
        ("rich", True, {}, OutputMode.RICH),
        ("plain", True, {}, OutputMode.PLAIN),
        ("json", True, {}, OutputMode.JSON),
        ("json", False, {}, OutputMode.JSON),
        ("auto", True, {"TERM": "dumb"}, OutputMode.PLAIN),
        ("rich", True, {"TERM": "dumb"}, OutputMode.PLAIN),
        ("auto", True, {"NO_COLOR": "1"}, OutputMode.RICH),
    ],
)
def test_resolve_output_mode(requested, tty, env, expected):
    assert resolve_output_mode(requested, stream=FakeStream(tty), env=env) == expected


def test_unknown_output_mode_is_rejected():
    with pytest.raises(ValueError):
        resolve_output_mode("fancy", stream=FakeStream(True), env={})


@pytest.mark.parametrize(
    ("mode", "color", "tty", "env", "expected"),
    [
        (OutputMode.RICH, ColorMode.AUTO, True, {}, "auto"),
        (OutputMode.RICH, ColorMode.AUTO, False, {}, None),
        (OutputMode.RICH, ColorMode.AUTO, True, {"NO_COLOR": "1"}, None),
        (OutputMode.RICH, ColorMode.ALWAYS, True, {"NO_COLOR": "1"}, None),
        (OutputMode.RICH, ColorMode.ALWAYS, False, {}, "auto"),
        (OutputMode.RICH, ColorMode.NEVER, True, {}, None),
        (OutputMode.PLAIN, ColorMode.ALWAYS, True, {}, None),
        (OutputMode.JSON, ColorMode.ALWAYS, True, {}, None),
    ],
)
def test_resolve_color_system(mode, color, tty, env, expected):
    assert resolve_color_system(mode, color, stream=FakeStream(tty), env=env) == expected
