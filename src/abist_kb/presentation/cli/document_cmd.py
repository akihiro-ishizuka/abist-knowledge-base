"""`document` コマンド群(設計書 §7.3): `document show <path>` / `document register-disk`。

一覧・削除等の追加操作は `DocumentService` に既に用意されているが、CLI へ配線して
あるのはこの2つだけ。`register-disk` は手置き Markdown を `documents` 台帳へ登録する
(置いただけ・`index build` だけでは索引されないため。詳細は
`application.document_register` のモジュール docstring 参照)。
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from abist_kb.application.document_register import register_disk_documents
from abist_kb.application.document_service import DocumentService
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.cli.context import AppTyper, get_context

document_app = AppTyper(help="文書の参照・登録。", no_args_is_help=True)


@document_app.command("show")
def document_show(
    ctx: typer.Context, path: Annotated[str, typer.Argument(help="docs相対パス。")]
) -> None:
    """文書1件のメタデータ・同期状態を表示する。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        service = DocumentService(conn)
        doc: dict[str, Any] = service.get(path)
        if cli_ctx.presenter.is_json:
            cli_ctx.presenter.json_result(doc)
            return
        cli_ctx.presenter.table("文書", ["列", "値"], [[key, value] for key, value in doc.items()])
    finally:
        conn.close()


@document_app.command("register-disk")
def document_register_disk(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="documents へ実際に登録する(既定は dry-run)。"),
    ] = False,
) -> None:
    """`docs/` の未登録 Markdown を `documents` へ登録する(既存行は変更しない)。"""
    cli_ctx = get_context(ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        service = DocumentService(conn)
        result = register_disk_documents(service, cli_ctx.settings.docs_dir, apply=apply)
    finally:
        conn.close()

    presenter = cli_ctx.presenter
    if presenter.is_json:
        presenter.json_result(result)
        return

    prefix = "登録" if apply else "[dry-run] 登録予定"
    presenter.success(
        f"{prefix}: {result['registered']} 件 "
        f"(走査 {result['scanned']} / 既存のためスキップ {result['skipped_existing']})"
    )
    for path in result["paths"]:
        presenter.line(f"  {path}")
    if result["skipped_reference"]:
        presenter.warning(
            f"参照コーパス扱いのため登録しなかった文書が {result['skipped_reference']} 件あります。"
        )
    if result["skipped_unreadable"]:
        presenter.warning(
            f"UTF-8 として読めなかった文書が {result['skipped_unreadable']} 件あります。"
        )
    if not apply and result["registered"]:
        presenter.info("--apply を付けて再実行すると documents へ登録します。")
    if apply and result["registered"]:
        presenter.info("`index build --corpus work` を実行すると検索できるようになります。")


__all__ = ["document_app"]
