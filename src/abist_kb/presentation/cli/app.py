"""Typer CLI アプリのエントリポイント(設計書 §7)。

コールバックが共通オプション(`--root`/`--output`/`--color`/`--quiet`/
`--verbose`/`--debug`/`--yes`)を解決し、`Presenter` と `Settings` を構築して
`CliContext` として `ctx.obj` へ格納する。以降の全サブコマンドはこの
`ctx.obj` 経由でのみそれらへアクセスする(`context.get_context` 参照)。
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
from importlib import metadata
from pathlib import Path
from typing import Annotated

import typer

from abist_kb import identity
from abist_kb.config import load_settings
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.observability.logging import configure_logging
from abist_kb.presentation.cli.config_cmd import config_app
from abist_kb.presentation.cli.context import CliContext, fail
from abist_kb.presentation.cli.doctor_cmd import doctor
from abist_kb.presentation.console.output import (
    ColorMode,
    OutputMode,
    resolve_color_system,
    resolve_output_mode,
)
from abist_kb.presentation.console.presenter import Presenter

app = typer.Typer(
    name=identity.CLI_NAME,
    help=f"{identity.DISPLAY_NAME} — 複数のナレッジソースを横断する CLI。",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(config_app, name="config")
app.command("doctor")(doctor)


def _fallback_presenter() -> Presenter:
    """Presenter 構築前(オプション検証など)にも使える最小構成の Presenter。"""
    return Presenter(OutputMode.PLAIN, stdout=sys.stdout, stderr=sys.stderr)


def _parse_output_mode(value: str) -> OutputMode:
    try:
        return resolve_output_mode(value, stream=sys.stdout)
    except ValueError as exc:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=str(exc),
            exit_code=ExitCode.INVALID_INPUT,
        ) from exc


def _parse_color_mode(value: str) -> ColorMode:
    try:
        return ColorMode(value.strip().lower())
    except ValueError as exc:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"未知の色指定です: {value!r}(有効値: auto, always, never)",
            exit_code=ExitCode.INVALID_INPUT,
        ) from exc


def _resolve_log_level(*, debug: bool, verbose: bool, quiet: bool) -> str:
    if debug or verbose:
        return "DEBUG"
    if quiet:
        return "WARNING"
    return "INFO"


def _version_callback(value: bool) -> None:
    if not value:
        return
    version = metadata.version(identity.DISTRIBUTION_NAME)
    _fallback_presenter().line(version)
    raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    root: Annotated[
        Path | None,
        typer.Option("--root", help="プロジェクトルート(既定: カレントディレクトリ)。"),
    ] = None,
    output: Annotated[
        str, typer.Option("--output", help="出力形式: auto, rich, plain, json。")
    ] = "auto",
    color: Annotated[str, typer.Option("--color", help="色使用: auto, always, never。")] = "auto",
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="通常メッセージを抑制する。")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="ログレベルを DEBUG に引き上げる。")
    ] = False,
    debug: Annotated[
        bool, typer.Option("--debug", help="デバッグ出力・詳細トレースバックを有効化する。")
    ] = False,
    assume_yes: Annotated[
        bool, typer.Option("--yes", "-y", help="確認プロンプトを自動で承認する。")
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help=f"{identity.DISPLAY_NAME} のバージョンを表示して終了する。",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """共通オプションを解決し、`CliContext` を構築してサブコマンドへ渡す。"""
    presenter: Presenter | None = None
    try:
        output_mode = _parse_output_mode(output)
        color_mode = _parse_color_mode(color)
        color_system = resolve_color_system(output_mode, color_mode, stream=sys.stdout)
        presenter = Presenter(
            output_mode,
            stdout=sys.stdout,
            stderr=sys.stderr,
            color_system=color_system,
            quiet=quiet,
            verbose=verbose,
            debug=debug,
        )
        settings = load_settings(root=root)
        configure_logging(
            level=_resolve_log_level(debug=debug, verbose=verbose, quiet=quiet),
            stream=sys.stderr,
        )
    except AppError as err:
        fail(presenter, err)

    ctx.obj = CliContext(
        settings=settings,
        presenter=presenter,
        debug=debug,
        assume_yes=assume_yes,
    )


def _enable_windows_utf8_console() -> None:
    """Windows のコンソール出力コードページを UTF-8 へ切り替える(表示専用)。

    日本語ロケール Windows の既定コードページ(cp932)ではセマンティックトークンの
    記号(✓ 等)が正しく表示できない。Presenter 自体はこの状況でもクラッシュしない
    フォールバックを備えている(`presentation.console.presenter`)が、記号を正しく
    *表示* するのはこのエントリポイントの責務である。非 Windows では no-op。
    コンソールを持たない実行形態(サービス化・パイプ経由など)でも CLI の起動を
    妨げてはならないため、あらゆる例外を握り潰す。
    """
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)


def main() -> None:
    """コンソールスクリプトのエントリポイント(`pyproject.toml` の `project.scripts`)。"""
    _enable_windows_utf8_console()
    try:
        app()
    except AppError as err:
        _fallback_presenter().error(err)
        sys.exit(int(err.exit_code))
    except KeyboardInterrupt:
        _fallback_presenter().error(
            AppError(
                code=ErrorCode.CANCELLED,
                message="ユーザーにより中断されました。",
                exit_code=ExitCode.CANCELLED,
            )
        )
        sys.exit(int(ExitCode.CANCELLED))


__all__ = ["app", "main"]
