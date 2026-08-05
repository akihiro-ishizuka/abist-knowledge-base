"""結果提示の唯一の経路(設計書 §6.2, §6.3)。

CLI はすべてこの `Presenter` を通して人間向けメッセージと
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
from abist_kb.domain.redaction import mask_secrets
from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.theme import TOKEN_STYLES, SemanticToken, build_theme

_DEBUG_HINT = "--debug を付けると詳細を表示します"
_NON_INTERACTIVE_HINT = "非対話実行では --yes を指定してください"


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


class _CrashSafeTextStream:
    """`reconfigure()` 自体が拒否された実ストリームに対する最終防衛線。

    既に読み取り済みの `TextIOWrapper` などは `reconfigure(encoding=...)` を
    呼んだ時点で `io.UnsupportedOperation`(`OSError` と `ValueError` の
    両方のサブクラス)を送出して再設定そのものを拒否することがある。その場合
    ストリームは元のコードページ(例: cp932/strict)のまま残り、`write()` に
    エンコードできない文字(SUCCESS トークンの `✓` など)を渡すと
    `UnicodeEncodeError` でクラッシュしてしまう。

    このクラスは `write()` だけをフックし、失敗したらその場で
    `backslashreplace` により書き込み可能な文字列へ作り直して再送する。
    `backslashreplace` の出力は常に ASCII になるため、strict なストリームへの
    再送は必ず成功する。`flush`/`isatty`/`fileno`/`encoding` など、それ以外の
    属性はすべて元のストリームへ委譲する(Rich の Console が触れるものを含む)。
    """

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        try:
            return self._stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self._stream, "encoding", None) or "ascii"
            safe_text = text.encode(encoding, "backslashreplace").decode(encoding)
            return self._stream.write(safe_text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _reconfigure_utf8(stream: IO[str]) -> IO[str]:
    """実ストリームを UTF-8/backslashreplace へ寄せ、cp932 等での書き込み失敗を防ぐ。

    日本語ロケール Windows の既定コードページ(cp932)は SUCCESS トークンの記号
    '✓'(U+2713)を表現できず、そのまま書き込むと UnicodeEncodeError で
    クラッシュする。`reconfigure` を持たないストリーム(`io.StringIO` など)は
    そのまま返す(エンコード層が無く、そもそもクラッシュしない)。

    **重要 — 副作用**: `reconfigure` に成功した場合、呼び出し元が渡した実ストリーム
    (典型的には `sys.stdout`/`sys.stderr`)はその場で UTF-8/backslashreplace へ
    *恒久的に* 書き換えられる。この変更は Presenter インスタンスのライフサイクルに
    縛られず、Presenter が破棄された後もプロセスが終了するまで残る。これは意図した
    挙動である — Presenter はこのプロジェクトの全 CLI/MCP エントリポイントに
    とって唯一の人間向け出力経路であり、プロセス全体で UTF-8 stdout/stderr を既定にすることが
    狙いだからである。レガシーな cp932 前提のツールへパイプする呼び出し元は、この
    副作用を踏まえてストリームを扱うこと。

    `reconfigure` が存在しても呼び出し自体が拒否される場合(既に読み取り済みの
    `TextIOWrapper` など、`AttributeError`/`OSError`/`ValueError` を送出する場合)は、
    元のストリームを書き換えずに `_CrashSafeTextStream` でラップして返す
    ("最良の結果(UTF-8 化)が得られないなら、せめてクラッシュしない"という
    フォールバック)。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return stream
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError, ValueError):
        return _CrashSafeTextStream(stream)
    return stream


