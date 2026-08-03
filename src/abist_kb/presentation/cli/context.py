"""CLI 全体で共有する実行コンテキストとエラー処理(設計書 §7)。

Typer のコールバック(`app.py` の `main_callback`)が唯一の構築点であり、
すべてのサブコマンドはここで定義する `get_context()` 経由でのみ
`Settings`/`Presenter` へアクセスする。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import NoReturn

import typer

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


__all__ = ["CliContext", "fail", "get_context"]
