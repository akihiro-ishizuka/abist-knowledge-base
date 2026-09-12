"""`batch` コマンド群(設計書 §7, §11.2): list/show/add/edit/remove/run/import。

**バッチは app.sqlite が正であり、旧 `batch-config.js` へは書き戻さない。**
`import` は読み取り専用の一方向インポート(`BatchService.import_from_old_config`)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from abist_kb.application.batch_service import BatchService
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.cli.context import AppTyper, get_context
from abist_kb.presentation.console.presenter import Presenter

batch_app = AppTyper(help="バッチ(収集対象定義)の管理。", no_args_is_help=True)


def _parse_items_json(value: str | None) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"--items は JSON として解析できません: {exc}",
            exit_code=ExitCode.INVALID_INPUT,
        ) from exc
    if not isinstance(parsed, list):
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message="--items は JSON 配列である必要があります。",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return parsed


def resolve_batch(service: BatchService, identifier: str) -> dict[str, Any]:
    """バッチを ID 優先、見つからなければ一意な名前で解決する。"""
    try:
        return service.show(identifier)
    except AppError as exc:
        if exc.code != ErrorCode.NOT_FOUND:
            raise
    return service.list_by_name(identifier)


def _present_batch(presenter: Presenter, batch: dict[str, Any]) -> None:
    if presenter.is_json:
        presenter.json_result(batch)
        return
    presenter.table(
        "バッチ",
        ["列", "値"],
        [[key, value] for key, value in batch.items() if key != "items"],
    )
    presenter.table(
        "対象",
        ["position", "source_id", "target", "options"],
        [
            [item["position"], item["source_id"], item["target"], item["options"]]
            for item in batch.get("items", [])
        ],
    )


@batch_app.command("list")
def batch_list(ctx: typer.Context) -> None:
    """バッチ一覧。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        batches = BatchService(conn).list()
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result({"batches": batches})
            return
        rows = [
            [b["id"], b["name"], b["type"], b["output_dir"], b["enabled"], len(b["items"])]
            for b in batches
        ]
        cli_ctx.presenter.table(
            "バッチ一覧", ["id", "name", "type", "output_dir", "enabled", "items"], rows
        )
    finally:
        conn.close()


@batch_app.command("show")
def batch_show(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="バッチ ID または名前。")],
) -> None:
    """バッチの詳細(対象一覧を含む)。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        batch = resolve_batch(BatchService(conn), batch_id)
        _present_batch(cli_ctx.presenter, batch)
    finally:
        conn.close()


@batch_app.command("add")
def batch_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name")],
    type: Annotated[str, typer.Option("--type", help="esa/web。")],
    output_dir: Annotated[str | None, typer.Option("--output-dir")] = None,
    items: Annotated[
        str | None, typer.Option("--items", help="対象一覧(JSON配列)。省略時は空。")
    ] = None,
) -> None:
    """バッチを新規追加する。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        created = BatchService(conn).add(
            name=name, type=type, output_dir=output_dir, items=_parse_items_json(items) or []
        )
        _present_batch(cli_ctx.presenter, created)
    finally:
        conn.close()


@batch_app.command("edit")
def batch_edit(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="バッチ ID または名前。")],
    output_dir: Annotated[str | None, typer.Option("--output-dir")] = None,
    items: Annotated[
        str | None, typer.Option("--items", help="対象一覧(JSON配列)。指定時のみ全置換する。")
    ] = None,
    enable: Annotated[bool | None, typer.Option("--enable/--disable")] = None,
) -> None:
    """バッチの一部の属性を変更する(未指定の属性は変わらない)。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        service = BatchService(conn)
        resolved_id = resolve_batch(service, batch_id)["id"]
        fields: dict[str, Any] = {}
        if output_dir is not None:
            fields["output_dir"] = output_dir
        if enable is not None:
            fields["enabled"] = enable
        parsed_items = _parse_items_json(items)
        if parsed_items is not None:
            fields["items"] = parsed_items
        updated = service.edit(resolved_id, **fields)
        _present_batch(cli_ctx.presenter, updated)
    finally:
        conn.close()


@batch_app.command("remove")
def batch_remove(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="バッチ ID または名前。")],
) -> None:
    """バッチを削除する(確認必須、監査記録あり)。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        service = BatchService(conn)
        existing = resolve_batch(service, batch_id)
        resolved_id = existing["id"]
        removed = service.remove(
            resolved_id,
            confirm=lambda prompt: cli_ctx.presenter.confirm(prompt, assume_yes=cli_ctx.assume_yes),
        )
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result({"id": resolved_id, "removed": removed})
            return
        if removed:
            cli_ctx.presenter.success(f"バッチを削除しました: {existing['name']}")
        else:
            cli_ctx.presenter.info("削除を中止しました。")
    finally:
        conn.close()


@batch_app.command("run")
def batch_run(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="バッチ ID または名前。")],
) -> None:
    """バッチの実行ジョブを投入し、完了まで同期実行する。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        service = BatchService(conn, settings=cli_ctx.settings)
        resolved_id = resolve_batch(service, batch_id)["id"]
        job = service.run(resolved_id)
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result(job)
            return
        cli_ctx.presenter.table("ジョブ", ["列", "値"], [[k, v] for k, v in job.items()])
    finally:
        conn.close()


@batch_app.command("import")
def batch_import(
    ctx: typer.Context,
    config_path: Annotated[Path, typer.Argument(help="旧 batch-config.js のパス(読み取り専用)。")],
) -> None:
    """旧 `batch-config.js` から一方向インポートする(旧ファイルへは書き戻さない)。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        result = BatchService(conn).import_from_old_config(config_path)
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result(result)
            return
        cli_ctx.presenter.success(f"{result['imported']}件のバッチをインポートしました。")
    finally:
        conn.close()


__all__ = ["batch_app", "resolve_batch"]
