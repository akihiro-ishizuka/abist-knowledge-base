"""`search` コマンド(設計書 §7, task-4-brief Step2)。

読み取り専用(`corpus-write` リースは取得しない)。出典(`path:start_line-end_line`)を
人間向け表示で常時表示する — 検索結果は `index_stale` な場合があり、行番号を
無条件に信用してよいわけではないが、それでも出典そのものは必ず提示する
(信頼性の判断は利用者・後続の `get_document` に委ねる)。
"""

from __future__ import annotations

from typing import Annotated

import typer

from abist_kb.application.search_service import SearchService
from abist_kb.presentation.cli.context import get_context


def _build_service(cli_ctx) -> SearchService:  # type: ignore[no-untyped-def]
    settings = cli_ctx.settings
    return SearchService(
        docs_dir=settings.docs_dir,
        work_index_path=settings.work_index_path,
        reference_index_path=settings.reference_index_path,
    )


def search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="検索クエリ。")],
    source: Annotated[str | None, typer.Option("--source", help="source で絞り込む。")] = None,
    document_type: Annotated[
        str | None, typer.Option("--document-type", help="document_type で絞り込む。")
    ] = None,
    status: Annotated[str | None, typer.Option("--status", help="status で絞り込む。")] = None,
    path_prefix: Annotated[
        str | None, typer.Option("--path-prefix", help="path の前方一致で絞り込む。")
    ] = None,
    corpus: Annotated[
        str, typer.Option("--corpus", help="対象コーパス(work/reference)。")
    ] = "work",
    limit: Annotated[int, typer.Option("--limit", help="最大件数。")] = 10,
) -> None:
    """`query` を検索する(全文 + ベクトルのハイブリッド検索)。"""
    cli_ctx = get_context(ctx)
    service = _build_service(cli_ctx)
    result = service.search(
        query,
        corpus=corpus,
        source=source,
        document_type=document_type,
        status=status,
        path_prefix=path_prefix,
        limit=limit,
    )

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result)
        return

    if result["count"] == 0:
        cli_ctx.presenter.warning("該当する結果はありませんでした。")
    for item in result["results"]:
        stale = " [STALE]" if item.get("index_stale") else ""
        citation = f"{item['path']}:{item.get('start_line')}-{item.get('end_line')}{stale}"
        cli_ctx.presenter.line(citation)
        title = item.get("title") or ""
        heading = item.get("heading_path") or ""
        cli_ctx.presenter.muted(f"  {title} > {heading}" if heading else f"  {title}")
        cli_ctx.presenter.muted(f"  {item.get('snippet', '')}")
    if result.get("note"):
        cli_ctx.presenter.info(result["note"])


__all__ = ["search"]
