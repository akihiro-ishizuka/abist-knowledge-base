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
from typer.core import TyperCommand

from abist_kb.config import Settings
from abist_kb.domain.errors import AppError
from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.presenter import Presenter


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
    """

    def invoke(self, ctx: typer.Context) -> Any:
        try:
            return super().invoke(ctx)
        except AppError as err:
            presenter = ctx.obj.presenter if isinstance(ctx.obj, CliContext) else None
            fail(presenter, err)


class AppTyper(typer.Typer):
    """`command()` の既定 `cls` を `AppErrorHandlingCommand` にする `Typer` サブクラス。

    `app.py`/`config_cmd.py` はどちらも素の `typer.Typer` の代わりにこれを使う。
    こうしておけば、`@app.command()` を書くだけの新しいコマンドが
    `cls=AppErrorHandlingCommand` を明示しなくても、`AppError` を正しい終了コードへ
    変換する既定の安全網を自動的に継承する(opt-in不要)。
    """

    def command(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        kwargs.setdefault("cls", AppErrorHandlingCommand)
        return super().command(*args, **kwargs)


__all__ = ["AppErrorHandlingCommand", "AppTyper", "CliContext", "fail", "get_context"]
