"""`source` コマンド群(設計書 §7): list/add/edit/remove/test。"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from abist_kb.application.source_service import SourceService
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.cli.context import AppTyper, get_context
from abist_kb.presentation.console.presenter import Presenter

source_app = AppTyper(help="ソース(接続設定)の管理。", no_args_is_help=True)


def _parse_connection_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"--connection は JSON として解析できません: {exc}",
            exit_code=ExitCode.INVALID_INPUT,
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message="--connection は JSON オブジェクトである必要があります。",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return parsed


def _present_source(presenter: Presenter, source: dict[str, Any]) -> None:
    if presenter.is_json:
        presenter.json_result(source)
        return
    presenter.table(
        "ソース",
        ["列", "値"],
        [[key, value] for key, value in source.items()],
    )


@source_app.command("list")
def source_list(ctx: typer.Context) -> None:
    """ソース一覧。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        sources = SourceService(conn).list()
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result({"sources": sources})
            return
        rows = [
            [s["id"], s["type"], s["display_name"], s["output_dir"], s["enabled"]] for s in sources
        ]
        cli_ctx.presenter.table(
            "ソース一覧", ["id", "type", "display_name", "output_dir", "enabled"], rows
        )
    finally:
        conn.close()


@source_app.command("add")
def source_add(
    ctx: typer.Context,
    type: Annotated[str, typer.Option("--type", help="ソース種別(esa/web)。")],
    display_name: Annotated[str, typer.Option("--display-name", help="表示名。")],
    output_dir: Annotated[str, typer.Option("--output-dir", help="出力先ディレクトリ。")],
    connection: Annotated[
        str | None, typer.Option("--connection", help="接続設定(JSON文字列)。")
    ] = None,
    disabled: Annotated[
        bool, typer.Option("--disabled", help="無効化した状態で作成する。")
    ] = False,
) -> None:
    """ソースを新規追加する。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        service = SourceService(conn)
        created = service.add(
            type=type,
            display_name=display_name,
            connection=_parse_connection_json(connection),
            output_dir=output_dir,
            enabled=not disabled,
        )
        _present_source(cli_ctx.presenter, created)
    finally:
        conn.close()


@source_app.command("edit")
def source_edit(
    ctx: typer.Context,
    source_id: Annotated[str, typer.Argument(help="ソースID。")],
    display_name: Annotated[str | None, typer.Option("--display-name")] = None,
    output_dir: Annotated[str | None, typer.Option("--output-dir")] = None,
    connection: Annotated[
        str | None, typer.Option("--connection", help="接続設定(JSON文字列)。")
    ] = None,
    enable: Annotated[bool | None, typer.Option("--enable/--disable")] = None,
) -> None:
    """ソースの一部の属性を変更する(未指定の属性は変わらない)。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        service = SourceService(conn)
        fields: dict[str, Any] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        if output_dir is not None:
            fields["output_dir"] = output_dir
        if connection is not None:
            fields["connection"] = _parse_connection_json(connection)
        if enable is not None:
            fields["enabled"] = enable
        updated = service.edit(source_id, **fields)
        _present_source(cli_ctx.presenter, updated)
    finally:
        conn.close()


@source_app.command("remove")
def source_remove(
    ctx: typer.Context,
    source_id: Annotated[str, typer.Argument(help="ソースID。")],
) -> None:
    """ソースを削除する(確認必須、監査記録あり)。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        service = SourceService(conn)
        existing = service.get(source_id)
        removed = service.remove(
            source_id,
            confirm=lambda prompt: cli_ctx.presenter.confirm(prompt, assume_yes=cli_ctx.assume_yes),
        )
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result({"id": source_id, "removed": removed})
            return
        if removed:
            cli_ctx.presenter.success(f"ソースを削除しました: {existing['display_name']}")
        else:
            cli_ctx.presenter.info("削除を中止しました。")
    finally:
        conn.close()


@source_app.command("test")
def source_test(
    ctx: typer.Context,
    source_id: Annotated[str, typer.Argument(help="ソースID。")],
) -> None:
    """接続設定の健全性をテストする(実ネットワーク呼び出しはしない)。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        service = SourceService(conn, settings=cli_ctx.settings)
        result = service.test_connection(source_id)
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result(result)
            return
        if result["ok"]:
            cli_ctx.presenter.success(result["detail"])
        else:
            cli_ctx.presenter.danger(result["detail"])
    finally:
        conn.close()


__all__ = ["source_app"]
