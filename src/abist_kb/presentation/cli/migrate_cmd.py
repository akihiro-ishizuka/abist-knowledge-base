"""`migrate` コマンド群(設計書 §11.3): inspect/plan/run/verify。

**移行元は常に読み取り専用。** SQLite アクセスは `migration.sandbox` 経由の
サンドボックスコピーのみを開く。`run` は一時ディレクトリへ構築し、検証
成功後だけ正式データディレクトリへ swap する。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

import typer

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.migration.inventory import inspect_source
from abist_kb.migration.manifest import Manifest, load_manifest, save_manifest
from abist_kb.migration.plan import assert_safe_roots, build_plan, load_plan, write_plan
from abist_kb.migration.runner import run_migration, swap_into_place
from abist_kb.migration.verify import verify_migration
from abist_kb.presentation.cli.context import AppTyper, get_context

migrate_app = AppTyper(
    help="旧システムのデータ移行(inspect/plan/run/verify)。", no_args_is_help=True
)

_DEFAULT_FROM = Path(r"C:\Temp\multi-source-knowledge-base")
_DEFAULT_TO = Path(r"C:\Temp\abist-knowledge-base")

_FROM_OPTION = typer.Option("--from", help="移行元(旧システム)のルート。")
_TO_OPTION = typer.Option("--to", help="移行先(新システム)のデータルート。")


@migrate_app.command("inspect")
def migrate_inspect(
    ctx: typer.Context,
    from_root: Annotated[Path, _FROM_OPTION] = _DEFAULT_FROM,
    output: Annotated[Path | None, typer.Option("--output", help="レポート出力先 JSON。")] = None,
) -> None:
    """移行元を read-only で棚卸しする(ファイルシステムを正とする)。"""
    cli_ctx = get_context(ctx)
    with tempfile.TemporaryDirectory(prefix="abist-migrate-sandbox-") as sandbox:
        report = inspect_source(from_root, Path(sandbox))
    if output is not None:
        output.write_text(
            __import__("json").dumps(report.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(report.to_dict())
        return
    cli_ctx.presenter.table(
        "移行棚卸し",
        ["項目", "値"],
        [
            ("移行元", report.from_root),
            ("Markdown件数", str(len(report.markdown_candidates))),
            (
                "docs外の候補",
                str(sum(1 for c in report.markdown_candidates if not c.inside_docs)),
            ),
            ("文字化け疑い", str(sum(1 for c in report.markdown_candidates if c.encoding_issue))),
            (
                "front matter破損",
                str(sum(1 for c in report.markdown_candidates if c.frontmatter_broken)),
            ),
            ("DB未登録(孤児)", str(len(report.orphan_paths))),
            (
                "sync-state総行数",
                str(report.sync_state.total_rows) if report.sync_state else "(無し)",
            ),
            (
                "reference-index総行数",
                str(report.reference_index.total_rows) if report.reference_index else "(無し)",
            ),
        ],
    )
    for warning in report.warnings:
        cli_ctx.presenter.warning(warning)


@migrate_app.command("plan")
def migrate_plan(
    ctx: typer.Context,
    from_root: Annotated[Path, _FROM_OPTION] = _DEFAULT_FROM,
    to_root: Annotated[Path, _TO_OPTION] = _DEFAULT_TO,
    output: Annotated[Path, typer.Option("--output", help="plan.json の出力先。")] = Path(
        "migration-plan.json"
    ),
) -> None:
    """ファイル単位で copy/convert/regenerate/exclude を確定し `plan.json` を書く。"""
    cli_ctx = get_context(ctx)
    assert_safe_roots(from_root, to_root)
    with tempfile.TemporaryDirectory(prefix="abist-migrate-sandbox-") as sandbox:
        report = inspect_source(from_root, Path(sandbox))
    plan = build_plan(report, from_root, to_root)
    write_plan(plan, output)
    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(plan.to_dict())
        return
    cli_ctx.presenter.table(
        "移行計画",
        ["区分", "件数"],
        [(action, str(count)) for action, count in plan.summary().items()],
    )


@migrate_app.command("run")
def migrate_run(
    ctx: typer.Context,
    plan_path: Annotated[Path, typer.Option("--plan", help="plan.json のパス。")],
    manifest_path: Annotated[
        Path, typer.Option("--manifest", help="migration-manifest.json のパス。")
    ] = Path("migration-manifest.json"),
    build_dir: Annotated[
        Path | None, typer.Option("--build-dir", help="一時ビルドディレクトリ。省略時は自動作成。")
    ] = None,
    swap: Annotated[
        bool,
        typer.Option("--swap/--no-swap", help="検証は別途 verify で行う。既定では swap しない。"),
    ] = False,
) -> None:
    """`plan.json` に従って移行先を構築する(再開可能)。"""
    cli_ctx = get_context(ctx)
    plan = load_plan(plan_path)
    from_root = Path(plan.from_root)
    to_root = Path(plan.to_root)
    assert_safe_roots(from_root, to_root)

    manifest = load_manifest(manifest_path) or Manifest(
        from_root=str(from_root), to_root=str(to_root)
    )
    effective_build_dir = build_dir or (to_root.parent / f"{to_root.name}.migration-build")
    sandbox_dir = effective_build_dir.parent / f"{to_root.name}.migration-sandbox"

    manifest = run_migration(
        plan, manifest, manifest_path, from_root, effective_build_dir, sandbox_dir
    )
    save_manifest(manifest, manifest_path)

    if swap:
        if manifest.unexplained_gap():
            raise AppError(
                ErrorCode.MIGRATION_FAILED,
                "未完了の工程があるため swap できません。",
                details={"gaps": manifest.unexplained_gap()},
            )
        swap_into_place(effective_build_dir, to_root)
        manifest.swapped_in = True
        save_manifest(manifest, manifest_path)

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(manifest.to_dict())
        return
    cli_ctx.presenter.table(
        "移行実行",
        ["工程", "状態", "件数"],
        [(name, step.status, str(step.counts)) for name, step in manifest.steps.items()],
    )


@migrate_app.command("verify")
def migrate_verify(
    ctx: typer.Context,
    manifest_path: Annotated[
        Path, typer.Option("--manifest", help="migration-manifest.json のパス。")
    ],
) -> None:
    """manifest を §11.4 の検証条件と突合する。"""
    cli_ctx = get_context(ctx)
    manifest = load_manifest(manifest_path)
    if manifest is None:
        raise AppError(ErrorCode.MIGRATION_FAILED, f"manifest が見つかりません: {manifest_path}")
    to_root = Path(manifest.to_root)
    # swap 前は正式データディレクトリが未だ存在しないため、`run` が構築した
    # 一時ビルドディレクトリ(既定の命名規約)を検証対象にする。
    build_dir_name = f"{to_root.name}.migration-build"
    content_dir = to_root if manifest.swapped_in else to_root.parent / build_dir_name
    result = verify_migration(manifest, Path(manifest.from_root), content_dir)
    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result.to_dict())
    else:
        cli_ctx.presenter.table(
            "移行検証",
            ["条件", "結果", "詳細"],
            [(c.name, "OK" if c.passed else "NG", c.detail) for c in result.conditions],
        )
    if not result.ok:
        raise AppError(ErrorCode.MIGRATION_FAILED, "検証条件を満たしていません。")
