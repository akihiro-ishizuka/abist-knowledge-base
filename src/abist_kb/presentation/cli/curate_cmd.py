"""`curate` コマンド群(設計書 §7, M7 task-5): 知識昇格。

旧実装 `tools/knowledge-curator/promote.py` を Typer サブコマンドへ移植した
薄いラッパー。ロジックは `application.curation.promote` にある。原本は
一切書き込まない(ドラフトの新規作成/上書きのみ)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from abist_kb.application.curation.promote import (
    RecordNotFoundError,
    SourceMissingError,
    promote,
)
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.presentation.cli.context import AppTyper, get_context

curate_app = AppTyper(help="B32doc ページの curated procedures への昇格。", no_args_is_help=True)


@curate_app.command("promote")
def promote_cmd(
    ctx: typer.Context,
    id: Annotated[str, typer.Option("--id", help="索引の id(例: prtug/prtugbt0501)。")],
    index: Annotated[
        Path | None,
        typer.Option(
            "--index",
            help="procedures-index.jsonl のパス(既定: docs/knowledge/generated/b32doc/catalog/)。",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir", help="curated 出力ルート(既定: docs/knowledge/curated/procedures/)。"
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="書き込まずプレビューのみ表示する。")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="既存ファイルを上書きする。")] = False,
) -> None:
    """索引済み B32doc ページを1件、curated/procedures へ昇格(ドラフト生成)する。"""
    cli_ctx = get_context(ctx)
    presenter = cli_ctx.presenter
    root = cli_ctx.settings.root_dir

    try:
        result = promote(
            record_id=id,
            repo_root=root,
            index_path=index,
            output_dir=output_dir,
            dry_run=dry_run,
            force=force,
        )
    except FileNotFoundError as exc:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=str(exc),
            hint="先に `build_index` で procedures-index.jsonl を生成してください。",
            exit_code=ExitCode.INVALID_INPUT,
        ) from exc
    except RecordNotFoundError as exc:
        raise AppError(
            code=ErrorCode.NOT_FOUND,
            message=str(exc),
            exit_code=ExitCode.NOT_FOUND,
        ) from exc
    except SourceMissingError as exc:
        raise AppError(
            code=ErrorCode.NOT_FOUND,
            message=str(exc),
            exit_code=ExitCode.NOT_FOUND,
        ) from exc
    except FileExistsError as exc:
        raise AppError(
            code=ErrorCode.CONFLICT,
            message=str(exc),
            hint="--force で上書きできます。",
            exit_code=ExitCode.CONFLICT,
        ) from exc

    if dry_run:
        rel = (
            result.output_path.relative_to(root).as_posix()
            if result.output_path.is_relative_to(root)
            else str(result.output_path)
        )
        presenter.line(f"[dry-run] 生成予定: {rel}")
        presenter.line("-" * 60)
        preview_lines = result.content.splitlines()
        presenter.line("\n".join(preview_lines[:40]))
        if len(preview_lines) > 40:
            presenter.line(f"... （残り {len(preview_lines) - 40} 行を省略）")
        return

    rel = (
        result.output_path.relative_to(root).as_posix()
        if result.output_path.is_relative_to(root)
        else str(result.output_path)
    )
    presenter.line(f"[ok] 昇格ドラフトを作成: {rel}")
    presenter.line("  本文を整形し、front matter の status を reviewed にしてください。")


__all__ = ["curate_app"]
