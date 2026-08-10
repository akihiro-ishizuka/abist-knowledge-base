"""`init` コマンド(設計書 §7.3): ワークスペースの scaffold。

新鮮なクローンで最初に実行するコマンド。ロジックは
`application.workspace_init.init_workspace` にあり、ここは提示だけを行う。
"""

from __future__ import annotations

import typer

from abist_kb import identity
from abist_kb.application.workspace_init import init_workspace, reference_corpus_dir
from abist_kb.presentation.cli.context import get_context


def init(ctx: typer.Context) -> None:
    """ディレクトリ・設定雛形・`app.sqlite` を作成する(既存物は変更しない)。"""
    cli_ctx = get_context(ctx)
    result = init_workspace(cli_ctx.settings)

    if cli_ctx.presenter.is_json:
        cli_ctx.presenter.json_result(result)
        return

    presenter = cli_ctx.presenter
    presenter.success(
        f"ワークスペースを初期化しました: {result['root_dir']}"
        f"(ディレクトリ 作成 {len(result['created_dirs'])} / "
        f"既存 {len(result['skipped_dirs'])}、"
        f"ファイル 作成 {len(result['created_files'])} / "
        f"既存 {len(result['skipped_files'])})"
    )
    for path in result["created_dirs"]:
        presenter.line(f"  [作成] {path}/")
    for path in result["created_files"]:
        presenter.line(f"  [作成] {path}")
    presenter.line(
        f"  [{'作成' if result['app_db_initialized'] else '既存'}] {result['app_db_path']}"
    )

    # 案内するパスは `Settings` から作る(カスタム `docs_dir` の環境で
    # `docs/` と案内すると、実際には索引されない場所へ置かせてしまう)。
    cli = identity.CLI_NAME
    presenter.info("次の手順:")
    presenter.line(
        f"  1. {cli_ctx.settings.docs_dir} に Markdown を置く"
        f"(参照コーパスは {reference_corpus_dir(cli_ctx.settings)})"
    )
    presenter.line(f"  2. {cli} document register-disk --apply   # documents へ登録")
    presenter.line(f"  3. {cli} index build --corpus work        # 索引を構築")
    presenter.line(f"  4. {cli} index embed --corpus work        # 意味検索が必要な場合")


__all__ = ["init"]
