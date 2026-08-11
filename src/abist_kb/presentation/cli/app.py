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
from typing import IO, Annotated

import typer

from abist_kb import identity
from abist_kb.config import load_settings
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode, wrap
from abist_kb.infrastructure.observability.logging import configure_logging
from abist_kb.presentation.cli.api_cmd import api_app
from abist_kb.presentation.cli.audit_cmd import audit_app
from abist_kb.presentation.cli.batch_cmd import batch_app
from abist_kb.presentation.cli.config_cmd import config_app
from abist_kb.presentation.cli.context import (
    UNEXPECTED_ERROR_HINT,
    UNEXPECTED_ERROR_MESSAGE,
    AppTyper,
    CliContext,
    fail,
)
from abist_kb.presentation.cli.curate_cmd import curate_app
from abist_kb.presentation.cli.doctor_cmd import doctor
from abist_kb.presentation.cli.document_cmd import document_app
from abist_kb.presentation.cli.index_cmd import index_app
from abist_kb.presentation.cli.init_cmd import init
from abist_kb.presentation.cli.jobs_cmd import jobs_app
from abist_kb.presentation.cli.mcp_cmd import mcp_app
from abist_kb.presentation.cli.migrate_cmd import migrate_app
from abist_kb.presentation.cli.search_cmd import search
from abist_kb.presentation.cli.source_cmd import source_app
from abist_kb.presentation.cli.sync_cmd import sync_app
from abist_kb.presentation.cli.video_cmd import video_app
from abist_kb.presentation.cli.worker_cmd import worker_app
from abist_kb.presentation.console.output import (
    ColorMode,
    OutputMode,
    resolve_color_system,
    resolve_output_mode,
)
from abist_kb.presentation.console.presenter import Presenter

app = AppTyper(
    name=identity.CLI_NAME,
    help=f"{identity.DISPLAY_NAME} — 複数のナレッジソースを横断する CLI。",
    no_args_is_help=True,
    add_completion=False,
    # --debug の既定値(オフ)に合わせて無効化する。既定の True のままだと、
    # AppError で正規化されていない未捕捉の例外(バグ)が --debug 無しでも
    # Rich の詳細トレースバックを出してしまい、「生のトレースバックは --debug の
    # ときだけ」という制約に反する(cp932 クラッシュがまさにこの経路で起きていた)。
    pretty_exceptions_enable=False,
)

app.add_typer(config_app, name="config")
app.add_typer(jobs_app, name="jobs")
app.add_typer(worker_app, name="worker")
app.add_typer(source_app, name="source")
app.add_typer(batch_app, name="batch")
app.add_typer(sync_app, name="sync")
app.add_typer(document_app, name="document")
app.add_typer(index_app, name="index")
app.add_typer(audit_app, name="audit")
app.add_typer(curate_app, name="curate")
app.add_typer(mcp_app, name="mcp")
app.add_typer(migrate_app, name="migrate")
app.add_typer(api_app, name="api")
app.add_typer(video_app, name="video")
app.command("init")(init)
app.command("search")(search)
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


