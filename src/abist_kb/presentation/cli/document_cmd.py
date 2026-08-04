"""`document` コマンド群(設計書 §7): `document show <path>`。

一覧・削除等の追加操作は `DocumentService` に既に用意されているが、本タスクの
CLI 契約(ブリーフ Step 4)が要求するのは `show` のみのため、それだけを配線する。
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from abist_kb.application.document_service import DocumentService
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.cli.context import AppTyper, get_context

document_app = AppTyper(help="文書の参照。", no_args_is_help=True)


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


__all__ = ["document_app"]
