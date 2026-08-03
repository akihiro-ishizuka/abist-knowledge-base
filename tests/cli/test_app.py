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


def test_version_flag_does_not_crash_when_package_metadata_is_missing(monkeypatch):
    """小項目の回帰テスト: `_version_callback` は `metadata.version()` を無防備に
    呼んでいたため、未インストールのソースツリーから実行した場合に生じる
    `PackageNotFoundError` が生のトレースバックとして `--version` を落として
    いた。バージョン確認は補助的な操作であり、これだけでクラッシュさせる
    必要はない。
    """
    from importlib import metadata

    from abist_kb.presentation.cli import app as app_module

    def _raise(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(app_module.metadata, "version", _raise)
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() != ""


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


def test_pretty_exceptions_enable_is_disabled_by_default():
    """Typer の既定(`pretty_exceptions_enable=True`)のままだと、Rich の
    `pretty_exceptions` 機構が未捕捉の例外に色付きの詳細トレースバックを付けて
    しまう。この属性の無効化自体は「生のトレースバックが出ない」ことを意味しない
    (色が消えるだけでトレースバックそのものは出続ける)。本テストはこの属性の
    既定値の回帰だけを守る。「未捕捉の例外が `--debug` 無しではトレースバックを
    出さず、`AppError(FAILURE)` として提示される」という実際の挙動は
    `test_unhandled_exception_*` 系のテストが検証する(以前はこの関数名がその
    挙動を保証しているかのような名前でありながら、実際には確認していなかった)。

    (このテストは以前 `monkeypatch` フィクスチャを受け取っていたが、
    どこにも使っていなかった。使わないフィクスチャは削除した。)
    """
    assert app.pretty_exceptions_enable is False


def test_unhandled_exception_in_command_is_wrapped_without_traceback_by_default():
    """デフォード#13 の回帰テスト: `AppError` に正規化されていない未捕捉の例外
    (例: プログラムのバグによる `KeyError`)が、`--debug` 無しでは生の Python
    トレースバックではなく、エラーコード・概要・回復手順を伴う
    `AppError(FAILURE)` として提示されること。以前は `AppErrorHandlingCommand`
    が `AppError` しか捕捉しておらず、それ以外の例外はコード無し・回復手順無しの
    生トレースバックとしてそのまま出ていた。
    """
    from abist_kb.presentation.cli.context import AppTyper

    throwaway = AppTyper()

    @throwaway.callback()
    def _noop_callback() -> None:
        return None

    @throwaway.command("boom")
    def _boom() -> None:
        raise KeyError("missing_field")

    result = runner.invoke(throwaway, ["boom"])
    assert result.exit_code == 1
    assert "FAILURE" in result.output
    assert "--debug" in result.output
    # `cause_type`(例外のクラス名のみ、秘密情報を含まない)は診断のため
    # `--debug` 無しでも details に出る(既存の `_DEBUG_ONLY_DETAIL_KEYS` の
    # 設計どおり)。ここで守るのは「トレースバック本体は出ない」ことだけ。
    assert "Traceback (most recent call last)" not in result.output


def test_unhandled_exception_shows_traceback_only_with_debug(tmp_root):
    """デフォード#13 の派生ケース: `--debug` 相当(`CliContext.presenter` を
    `debug=True` で構築した状態)では、未捕捉例外のトレースバックが
    (マスク済みで)提示されること。`--debug` フラグを解釈するのは
    `main_callback` であり素の `AppTyper` には無いため、`CliContext` を
    自前で組み立てて `debug=True` を模擬する。
    """
    from pathlib import Path

    import typer as typer_module

    from abist_kb.config import Settings
    from abist_kb.presentation.cli.context import AppTyper, CliContext
    from abist_kb.presentation.console.output import OutputMode
    from abist_kb.presentation.console.presenter import Presenter

    presenters: list[Presenter] = []
    throwaway = AppTyper()

    @throwaway.callback()
    def _cb(ctx: typer_module.Context) -> None:
        presenter = Presenter(OutputMode.PLAIN, debug=True)
        presenters.append(presenter)
        ctx.obj = CliContext(
            settings=Settings(root_dir=Path(tmp_root)),
            presenter=presenter,
            debug=True,
            assume_yes=False,
        )

    @throwaway.command("boom")
    def _boom() -> None:
        raise KeyError("missing_field")

    result = runner.invoke(throwaway, ["boom"])
    assert result.exit_code == 1
    out = presenters[0].stderr_value()
    assert "FAILURE" in out
    assert "KeyError" in out
    assert "Traceback (most recent call last)" in out


def test_main_backstop_wraps_unhandled_exception_without_traceback(monkeypatch, capsys):
    """デフォード#13 の派生ケース: `AppErrorHandlingCommand`/`AppErrorHandlingGroup`
    のどちらも経由しない箇所(理論上は素の `typer.Typer()` の誤用や、Click 自身の
    内部コードなど)から `main()` まで生の例外が届いた場合の最終防衛線。
    `app()` 自体を例外を投げるスタブへ差し替えて、`main()` 単体でこの経路を検証する。
    """
    import abist_kb.presentation.cli.app as app_module

    def _raise() -> None:
        raise KeyError("missing_field")

    monkeypatch.setattr(app_module, "app", _raise)
    monkeypatch.setattr(sys, "argv", ["abist-kb"])

    with pytest.raises(SystemExit) as exc_info:
        app_module.main()
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    # `cause_type`(例外のクラス名のみ)は診断のため出てよい。守るのは
    # トレースバック本体(コードやスタックフレームなど)が出ないことだけ。
    assert "Traceback (most recent call last)" not in captured.err
    assert "FAILURE" in captured.err
    assert "--debug" in captured.err


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


def test_settings_toml_log_level_actually_configures_the_logger(tmp_root):
    """I5(IMPORTANT)の回帰テスト: `Settings.log_level` は宣言・検証・
    `config show` での表示まで実装されているのに、`src/` のどこからも
    読まれておらず、実際のログレベルには一切影響しなかった(`_resolve_log_level`
    はCLIフラグしか見ていなかった)。`settings.toml` に `log_level = "DEBUG"` と
    書いても、フラグ無しでは `abist_kb` ロガーが INFO(20)のまま留まっていた。
    """
    import logging

    cfg = tmp_root / "config" / "settings.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('log_level = "DEBUG"\n', encoding="utf-8")

    result = runner.invoke(app, ["--root", str(tmp_root), "doctor"])
    assert result.exit_code == 0, result.output
    assert logging.getLogger("abist_kb").level == logging.DEBUG


def test_cli_flags_override_settings_toml_log_level(tmp_root):
    """I5 の派生ケース: CLI フラグは設定ファイルの値より優先されること。
    `--quiet` は `settings.toml` の `log_level = "DEBUG"` があっても WARNING に
    引き上げる(抑制する)側へ勝つ。
    """
    import logging

    cfg = tmp_root / "config" / "settings.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('log_level = "DEBUG"\n', encoding="utf-8")

    result = runner.invoke(app, ["--root", str(tmp_root), "--quiet", "doctor"])
    assert result.exit_code == 0, result.output
    assert logging.getLogger("abist_kb").level == logging.WARNING


def test_group_callback_raising_app_error_still_maps_exit_code():
    """I2(IMPORTANT)の回帰テスト: `AppTyper` は `command()` の既定 `cls` しか
    上書きしていなかったため、グループのコールバック(`@group.callback()`)自体が
    `AppError` を送出する経路は保護されていなかった。グループコールバックは
    グループ共通オプション(`source --id`、`jobs --state` 等)の自然な置き場所であり、
    M3 で必ず使われる。`design/plans/M0-foundation.md` の申し送りは以前
    「`AppTyper()` で作ればコマンド本体からの `AppError` は保護される」とだけ
    書いており、この穴を閉じたことを正しく反映するよう修正済み。
    """
    from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
    from abist_kb.presentation.cli.context import AppTyper

    throwaway = AppTyper()

    @throwaway.callback()
    def _group_callback() -> None:
        raise AppError(
            code=ErrorCode.CONFIG_ERROR,
            message="意図的な失敗(グループコールバック、回帰テスト用)",
            exit_code=ExitCode.CONFIG_ERROR,
        )

    @throwaway.command("sub")
    def _sub() -> None:
        return None

    result = runner.invoke(throwaway, ["sub"])
    assert result.exit_code == 3
