"""`batch` CLI コマンド群の受入テスト。"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

runner = CliRunner()

_SAMPLE_BATCH_CONFIG_JS = """#!/usr/bin/env node

export const batchConfigs = {
  'esaBatch': [
    'カテゴリA',
    'カテゴリB/サブ'
  ]
};
"""


def _root_args(tmp_root, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


def test_batch_add_show_and_list(tmp_root):
    add_result = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "batch",
            "add",
            "--name",
            "b1",
            "--type",
            "esa",
            "--output-dir",
            "docs/b1",
            "--items",
            '[{"target": "cat1"}]',
        ),
    )
    assert add_result.exit_code == 0, add_result.output
    show_result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "batch", "show", "b1")
    )
    assert show_result.exit_code == 0, show_result.output
    payload = json.loads(show_result.stdout)
    assert payload["items"][0]["target"] == "cat1"

    list_result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "batch", "list"))
    assert len(json.loads(list_result.stdout)["batches"]) == 1


def test_batch_remove_requires_yes_non_interactively(tmp_root):
    add_result = runner.invoke(
        app,
        _root_args(tmp_root, "--output", "json", "batch", "add", "--name", "b1", "--type", "esa"),
    )
    batch_id = json.loads(add_result.stdout)["id"]
    result = runner.invoke(app, _root_args(tmp_root, "batch", "remove", batch_id))
    assert result.exit_code != 0

    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "--yes", "batch", "remove", batch_id)
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["removed"] is True


def test_batch_run_submits_and_completes_job(tmp_root):
    # オフライン完了: 空の web バッチ(ネットワーク不要)。
    added = runner.invoke(
        app,
        _root_args(tmp_root, "--output", "json", "batch", "add", "--name", "b1", "--type", "web"),
    )
    assert added.exit_code == 0, added.output
    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "batch", "run", "b1"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["kind"] == "batch"
    assert payload["state"] == "succeeded"


def test_batch_edit_and_remove_accept_name(tmp_root):
    added = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "batch",
            "add",
            "--name",
            "named",
            "--type",
            "esa",
        ),
    )
    assert added.exit_code == 0, added.output

    edited = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "batch",
            "edit",
            "named",
            "--output-dir",
            "docs/named",
        ),
    )
    assert edited.exit_code == 0, edited.output
    assert json.loads(edited.stdout)["output_dir"] == "docs/named"

    removed = runner.invoke(
        app,
        _root_args(tmp_root, "--output", "json", "--yes", "batch", "remove", "named"),
    )
    assert removed.exit_code == 0, removed.output
    assert json.loads(removed.stdout)["removed"] is True


def test_batch_import_from_old_config_does_not_modify_source_file(tmp_root, tmp_path: Path):
    config_path = tmp_path / "batch-config.js"
    config_path.write_text(_SAMPLE_BATCH_CONFIG_JS, encoding="utf-8")

    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "batch", "import", str(config_path))
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["imported"] == 1
    assert config_path.read_text(encoding="utf-8") == _SAMPLE_BATCH_CONFIG_JS

    list_result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "batch", "list"))
    names = {b["name"] for b in json.loads(list_result.stdout)["batches"]}
    assert names == {"esaBatch"}
