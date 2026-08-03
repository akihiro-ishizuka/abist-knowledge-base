"""CLI 全体で共有する実行コンテキストとエラー処理(設計書 §7)。

Typer のコールバック(`app.py` の `main_callback`)が唯一の構築点であり、
すべてのサブコマンドはここで定義する `get_context()` 経由でのみ
`Settings`/`Presenter` へアクセスする。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

import typer
from typer import Abort, Exit
from typer._click.exceptions import ClickException
from typer.core import TyperCommand, TyperGroup

from abist_kb.config import Settings
from abist_kb.domain.errors import AppError, ErrorCode, wrap
from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.presenter import Presenter

UNEXPECTED_ERROR_MESSAGE = "予期しないエラーが発生しました。"
UNEXPECTED_ERROR_HINT = "--debug を付けて再実行すると詳細なトレースバックを確認できます。"
"""デフォード#13 の回帰修正で使う定型文言。`AppErrorHandlingCommand.invoke()` と
`app.py::main()` の両方の最終防衛線で同じ文言・同じ `ErrorCode.FAILURE` を使い、
挙動の異なる2つの正規化にならないようにする(`app.py` からも import して使う
ため、モジュール内部限定を示す先頭アンダースコアは付けていない)。"""


@dataclass(frozen=True, slots=True)
class CliContext:
    """コールバックが構築し `ctx.obj` として全サブコマンドへ渡す実行コンテキスト。"""

    settings: Settings
    presenter: Presenter
    debug: bool
    assume_yes: bool


def get_context(ctx: typer.Context) -> CliContext:
    """`ctx.obj` から `CliContext` を取得する。

    Typer のグループコールバックが子コンテキストへ `obj` を伝播させるため、
    サブコマンドの `ctx` からも常に取得できる。コールバックより先に
    サブコマンド本体が呼ばれることは Typer の実行順序上あり得ないが、
    想定外の呼び出し順を早期に検出できるよう型を検査する。
    """
    obj = ctx.obj
    if not isinstance(obj, CliContext):
        raise RuntimeError(
            "CliContext が未構築です。Typer のコールバックが先に実行されている前提が崩れています。"
        )
    return obj


def fail(presenter: Presenter | None, err: AppError) -> NoReturn:
    """`AppError` を Presenter で提示し、対応する終了コードで CLI を終了する。

    Typer のコールバック/コマンドの内側から呼ぶ想定。オプション検証など
    Presenter 構築前の失敗にも対応できるよう、未構築時は標準出力/標準エラーへ
    直結する最小限の Presenter をその場で作る。`typer.Exit` は Click の
    `standalone_mode` により `sys.exit()` へ変換されるため、`CliRunner` 経由の
    テストでも実プロセスでも同じ終了コードになる。
    """
    active = presenter or Presenter(OutputMode.PLAIN, stdout=sys.stdout, stderr=sys.stderr)
    active.error(err)
    raise typer.Exit(code=int(err.exit_code))


class AppErrorHandlingCommand(TyperCommand):
    """`AppError` を Presenter で提示し終了コードへ変換する、コマンド実行の単一の関所。

    Typer 標準の `TyperCommand.invoke()` は Click の例外階層
    (`ClickException`/`Exit`/`Abort`)に属さない一般的な `Exception` を一切捕捉しない。
    `AppError` はまさにそれに該当するため、コマンド本体が自前で
    `try/except AppError: ... raise typer.Exit(...)` を書き忘れると、実プロセスでは
    `app.py::main()` に残した防御的な `except AppError` が拾って正しい終了コードに
    なる一方、`typer.testing.CliRunner` はコマンドの click オブジェクトを直接呼び
    `main()` を経由しないため、`AppError` は `CliRunner.invoke()` の汎用の
    `except Exception` に落ちて `exit_code=1` に潰れてしまう。実プロセスとテストで
    挙動が食い違うこの罠は、書き忘れた開発者が最も気付きにくい形で顕在化する。

    M3 で追加される各コマンドグループ(`source`/`batch`/`sync`/`jobs`/`worker`/
    `document` 等)が個別に対応しなくても済むよう、この変換をコマンドクラス側に
    一箇所だけ実装し、`AppTyper` 経由で全コマンドへ既定適用する。

    デフォード#13 の回帰修正: `AppError` に正規化されていない未捕捉の例外
    (プログラムのバグ、例: `KeyError`)も同じ関所で拾う。以前はここで
    `AppError` しか捕捉していなかったため、そのような例外は Click/Typer の
    `_main()` にも捕捉されず(`ClickException`/`Exit`/`Abort`/`OSError` しか
    見ていない)、Python インタプリタの既定のハンドラまでそのまま突き抜けて、
    `--debug` の有無に関わらず生のトレースバックがコード無し・回復手順無しで
    出力されていた(§8 違反)。`typer.Exit`/`typer.Abort`/`ClickException`
    (`typer.BadParameter` 等の `UsageError` 系を含む)は Click 自身の制御フロー
    なのでここでは再送出し、素通しする。
    """

    def invoke(self, ctx: typer.Context) -> Any:
        try:
            return super().invoke(ctx)
        except AppError as err:
            presenter = ctx.obj.presenter if isinstance(ctx.obj, CliContext) else None
            fail(presenter, err)
        except (Exit, Abort, ClickException):
            raise
        except Exception as exc:
            presenter = ctx.obj.presenter if isinstance(ctx.obj, CliContext) else None
            fail(
                presenter,
                wrap(
                    exc,
                    code=ErrorCode.FAILURE,
                    message=UNEXPECTED_ERROR_MESSAGE,
                    hint=UNEXPECTED_ERROR_HINT,
                ),
            )


class AppErrorHandlingGroup(TyperGroup):
    """`AppError` を Presenter で提示し終了コードへ変換する、グループ実行の単一の関所。

    `AppErrorHandlingCommand` と対になる修正。Click の `MultiCommand.invoke()`
    (`TyperGroup` の実行経路)は、グループ自身のコールバック(`@group.callback()`、
    サブコマンド共通オプションの自然な置き場所。例: `source --id`、`jobs --state`)を
    `Command.invoke(self, ctx)` として実行してからサブコマンドへディスパッチする。
    このグループコールバックが送出する `AppError` は `AppErrorHandlingCommand`(個々の
    コマンド本体だけを保護する)の対象外であり、`AppTyper()` で作ったグループでも
    保護されていなかった(症状は `AppErrorHandlingCommand` と同じ: 実プロセスは
    `main()` の残置キャッチで正しい終了コードになる一方、`CliRunner` では
    `exit_code=1` に潰れる)。サブコマンド自身の `invoke()` が既にここで `AppError` を
    `fail()` へ変換して `typer.Exit` を送出済みの場合、その `typer.Exit` は
    `AppError` ではないためここでは再捕捉されず、二重処理は起きない。
    """

    def invoke(self, ctx: typer.Context) -> Any:
        try:
            return super().invoke(ctx)
        except AppError as err:
            presenter = ctx.obj.presenter if isinstance(ctx.obj, CliContext) else None
            fail(presenter, err)


class AppTyper(typer.Typer):
    """コマンド・グループ双方の既定 `cls` を自動的に安全側へ倒す `Typer` サブクラス。

    `app.py`/`config_cmd.py` はどちらも素の `typer.Typer` の代わりにこれを使う。
    こうしておけば、`@app.command()` を書くだけの新しいコマンドも、
    `@group.callback()` を書くだけの新しいグループコールバックも、
    `cls=` を明示しなくても `AppError` を正しい終了コードへ変換する既定の安全網を
    自動的に継承する(opt-in不要)。`add_typer()` は親の設定を子へ遡及適用しないため、
    サブコマンド群を作るときも必ず素の `typer.Typer()` ではなく `AppTyper()` を使うこと。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("cls", AppErrorHandlingGroup)
        super().__init__(*args, **kwargs)

    def command(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        kwargs.setdefault("cls", AppErrorHandlingCommand)
        return super().command(*args, **kwargs)


__all__ = [
    "UNEXPECTED_ERROR_HINT",
    "UNEXPECTED_ERROR_MESSAGE",
    "AppErrorHandlingCommand",
    "AppErrorHandlingGroup",
    "AppTyper",
    "CliContext",
    "fail",
    "get_context",
]
