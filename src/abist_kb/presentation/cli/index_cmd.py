"""`index` コマンド群(設計書 §7, §11.2, task-4-brief Step2): build/embed/status。

`build`/`embed` は `corpus-write:<corpus>` リソースリース配下でインライン実行する
(`application.index_service.run_index_inline` 経由)。`status` は読み取り専用で
ジョブを経由しない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from abist_kb.application.audit.search_quality import (
    DEFAULT_QUERIES_RELATIVE_PATH,
    compare_tokenizers,
)
from abist_kb.application.audit.search_quality import load_queries as load_eval_queries
from abist_kb.application.index_service import CORPORA, IndexService, run_index_inline
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.cli.context import AppTyper, get_context

index_app = AppTyper(
    help="索引(work/reference)の構築・埋め込み生成・状態確認。", no_args_is_help=True
)


def _build_service(cli_ctx) -> IndexService:  # type: ignore[no-untyped-def]
    settings = cli_ctx.settings
    return IndexService(
        docs_dir=settings.docs_dir,
        app_db_path=settings.app_db_path,
        work_index_path=settings.work_index_path,
        reference_index_path=settings.reference_index_path,
    )


_CORPUS_OPTION = typer.Option("--corpus", help=f"対象コーパス({'/'.join(CORPORA)})。")


@index_app.command("build")
def index_build(
    ctx: typer.Context,
    corpus: Annotated[str, _CORPUS_OPTION] = "work",
) -> None:
    """`docs/` を再走査し、索引DBへ差分反映する。"""
    cli_ctx = get_context(ctx)
    service = _build_service(cli_ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        result = run_index_inline(service, conn, action="build", corpus=corpus)
    finally:
        conn.close()

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result)
        return
    summary = result.get("result", {}).get("summary", {})
    cli_ctx.presenter.success(
        f"索引構築完了({corpus}): 追加 {summary.get('documents_added', 0)} / "
        f"更新 {summary.get('documents_updated', 0)} / "
        f"変更なし {summary.get('documents_unchanged', 0)} / "
        f"削除 {summary.get('documents_removed', 0)}"
    )
    disk_only = result.get("result", {}).get("disk_only_count", 0)
    if disk_only:
        cli_ctx.presenter.warning(
            f"documents に無いが docs/ に実在するファイルが{disk_only}件あります"
            "(`disk_only_paths_sample` を参照してください)。"
        )


@index_app.command("embed")
def index_embed(
    ctx: typer.Context,
    corpus: Annotated[str, _CORPUS_OPTION] = "work",
) -> None:
    """`corpus` の未埋め込み/変更チャンクへ埋め込みを生成する。"""
    cli_ctx = get_context(ctx)
    service = _build_service(cli_ctx)
    conn = open_app_db(cli_ctx.settings.app_db_path)
    try:
        result = run_index_inline(service, conn, action="embed", corpus=corpus)
    finally:
        conn.close()

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result)
        return
    summary = result.get("result", {}).get("summary", {})
    cli_ctx.presenter.success(
        f"埋め込み生成完了({corpus}): 生成 {summary.get('generated', 0)} / "
        f"スキップ {summary.get('skipped', 0)} / 走査 {summary.get('scanned', 0)}"
    )


@index_app.command("status")
def index_status(ctx: typer.Context) -> None:
    """work/reference 両コーパスの索引状態を表示する。"""
    cli_ctx = get_context(ctx)
    service = _build_service(cli_ctx)
    result = service.status()

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result)
        return

    rows = []
    for corpus, info in result["corpora"].items():
        rows.append(
            [
                corpus,
                info["label"],
                "available" if info["available"] else "unavailable",
                info["documents"],
                info["chunks"],
                info["embedded_chunks"],
                info["last_indexed_at"] or "-",
            ]
        )
    cli_ctx.presenter.table(
        "索引状態",
        ["corpus", "label", "state", "documents", "chunks", "embedded_chunks", "last_indexed_at"],
        rows,
    )


@index_app.command("compare-tokenizers")
def index_compare_tokenizers(
    ctx: typer.Context,
    corpus: Annotated[str, _CORPUS_OPTION] = "work",
    queries: Annotated[
        Path | None, typer.Option("--queries", help="評価クエリの JSON Lines ファイル。")
    ] = None,
) -> None:
    """`unicode61` と `trigram` を同条件(bm25_raw相当)で比較する(task-5-brief Step2後段)。"""
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    queries_path = queries or (settings.root_dir / DEFAULT_QUERIES_RELATIVE_PATH)
    if not queries_path.is_file():
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"評価クエリファイルが見つかりません: {queries_path}",
            exit_code=ExitCode.INVALID_INPUT,
        )
    index_path = settings.work_index_path if corpus == "work" else settings.reference_index_path
    if not index_path.is_file():
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"コーパス '{corpus}' の索引がまだありません: {index_path}",
            hint="`index build` を先に実行してください。",
            exit_code=ExitCode.INVALID_INPUT,
        )

    query_list = load_eval_queries(queries_path)
    conn = connect(index_path, read_only=True)
    try:
        result = compare_tokenizers(conn, query_list)
    finally:
        conn.close()

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result)
        return
    for tokenizer, metrics in result["tokenizers"].items():
        cli_ctx.presenter.line(
            f"{tokenizer}: recall5={metrics['recall5']:.4f} mrr={metrics['mrr']:.4f} "
            f"ndcg10={metrics['ndcg10']:.4f} zero_hit={metrics['zero_hit_queries']}"
        )


__all__ = ["index_app"]
