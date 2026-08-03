"""結果提示の唯一の経路(設計書 §6.2, §6.3)。

CLI/TUI/Web はすべてこの `Presenter` を通して人間向けメッセージと
機械可読な JSON 結果を出力する。`plain`/`json` モードでは ANSI とアニメーションを
一切出さない(purity テストが `\\x1b[` の不在を保証する)。
"""

from __future__ import annotations

import io
import json
import sys
from typing import IO, Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text
from rich.traceback import Traceback

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.theme import TOKEN_STYLES, SemanticToken, build_theme

_DEBUG_HINT = "--debug を付けると詳細を表示します"
_NON_INTERACTIVE_HINT = "非対話実行では --yes を指定してください"


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


class Presenter:
    """出力モードに応じて人間向け表示と JSON 結果を振り分ける。"""

    def __init__(
        self,
        mode: OutputMode | str,
        *,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
        color_system: str | None = "auto",
        force_terminal: bool | None = None,
        width: int | None = None,
        quiet: bool = False,
        verbose: bool = False,
        debug: bool = False,
    ) -> None:
        self._mode = OutputMode(mode)
        self._quiet = quiet
        self._verbose = verbose
        self._debug = debug

        self._owns_stdout = stdout is None
        self._owns_stderr = stderr is None
        self._stdout_stream: IO[str] = io.StringIO() if stdout is None else stdout
        self._stderr_stream: IO[str] = io.StringIO() if stderr is None else stderr

        theme = build_theme()
        common: dict[str, Any] = {
            "theme": theme,
            "markup": False,
            "highlight": False,
            "soft_wrap": True,
            "safe_box": True,
            "width": width,
        }

        if self._mode is OutputMode.RICH:
            self._console = Console(
                file=self._stdout_stream,
                color_system=color_system,
                force_terminal=force_terminal,
                **common,
            )
            self._err_console = Console(
                file=self._stderr_stream,
                color_system=color_system,
                force_terminal=force_terminal,
                **common,
            )
        else:
            # PLAIN/JSON: ANSI もアニメーションも一切出さない。
            self._console = Console(
                file=self._stdout_stream,
                color_system=None,
                no_color=True,
                force_terminal=False,
                **common,
            )
            self._err_console = Console(
                file=self._stderr_stream,
                color_system=None,
                no_color=True,
                force_terminal=False,
                **common,
            )

    # -- プロパティ ---------------------------------------------------

    @property
    def mode(self) -> OutputMode:
        return self._mode

    @property
    def is_json(self) -> bool:
        return self._mode is OutputMode.JSON

    @property
    def quiet(self) -> bool:
        return self._quiet

    @property
    def debug(self) -> bool:
        return self._debug

    @property
    def console(self) -> Console:
        return self._console

    @property
    def err_console(self) -> Console:
        return self._err_console

    # -- テスト用ヘルパ -----------------------------------------------

    def stdout_value(self) -> str:
        """内部バッファ(StringIO)へ蓄積された stdout の内容。"""
        if not self._owns_stdout:
            raise RuntimeError("stdout が外部から指定されているため stdout_value() は使えません")
        return self._stdout_stream.getvalue()  # type: ignore[union-attr]

    def stderr_value(self) -> str:
        """内部バッファ(StringIO)へ蓄積された stderr の内容。"""
        if not self._owns_stderr:
            raise RuntimeError("stderr が外部から指定されているため stderr_value() は使えません")
        return self._stderr_stream.getvalue()  # type: ignore[union-attr]

    # -- 人間向け出力 ---------------------------------------------------

    def _target(self) -> Console:
        """JSON モードでは人間向けメッセージをすべて stderr へ逃がす。"""
        return self._err_console if self._mode is OutputMode.JSON else self._console

    def _token_text(self, text: str, token: SemanticToken | None) -> Text:
        if token is None:
            return Text(text)
        rendered = Text()
        rendered.append(TOKEN_STYLES[token].symbol, style=token.value)
        rendered.append(" ")
        rendered.append(text)
        return rendered

    def _emit(self, text: str, *, token: SemanticToken | None, suppressible: bool) -> None:
        if suppressible and self._quiet:
            return
        self._target().print(self._token_text(text, token))

    def line(self, text: str, *, token: SemanticToken | None = None) -> None:
        self._emit(text, token=token, suppressible=True)

    def success(self, text: str) -> None:
        self._emit(text, token=SemanticToken.SUCCESS, suppressible=True)

    def warning(self, text: str) -> None:
        self._emit(text, token=SemanticToken.WARNING, suppressible=False)

    def danger(self, text: str) -> None:
        self._emit(text, token=SemanticToken.DANGER, suppressible=False)

    def info(self, text: str) -> None:
        self._emit(text, token=SemanticToken.INFO, suppressible=True)

    def muted(self, text: str) -> None:
        self._emit(text, token=SemanticToken.MUTED, suppressible=True)

    def table(self, title: str, columns: list[str], rows: list[list[Any]]) -> None:
        if self._quiet:
            return
        rendered = Table(title=title, safe_box=True)
        for column in columns:
            rendered.add_column(str(column))
        for row in rows:
            rendered.add_row(*(str(cell) for cell in row))
        self._target().print(rendered)

    def panel(self, title: str, body: str) -> None:
        if self._quiet:
            return
        self._target().print(Panel(body, title=title, safe_box=True))

    def markdown(self, text: str) -> None:
        if self._quiet:
            return
        self._target().print(Markdown(text))

    # -- 機械可読出力 -----------------------------------------------------

    def json_result(self, payload: Any) -> None:
        """`--output json` の唯一の stdout 出力。1 行の JSON。"""
        encoded = json.dumps(payload, ensure_ascii=False)
        self._console.print(encoded)

    def error(self, err: AppError) -> None:
        """エラー提示。順序は固定: コード→概要→原因→回復手順→--debug案内。"""
        if self._mode is OutputMode.JSON:
            self._err_console.print(json.dumps(err.to_dict(), ensure_ascii=False))
            return

        danger_symbol = TOKEN_STYLES[SemanticToken.DANGER].symbol
        code_line = Text()
        code_line.append(f"{danger_symbol} ", style=SemanticToken.DANGER.value)
        code_line.append(str(err.code), style=SemanticToken.DANGER.value)
        self._err_console.print(code_line)

        self._err_console.print(Text(err.message))

        details = err.details if self._debug else err.to_dict()["details"]
        if details:
            self._err_console.print(Text("原因:", style=SemanticToken.MUTED.value))
            for key, value in details.items():
                self._err_console.print(Text(f"  {key}: {value}"))

        if err.hint:
            info_symbol = TOKEN_STYLES[SemanticToken.INFO].symbol
            hint_line = Text()
            hint_line.append(f"{info_symbol} ", style=SemanticToken.INFO.value)
            hint_line.append(err.hint)
            self._err_console.print(hint_line)

        self._err_console.print(Text(_DEBUG_HINT, style=SemanticToken.MUTED.value))

        if self._debug and err.__cause__ is not None:
            traceback = Traceback.from_exception(
                type(err.__cause__),
                err.__cause__,
                err.__cause__.__traceback__,
                show_locals=False,
            )
            self._err_console.print(traceback)

    # -- 対話 -----------------------------------------------------------

    def confirm(self, prompt: str, *, assume_yes: bool = False) -> bool:
        """破壊的操作の確認。非対話実行では `--yes` を必須にする。"""
        if assume_yes:
            return True
        if self._mode is OutputMode.JSON or not _stdin_is_tty():
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message="確認が必要な操作のため処理を中断しました",
                hint=_NON_INTERACTIVE_HINT,
            )
        return Confirm.ask(prompt, console=self._console)