def _redact_details(details: dict[str, Any]) -> dict[str, Any]:
    """`AppError.details` の文字列値だけを `mask_secrets` へ通した辞書を返す。

    `wrap()` が保持する `cause_message`(捕捉した外部例外の生メッセージ)経由で
    URL の資格情報やトークンが紛れ込みうるため、`Presenter.error()` が details を
    表示する箇所(PLAIN/RICH の人間向け表示・JSON の機械可読出力の両方)で必ず通す。
    数値・真偽値など文字列以外の値は型を保持したまま素通しする(JSON 出力の
    型フィデリティを壊さないため)。
    """
    return {
        key: mask_secrets(value) if isinstance(value, str) else value
        for key, value in details.items()
    }


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
        """`stdout`/`stderr` に実ストリームを渡した場合の副作用に注意。

        `stdout`/`stderr` に `None` 以外の実ストリーム(例: `sys.stdout`)を渡すと、
        そのストリームが `reconfigure()` に対応していれば UTF-8/backslashreplace へ
        **その場で恒久的に書き換える**(`_reconfigure_utf8` 参照)。この変更は
        Presenter インスタンスより長生きし、Presenter が破棄された後もプロセスが
        終了するまで残る。これは意図した挙動である — Presenter はこのプロジェクトの
        全 CLI/MCP エントリポイントにとって唯一の人間向け出力経路であり、プロセス全体で
        UTF-8 の stdout/stderr を既定にすることが狙いだからである。レガシーな
        cp932 前提のツールへ後段でパイプする場合はこの副作用を踏まえること。
        `None`(既定)を渡した場合は内部の `io.StringIO` を使うため副作用は無い。
        """
        self._mode = OutputMode(mode)
        self._quiet = quiet
        self._verbose = verbose
        self._debug = debug

        self._owns_stdout = stdout is None
        self._owns_stderr = stderr is None
        self._stdout_stream: IO[str] = io.StringIO() if stdout is None else stdout
        self._stderr_stream: IO[str] = io.StringIO() if stderr is None else stderr
        self._json_emitted = False

        # 実ストリーム(TextIOWrapper 等)が cp932 などの非 UTF-8 コードページに
        # 固定されている場合でも、記号(✓ 等)の書き込みでクラッシュしないようにする。
        # 成功すれば渡されたストリームをその場で UTF-8 へ恒久的に書き換える
        # (プロセス終了までその変更は残る。`_reconfigure_utf8` のドキュメント参照)。
        # `io.StringIO` は `reconfigure` を持たないため何もしない。再設定自体が
        # 拒否された場合はクラッシュ安全な書き込みプロキシへ差し替える。
        self._stdout_stream = _reconfigure_utf8(self._stdout_stream)
        self._stderr_stream = _reconfigure_utf8(self._stderr_stream)

        theme = build_theme()
        common: dict[str, Any] = {
            "theme": theme,
            "markup": False,
            "highlight": False,
            "emoji": False,
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
        # `Panel._title` calls `Text.from_markup(title)` internally *whenever title is
        # a plain `str`*, with its own hardcoded `emoji=True` default — this bypasses
        # the Console's `emoji=False` entirely (Console-level settings only govern
        # `render_str()`, and Panel never routes the title through it). Pre-wrapping
        # the title in `Text(...)` (which does not parse markup/emoji codes) is the
        # only way to keep the title byte-identical; the body is unaffected because it
        # is rendered via `console.render_lines()`, which does honour `emoji=False`.
        self._target().print(Panel(body, title=Text(title), safe_box=True))

    def markdown(self, text: str) -> None:
        if self._quiet:
            return
        self._target().print(Markdown(text))

    # -- 機械可読出力 -----------------------------------------------------

    def json_result(self, payload: Any) -> None:
        """`--output json` の唯一の stdout 出力。1 行の JSON。

        「stdout は payload のみ」という契約上、1プロセスにつき1回しか
        呼び出せない。2回目は壊れた JSON を黙って連結するのではなく、
        `RuntimeError` として直ちに失敗させる。
        """
        if self._json_emitted:
            raise RuntimeError(
                "json_result() は1度しか呼び出せません"
                "(stdout は単一の JSON ドキュメントのみを保持する契約のため)"
            )
        self._json_emitted = True
        encoded = json.dumps(payload, ensure_ascii=False)
        self._console.print(encoded)

    def error(self, err: AppError) -> None:
        """エラー提示。順序は固定: コード→概要→原因→回復手順→--debug案内。

        `details`(`wrap()` が保持する `cause_message` を含みうる)は捕捉した外部
        例外の生メッセージをそのまま運ぶことがあるため、`--debug` の有無や
        `--output json` かどうかに関わらず必ず `mask_secrets` を通す。「ユーザーが
        `--debug` を頼んだのだから自己責任」は理由にならない(§15 はテスト
        スナップショットからの秘密情報不在を、§13.2 は Rich レンダラのスナップショット
        テストを要求しており、CI はこの stderr を Actions ログへそのまま残す)。
        """
        if self._mode is OutputMode.JSON:
            payload = err.to_dict()
            payload["details"] = _redact_details(payload["details"])
            self._err_console.print(json.dumps(payload, ensure_ascii=False))
            return

        danger_symbol = TOKEN_STYLES[SemanticToken.DANGER].symbol
        code_line = Text()
        code_line.append(f"{danger_symbol} ", style=SemanticToken.DANGER.value)
        code_line.append(str(err.code), style=SemanticToken.DANGER.value)
        self._err_console.print(code_line)

        self._err_console.print(Text(err.message))

        details = _redact_details(err.details if self._debug else err.to_dict()["details"])
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
            # Rich の Traceback をそのまま stderr の Console へ print すると、
            # 例外メッセージ(URL の資格情報やトークンを含みうる)がマスクされずに
            # そのまま出る。同じ幅の使い捨て Console でいったん文字列へレンダリング
            # してから mask_secrets を通し、Text として再出力する。
            # `no_color=True`/`color_system=None` で ANSI を含まない素のテキストに
            # してから mask_secrets へ渡す(ANSI混じりの文字列だと秘密情報の一致箇所が
            # エスケープシーケンスで分断され、パターンにマッチしなくなるおそれがある)。
            capture_buffer = io.StringIO()
            capture_console = Console(
                file=capture_buffer,
                width=self._err_console.width,
                color_system=None,
                no_color=True,
                force_terminal=False,
                highlight=False,
                markup=False,
                emoji=False,
                safe_box=True,
            )
            capture_console.print(traceback)
            self._err_console.print(Text(mask_secrets(capture_buffer.getvalue())))

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
