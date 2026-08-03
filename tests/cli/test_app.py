import io
import json
import sys

import pytest
from typer.testing import CliRunner

from abist_kb import identity
from abist_kb.presentation.cli.app import app, main

runner = CliRunner()


def test_help_shows_the_cli_name_and_display_name():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert identity.DISPLAY_NAME in result.output


def test_global_options_are_declared():
    result = runner.invoke(app, ["--help"])
    for flag in ("--output", "--color", "--quiet", "--verbose", "--debug", "--yes"):
        assert flag in result.output


def test_invalid_output_mode_exits_with_input_error():
    result = runner.invoke(app, ["--output", "fancy", "doctor"])
    assert result.exit_code == 2


def test_unknown_command_exits_with_input_error():
    result = runner.invoke(app, ["nosuchcommand"])
    assert result.exit_code == 2


def test_version_flag_prints_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_json_output_from_a_command_is_parseable(tmp_root):
    result = runner.invoke(app, ["--root", str(tmp_root), "--output", "json", "config", "path"])
    assert result.exit_code == 0
    json.loads(result.stdout)


def test_help_does_not_crash_when_real_stdout_is_cp932(monkeypatch):
    """CRITICAL 回帰: `Typer(help=...)` に含む em dash(—, U+2014)は cp932 で
    表現できない。`--help` は `main_callback` 実行前(引数解析中)に Typer 自身の
    内部 Rich Console で描画されるため、`Presenter._reconfigure_utf8`(Task 2)の
    恩恵を受けない。`CliRunner` は既定で stdout/stderr を UTF-8 に固定してしまい
    この不具合を再現できないため、`sys.stdout`/`sys.stderr` を直接 cp932 の
    `TextIOWrapper` に差し替えたうえで実際のエントリポイント `main()` を呼ぶ。
    """
    stdout_stream = io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="strict")
    stderr_stream = io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout_stream)
    monkeypatch.setattr(sys, "stderr", stderr_stream)
    monkeypatch.setattr(sys, "argv", ["abist-kb", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_bare_invocation_does_not_crash_when_real_stdout_is_cp932(monkeypatch):
    """上と同じ不具合が `no_args_is_help=True` 経由の裸呼び出しでも起きないことを確認する。

    裸呼び出しは Click の `NoArgsIsHelpError`(`UsageError` 系、終了コード2)として
    処理される既存仕様であり、本修正が変えるのは「クラッシュしないこと」であって
    終了コードそのものではない。
    """
    stdout_stream = io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="strict")
    stderr_stream = io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout_stream)
    monkeypatch.setattr(sys, "stderr", stderr_stream)
    monkeypatch.setattr(sys, "argv", ["abist-kb"])

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2


def test_unexpected_crash_does_not_dump_a_traceback_without_debug(monkeypatch):
    """`pretty_exceptions_enable` は debug フラグの既定値(オフ)に合わせて無効化する。

    Typer の既定(`pretty_exceptions_enable=True`)のままだと、未捕捉の例外(今回の
    cp932 クラッシュのような`AppError` でないバグ)が `--debug` 無しでも Rich の
    詳細トレースバックを出してしまい、「生のトレースバックは --debug のときだけ」
    という制約に反する。ここでは意図的に壊れた `--output` 検証をすり抜けさせず、
    Typer アプリの `pretty_exceptions_enable` 属性そのものが False であることを
    直接検証する(実際のクラッシュ経路を再現するのではなく、既定設定の回帰を防ぐ)。
    """
    assert app.pretty_exceptions_enable is False


def test_command_raising_app_error_without_local_handling_still_maps_exit_code():
    """IMPORTANT 回帰: コマンド本体が `AppError` を自分で捕捉しなくても、
    `CliRunner` 経由で正しい終了コードになることを保証する。

    Typer 標準の `TyperCommand.invoke()` は Click の例外階層
    (`ClickException`/`Exit`/`Abort`)に属さない一般的な `Exception` を一切
    捕捉しない。`AppError` はまさにそれに該当するため、コマンド本体が自前で
    `try/except AppError: ... raise typer.Exit(...)` を書き忘れると、実プロセスでは
    `app.py::main()` の残存 except が拾って正しい終了コードになる一方、
    `CliRunner` 経由(`cli.main()` を直接呼ぶため `main()` を経由しない)では
    `exit_code=1` に潰れてしまう。M3 で追加される新しいコマンド群がこの罠に
    落ちないよう、`AppTyper`(既定で `AppErrorHandlingCommand` を使う `Typer`
    サブクラス)を使えば opt-in 無しで正しく変換されることを、ローカルの
    try/except を一切書かない使い捨てコマンドで確認する。
    """
    from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
    from abist_kb.presentation.cli.context import AppTyper

    throwaway = AppTyper()

    @throwaway.callback()
    def _noop_callback() -> None:
        return None

    @throwaway.command("boom")
    def _boom() -> None:
        raise AppError(
            code=ErrorCode.CONFIG_ERROR,
            message="意図的な失敗(回帰テスト用、ローカルのtry/exceptは無い)",
            exit_code=ExitCode.CONFIG_ERROR,
        )

    result = runner.invoke(throwaway, ["boom"])
    assert result.exit_code == 3