def _resolve_log_level(setting_level: str, *, debug: bool, verbose: bool, quiet: bool) -> str:
    """実効ログレベルを決める。優先順位は CLI フラグ > `settings.toml`/環境変数。

    I5(IMPORTANT)の回帰修正: 以前はここが `settings.log_level` を一切読んでおらず、
    `Settings` に宣言・検証され `config show` にも表示されるフィールドが実際の
    ロギング挙動には何の影響も与えない死んだ設定になっていた
    (`settings.toml` に `log_level = "DEBUG"` と書いてもロガーは INFO のまま)。
    フラグは常に設定より優先する(`--debug`/`--verbose` は強制的に DEBUG へ、
    `--quiet` は強制的に WARNING へ引き上げる)。
    """
    if debug or verbose:
        return "DEBUG"
    if quiet:
        return "WARNING"
    return setting_level


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        version = metadata.version(identity.DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        # 未インストールのソースツリーから直接実行した場合(`uv sync` 前の
        # チェックアウトや、配布物として構築されていない実行環境)、
        # distribution メタデータが存在せず `PackageNotFoundError` が生の
        # トレースバックとして `--version` を落としていた。バージョン確認は
        # 補助的な操作であり、これだけでクラッシュさせる必要はない。
        version = "unknown(パッケージ未インストール)"
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
            level=_resolve_log_level(settings.log_level, debug=debug, verbose=verbose, quiet=quiet),
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


def _reconfigure_stream_utf8(stream: IO[str]) -> None:
    """`sys.stdout`/`sys.stderr` の実体を UTF-8/backslashreplace へ再設定する。

    `TextIOWrapper` のエンコーディングはインタプリタ起動時にシステムロケールから
    固定される(日本語ロケール Windows では既定で cp932)。`_enable_windows_utf8_console`
    が行う `SetConsoleOutputCP(65001)` は Win32 コンソール側の *表示* コードページを
    変えるだけで、この Python 側ストリームのエンコーディングには一切影響しない。

    これが問題になるのは `--help`(および `no_args_is_help=True` による裸呼び出し)
    のように、`main_callback` が実行される *前*(引数解析の途中)に Typer 自身が
    内部で新しい `rich.console.Console` を作って描画する経路である。この経路は
    `Presenter` を経由しないため、Task 2 で `Presenter._reconfigure_utf8` に実装した
    cp932 対策の恩恵を受けない。`Typer(help=...)` に含む全角ダッシュ等の非ASCII文字が
    cp932 で表現できず `UnicodeEncodeError` を送出し、`AppError` でも `ClickException`
    でもないため `--debug` 抜きで生のトレースバックが出る、という実測済みの不具合が
    起きる。ここで `main()` の最初(`app()` 呼び出しより前)に `sys.stdout`/
    `sys.stderr` そのものを直接 UTF-8 化することで、Typer 自身が内部で作る Console も
    含めて解消する(Rich の `Console.file` は既定で `sys.stdout`/`sys.stderr` を
    その都度動的に参照するため、この時点での再設定で以降のすべての描画に効く)。

    `reconfigure` を持たないストリーム、または(既に読み取り済み等の理由で)
    `reconfigure` 自体を拒否するストリームは、静かにスキップする
    (この関数自体が例外を送出することは無い)。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    with contextlib.suppress(AttributeError, OSError, ValueError):
        reconfigure(encoding="utf-8", errors="backslashreplace")


def _enable_windows_utf8_console() -> None:
    """Windows のコンソール出力コードページを UTF-8 へ切り替える(表示専用)。

    日本語ロケール Windows の既定コードページ(cp932)ではセマンティックトークンの
    記号(✓ 等)が正しく表示できない。Presenter 自体はこの状況でもクラッシュしない
    フォールバックを備えている(`presentation.console.presenter`)が、記号を正しく
    *表示* するのはこのエントリポイントの責務である。非 Windows では no-op。
    コンソールを持たない実行形態(サービス化・パイプ経由など)でも CLI の起動を
    妨げてはならないため、あらゆる例外を握り潰す。

    **重要 — 副作用**: `SetConsoleOutputCP`/`SetConsoleCP` はプロセス単位ではなく
    *コンソール* 単位の設定であり、このCLIプロセスが終了した後もコンソール
    (呼び出し元の PowerShell/cmd セッションなど)に恒久的に残る。手動で
    `chcp 65001` を実行したのと同じ効果であり、同じコンソールを使い続ける他の
    cp932 前提のレガシーツールへ影響しうる。Task 2 の
    `Presenter._reconfigure_utf8` がプロセス内 Python ストリームの書き換えという
    副作用を明示しているのと同様にここでも明示しておく。こちらは影響範囲が
    プロセスの寿命を超えて親シェルのコンソールという OS レベルの状態にまで及ぶため、
    より広い。
    """
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)


def main() -> None:
    """コンソールスクリプトのエントリポイント(`pyproject.toml` の `project.scripts`)。"""
    _reconfigure_stream_utf8(sys.stdout)
    _reconfigure_stream_utf8(sys.stderr)
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
    except Exception as exc:
        # デフォード#13 の回帰修正: `AppErrorHandlingCommand`/`AppErrorHandlingGroup`
        # のどちらも経由しない箇所から生の例外がここまで届いた場合の最終防衛線。
        # 以前はここに `except Exception` が無く、`AppError` に正規化されていない
        # バグ(例: `KeyError`)が Python インタプリタの既定のハンドラまで
        # そのまま突き抜けて、`--debug` の有無に関わらず生のトレースバックが
        # コード無し・回復手順無しで出力されていた(§8 違反)。
        err = wrap(
            exc,
            code=ErrorCode.FAILURE,
            message=UNEXPECTED_ERROR_MESSAGE,
            hint=UNEXPECTED_ERROR_HINT,
        )
        _fallback_presenter().error(err)
        sys.exit(int(err.exit_code))


__all__ = ["app", "main"]
