"""`sync` CLI コマンド群の受入テスト(`source`/`batch`/`all`)。

`docs-write` リース経由でジョブとして実行される経路そのもの(`source add` →
`batch add` → `sync batch`)を、他の CLI コマンド群のテスト(`tests/cli/
test_batch_cmd.py` 等)と同じ流儀(`typer.testing.CliRunner` + `--root`)で検証する。
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

from .conftest import FAKE_TOKEN, MockEsaServer
from .test_esa import make_post

runner = CliRunner()


def _root_args(tmp_root, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


def test_sync_source_via_cli_writes_files_and_report(tmp_root, esa_server: MockEsaServer) -> None:
    esa_server.add_post(make_post(category="対象カテゴリ"))

    add_source = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "source",
            "add",
            "--type",
            "esa",
            "--display-name",
            "テストesa",
            "--output-dir",
            "docs/_cli_test",
            "--connection",
            json.dumps(
                {
                    "team": esa_server.team,
                    "access_token": FAKE_TOKEN,
                    "base_url": esa_server.base_url,
                }
            ),
        ),
    )
    assert add_source.exit_code == 0, add_source.output
    source_id = json.loads(add_source.stdout)["id"]

    result = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "sync",
            "source",
            source_id,
            "--category",
            "対象カテゴリ",
        ),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"]["totals"]["added"] == 1
    assert payload["report_path"] is not None

    saved = list((tmp_root / "docs" / "_cli_test" / "対象カテゴリ").glob("*.md"))
    assert len(saved) == 1


def test_sync_batch_via_cli(tmp_root, esa_server: MockEsaServer) -> None:
    esa_server.add_post(make_post(category="カテゴリX"))

    add_source = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "source",
            "add",
            "--type",
            "esa",
            "--display-name",
            "テストesa",
            "--output-dir",
            "docs/_cli_batch_test",
            "--connection",
            json.dumps(
                {
                    "team": esa_server.team,
                    "access_token": FAKE_TOKEN,
                    "base_url": esa_server.base_url,
                }
            ),
        ),
    )
    source_id = json.loads(add_source.stdout)["id"]

    add_batch = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "batch",
            "add",
            "--name",
            "CLIバッチ",
            "--type",
            "esa",
            "--output-dir",
            "docs/_cli_batch_test",
            "--items",
            json.dumps([{"source_id": source_id, "target": "カテゴリX"}]),
        ),
    )
    batch_id = json.loads(add_batch.stdout)["id"]

    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "sync", "batch", batch_id))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"]["totals"]["added"] == 1
