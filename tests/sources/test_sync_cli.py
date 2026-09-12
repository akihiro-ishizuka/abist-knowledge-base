"""`sync` CLI コマンド群の受入テスト(`source`/`batch`/`all`)。

`docs-write` リース経由でジョブとして実行される経路そのもの(`source add` →
`batch add` → `sync batch`)を、他の CLI コマンド群のテスト(`tests/cli/
test_batch_cmd.py` 等)と同じ流儀(`typer.testing.CliRunner` + `--root`)で検証する。
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

from .conftest import FAKE_TOKEN, MockEsaServer
from .test_esa import make_post
from .test_web import WebPageServer

runner = CliRunner()


def _root_args(tmp_root, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


@pytest.fixture
def web_server():  # type: ignore[no-untyped-def]
    server = WebPageServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


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
    assert add_batch.exit_code == 0, add_batch.output
    result = runner.invoke(
        app,
        _root_args(tmp_root, "--output", "json", "sync", "batch", "CLIバッチ"),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"]["totals"]["added"] == 1


def test_sync_batch_web_via_cli(tmp_root, web_server: WebPageServer) -> None:
    web_server.state.body = "<p>本文</p>"

    add_batch = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "batch",
            "add",
            "--name",
            "CLI webバッチ",
            "--type",
            "web",
            "--output-dir",
            "docs/_cli_web_test",
            "--items",
            json.dumps([{"options": {"url": web_server.base_url + "/", "max_depth": 0}}]),
        ),
    )
    assert add_batch.exit_code == 0, add_batch.output
    batch_id = json.loads(add_batch.stdout)["id"]

    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "sync", "batch", batch_id))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"]["totals"]["added"] == 1


def test_batch_add_git_is_rejected(tmp_root) -> None:
    add_batch = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "batch",
            "add",
            "--name",
            "CLI gitバッチ",
            "--type",
            "git",
            "--output-dir",
            "docs/_cli_git_test",
            "--items",
            json.dumps([{"options": {"repository": "https://example.com/repo.git"}}]),
        ),
    )
    assert add_batch.exit_code != 0
    assert "Git 同期は削除" in add_batch.output


def test_sync_all_via_cli_exits_5_on_partial_failure(tmp_root, esa_server: MockEsaServer) -> None:
    """task-5b の判断: `sync all` は1バッチの失敗で残りを止めず、終了コード5で終わる。"""
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
            "docs/_cli_all_test",
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

    ok_batch = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "batch",
            "add",
            "--name",
            "成功する方",
            "--type",
            "esa",
            "--output-dir",
            "docs/_cli_all_test",
            "--items",
            json.dumps([{"source_id": source_id, "target": "対象カテゴリ"}]),
        ),
    )
    assert ok_batch.exit_code == 0, ok_batch.output

    bad_batch = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "batch",
            "add",
            "--name",
            "失敗する方",
            "--type",
            "web",
            "--output-dir",
            "docs/_cli_all_fail",
            "--items",
            json.dumps([{"options": {}}])
        ),
    )
    assert bad_batch.exit_code == 0, bad_batch.output

    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "sync", "all"))

    assert result.exit_code == 5, result.output
    payload = json.loads(result.stdout)
    results = payload["results"]
    assert len(results) == 2
    by_name = {r["batch_name"]: r for r in results}
    assert "error" in by_name["失敗する方"]
    assert by_name["成功する方"]["summary"]["totals"]["added"] == 1
