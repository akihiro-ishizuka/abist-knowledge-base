"""`audit` コマンド群(設計書 §7, task-5-brief Step2): search-quality。

読み取り専用(`corpus-write` リースは取得しない)。`tests/fixtures/eval/
queries.jsonl` 形式の22クエリを既定で読み、M4 の受け入れゲート(hybrid Recall@5
0.9445以上/bm25_raw 0.7855以上、`tests/fixtures/eval/baseline.json` 参照)の
実測値をそのまま算出する。`reports/eval/eval-<timestamp>.json` へ書き出し、
前回結果との比較(-0.01超の悪化)を警告する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from abist_kb.application.audit.search_quality import (
    DEFAULT_QUERIES_RELATIVE_PATH,
    compare_with_previous,
    evaluate,
    find_latest_report,
    load_queries,
    write_report,
)
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.presentation.cli.context import AppTyper, get_context

audit_app = AppTyper(help="品質監査(検索評価等)。", no_args_is_help=True)


def _resolve_queries_path(settings, queries: Path | None) -> Path:
    path = queries or (settings.root_dir / DEFAULT_QUERIES_RELATIVE_PATH)
    if not path.is_file():
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"評価クエリファイルが見つかりません: {path}",
            hint="`--queries` で JSON Lines のクエリファイルを指定してください。",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return path


def _index_path_for(settings, corpus: str) -> Path:
    if corpus == "work":
        return settings.work_index_path
    if corpus == "reference":
        return settings.reference_index_path
    raise AppError(
        code=ErrorCode.INVALID_INPUT,
        message=f"未知のコーパスです: {corpus!r}(有効値: work, reference)",
        exit_code=ExitCode.INVALID_INPUT,
    )


@audit_app.command("search-quality")
def search_quality(
    ctx: typer.Context,
    corpus: Annotated[
        str, typer.Option("--corpus", help="対象コーパス(work/reference)。")
    ] = "work",
    queries: Annotated[
        Path | None, typer.Option("--queries", help="評価クエリの JSON Lines ファイル。")
    ] = None,
) -> None:
    """22クエリを bm25_raw/hybrid で実行し、Recall@5/MRR/nDCG@10 を測定する。"""
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    queries_path = _resolve_queries_path(settings, queries)
    index_path = _index_path_for(settings, corpus)
    if not index_path.is_file():
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"コーパス '{corpus}' の索引がまだありません: {index_path}",
            hint="`index build` と `index embed` を先に実行してください。",
            exit_code=ExitCode.INVALID_INPUT,
        )

    query_list = load_queries(queries_path)
    conn = connect(index_path, read_only=True)
    try:
        report = evaluate(conn, query_list, docs_dir=settings.docs_dir)
    finally:
        conn.close()

    previous_path = find_latest_report(settings.reports_dir)
    previous = (
        json.loads(previous_path.read_text(encoding="utf-8")) if previous_path is not None else None
    )
    warnings = compare_with_previous(report, previous)
    report_path = write_report(report, reports_dir=settings.reports_dir)

    result = {
        "report_path": str(report_path),
        "previous_report_path": str(previous_path) if previous_path is not None else None,
        "macro": report["macro"],
        "warnings": warnings,
    }

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result)
        return

    for method, metrics in report["macro"].items():
        cli_ctx.presenter.line(
            f"{method}: recall5={metrics['recall5']:.4f} mrr={metrics['mrr']:.4f} "
            f"ndcg10={metrics['ndcg10']:.4f} zero_hit={metrics['zero_hit_queries']}"
        )
    for warning in warnings:
        cli_ctx.presenter.warning(
            f"{warning['method']}/{warning['metric']} が悪化しました: "
            f"{warning['previous']:.4f} -> {warning['current']:.4f}"
        )
    cli_ctx.presenter.info(f"レポート: {report_path}")


__all__ = ["audit_app"]
