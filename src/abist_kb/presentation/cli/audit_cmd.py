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

from abist_kb.application.audit.backfill_metadata import (
    BackfillMetadataService,
    run_backfill_inline,
)
from abist_kb.application.audit.check_contradictions import CheckContradictionsService
from abist_kb.application.audit.find_duplicates import FindDuplicatesService
from abist_kb.application.audit.parallel_compare import default_repo_root, run_parallel_compare
from abist_kb.application.audit.search_quality import (
    DEFAULT_QUERIES_RELATIVE_PATH,
    compare_with_previous,
    evaluate,
    find_latest_report,
    load_queries,
    write_report,
)
from abist_kb.application.audit.verify_integrity import VerifyIntegrityService
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.cli.context import AppTyper, get_context

audit_app = AppTyper(
    help="品質監査(整合性・重複・矛盾・メタデータ補完・検索評価)。", no_args_is_help=True
)


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


@audit_app.command("parallel-compare")
def parallel_compare(
    ctx: typer.Context,
    corpus: Annotated[
        str, typer.Option("--corpus", help="検索品質比較に使う索引コーパス(work/reference)。")
    ] = "work",
    queries: Annotated[
        Path | None, typer.Option("--queries", help="評価クエリの JSON Lines ファイル。")
    ] = None,
    skip_search_quality: Annotated[
        bool,
        typer.Option(
            "--skip-search-quality", help="検索品質(22クエリ)の比較を省略する(索引未構築時)。"
        ),
    ] = False,
    repo_root: Annotated[
        Path | None,
        typer.Option(
            "--repo-root",
            help="pytest を実行する abist-knowledge-base ソースツリーのルート"
            "(既定: このパッケージの所在から自動検出。`--root` とは別物)。",
        ),
    ] = None,
) -> None:
    """M9 並行稼働比較(設計書 §14 step 8): 同期・MCP・検索品質を1コマンドで比較する。

    M3 のバイト同一検証(`tests/sources/test_e2e_byte_identity.py`)、
    M5 の MCP契約diff(`tests/mcp/test_all_server_tools_list_diff.py`)、
    M4 の検索品質評価(`audit search-quality` と同じ計算)を1回で実行し、
    まとめて記録する。読み取り専用(`corpus-write` リースは取得しない)。
    旧リポジトリが無い環境では同期比較セクションが自動的に `skipped` になる。
    """
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings

    conn = None
    queries_path = None
    if not skip_search_quality:
        index_path = _index_path_for(settings, corpus)
        if index_path.is_file():
            queries_path = _resolve_queries_path(settings, queries)
            conn = connect(index_path, read_only=True)

    try:
        report = run_parallel_compare(
            repo_root=repo_root or default_repo_root(),
            work_index_conn=conn,
            queries_path=queries_path,
            reports_dir=settings.reports_dir,
            docs_dir=settings.docs_dir,
        )
    finally:
        if conn is not None:
            conn.close()

    result = report.as_dict()
    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result)
        return

    cli_ctx.presenter.line(f"同期・収集(バイト同一): {report.sync_and_collection.status}")
    cli_ctx.presenter.line(f"MCP契約diff: {report.mcp_contract.status}")
    sq_status = report.search_quality.get("status", "skipped")
    cli_ctx.presenter.line(f"検索品質(22クエリ): {sq_status}")
    if sq_status == "ok":
        for method, metrics in report.search_quality["macro"].items():
            cli_ctx.presenter.line(
                f"  {method}: recall5={metrics['recall5']:.4f} mrr={metrics['mrr']:.4f} "
                f"ndcg10={metrics['ndcg10']:.4f}"
            )
        for warning in report.search_quality.get("warnings", []):
            cli_ctx.presenter.warning(
                f"{warning['method']}/{warning['metric']} が悪化しました: "
                f"{warning['previous']:.4f} -> {warning['current']:.4f}"
            )
    if not report.all_ok():
        cli_ctx.presenter.warning(
            "並行比較で不一致・失敗が検出されました。詳細は上記を確認してください。"
        )


@audit_app.command("integrity")
def integrity(
    ctx: typer.Context,
    no_update: Annotated[
        bool, typer.Option("--no-update", help="検査のみ行い documents を書き換えない。")
    ] = False,
) -> None:
    """documents と実ファイルを突き合わせ、6状態に分類する(欠落は削除の証拠にしない)。"""
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    conn = open_app_db(settings.app_db_path)
    try:
        service = VerifyIntegrityService(conn, docs_dir=settings.docs_dir)
        result = service.run(update_db=not no_update)
    finally:
        conn.close()

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result({"run_id": result.run_id, "totals": result.totals.as_dict()})
        return
    for key, value in result.totals.as_dict().items():
        cli_ctx.presenter.line(f"{key}: {value}")
    cli_ctx.presenter.info(f"監査実行ID: {result.run_id}")


