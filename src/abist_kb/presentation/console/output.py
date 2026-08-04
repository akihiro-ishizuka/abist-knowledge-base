"""出力モードと色の解決(設計書 §6.3)。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from typing import IO, Any


class OutputMode(StrEnum):
    RICH = "rich"
    PLAIN = "plain"
    JSON = "json"


class ColorMode(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


_VALID_REQUESTS = frozenset({"auto", "rich", "plain", "json"})


def _is_tty(stream: IO[Any] | None) -> bool:
    if stream is None:
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _is_dumb_terminal(env: Mapping[str, str]) -> bool:
    return env.get("TERM", "").strip().lower() == "dumb"


def _no_color(env: Mapping[str, str]) -> bool:
    # NO_COLOR 仕様: 値の内容によらず、設定されていれば色を出さない。
    return "NO_COLOR" in env


def resolve_output_mode(
    requested: str,
    *,
    stream: IO[Any] | None,
    env: Mapping[str, str] | None = None,
) -> OutputMode:
    """`--output` の要求値と実行環境から実際の出力モードを決める。"""
    env = os.environ if env is None else env
    value = requested.strip().lower()
    if value not in _VALID_REQUESTS:
        raise ValueError(f"未知の出力モード: {requested!r}(有効値: auto, rich, plain, json)")

    if value == "json":
        return OutputMode.JSON
    if value == "plain":
        return OutputMode.PLAIN
    if _is_dumb_terminal(env):
        return OutputMode.PLAIN
    if value == "rich":
        return OutputMode.RICH
    return OutputMode.RICH if _is_tty(stream) else OutputMode.PLAIN


def resolve_color_system(
    mode: OutputMode,
    color: ColorMode,
    *,
    stream: IO[Any] | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Rich Console へ渡す color_system。None は色なしを意味する。"""
    env = os.environ if env is None else env
    if mode is not OutputMode.RICH:
        return None
    if _no_color(env) or color is ColorMode.NEVER:
        return None
    if color is ColorMode.ALWAYS:
        return "auto"
    return "auto" if _is_tty(stream) else None