@audit_app.command("duplicates")
def duplicates(
    ctx: typer.Context,
    corpus: Annotated[
        str, typer.Option("--corpus", help="近似重複検出に使う索引コーパス(work/reference)。")
    ] = "work",
) -> None:
    """same_article / identical / near の3種の重複候補を報告する(変更は一切行わない)。"""
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    conn = open_app_db(settings.app_db_path)
    index_path = _index_path_for(settings, corpus)
    index_conn = connect(index_path, read_only=True) if index_path.is_file() else None
    try:
        service = FindDuplicatesService(conn, index_conn=index_conn)
        result = service.run()
    finally:
        conn.close()
        if index_conn is not None:
            index_conn.close()

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(
            {
                "run_id": result.run_id,
                "totals": result.totals(),
                "same_article": result.same_article,
                "identical": result.identical,
                "near": result.near,
                "series": result.series,
            }
        )
        return
    for key, value in result.totals().items():
        cli_ctx.presenter.line(f"{key}: {value}")
    cli_ctx.presenter.info(f"監査実行ID: {result.run_id}")


@audit_app.command("contradictions")
def contradictions(
    ctx: typer.Context,
    similarity: Annotated[
        float, typer.Option("--similarity", help="矛盾候補とみなす段落類似度の下限。")
    ] = 0.99,
) -> None:
    """限定した候補文書対から、数値・否定・状態語の食い違いを報告する(変更は一切行わない)。"""
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    conn = open_app_db(settings.app_db_path)
    index_path = _index_path_for(settings, "work")
    index_conn = connect(index_path, read_only=True) if index_path.is_file() else None
    try:
        service = CheckContradictionsService(
            conn, docs_dir=settings.docs_dir, index_conn=index_conn
        )
        result = service.run(similarity=similarity)
    finally:
        conn.close()
        if index_conn is not None:
            index_conn.close()

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(
            {
                "run_id": result.run_id,
                "candidate_pair_count": result.candidate_pair_count,
                "candidates": [
                    {
                        "path_a": c.path_a,
                        "path_b": c.path_b,
                        "sources": c.sources,
                        "conflicts": c.conflicts,
                    }
                    for c in result.candidates
                ],
            }
        )
        return
    cli_ctx.presenter.line(f"候補ペア: {result.candidate_pair_count}")
    cli_ctx.presenter.line(f"矛盾候補: {len(result.candidates)}")
    cli_ctx.presenter.info(f"監査実行ID: {result.run_id}")


@audit_app.command("backfill-metadata")
def backfill_metadata(
    ctx: typer.Context,
    apply: Annotated[
        bool, typer.Option("--apply", help="実際に書き込む(既定は dry-run)。")
    ] = False,
) -> None:
    """front matter の4キー(source/managed_by/document_type/status)を前方補完する。

    既定は dry-run。`--apply` で実際に書き込む(§12: 破壊的操作は対象を提示して
    から実行する。非対話環境では `--yes` が必要)。
    """
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    conn = open_app_db(settings.app_db_path)
    try:
        service = BackfillMetadataService(conn, docs_dir=settings.docs_dir)
        if apply:
            preview = service.run(apply=False)
            write_count = sum(1 for p in preview.plans if p.action == "write")
            confirmed = cli_ctx.presenter.confirm(
                f"{write_count}件の文書に front matter を書き込みます。よろしいですか?",
                assume_yes=cli_ctx.assume_yes,
            )
            if not confirmed:
                cli_ctx.presenter.info("中止しました。何も書き込んでいません。")
                return
        result = run_backfill_inline(service, conn, apply=apply, confirmed=apply)
    finally:
        conn.close()

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(
            {"run_id": result.run_id, "mode": result.mode, "totals": result.totals.as_dict()}
        )
        return
    cli_ctx.presenter.line(f"モード: {result.mode}")
    for key, value in result.totals.as_dict().items():
        cli_ctx.presenter.line(f"{key}: {value}")
    cli_ctx.presenter.info(f"監査実行ID: {result.run_id}")


__all__ = ["audit_app"]
