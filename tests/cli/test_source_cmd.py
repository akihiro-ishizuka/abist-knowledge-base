"""`source` CLI コマンド群の受入テスト。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

runner = CliRunner()


def _root_args(tmp_root, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


def test_source_add_and_list(tmp_root):
    result = runner.invoke(
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
            "esa本体",
            "--output-dir",
            "docs/esa",
            "--connection",
            '{"team": "abist"}',
        ),
    )
    assert result.exit_code == 0, result.output
    created = json.loads(result.stdout)
    assert created["display_name"] == "esa本体"

    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "source", "list"))
    payload = json.loads(result.stdout)
    assert len(payload["sources"]) == 1


def test_source_edit_updates_display_name(tmp_root):
    add_result = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "source",
            "add",
            "--type",
            "web",
            "--display-name",
            "Old",
            "--output-dir",
            "docs/x",
        ),
    )
    source_id = json.loads(add_result.stdout)["id"]
    result = runner.invoke(
        app,
        _root_args(
            tmp_root, "--output", "json", "source", "edit", source_id, "--display-name", "New"
        ),
    )
    assert json.loads(result.stdout)["display_name"] == "New"


def test_source_remove_without_yes_is_rejected_non_interactively(tmp_root):
    add_result = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "source",
            "add",
            "--type",
            "web",
            "--display-name",
            "X",
            "--output-dir",
            "docs/x",
        ),
    )
    source_id = json.loads(add_result.stdout)["id"]
    result = runner.invoke(app, _root_args(tmp_root, "source", "remove", source_id))
    assert result.exit_code != 0


def test_source_remove_with_yes_deletes(tmp_root):
    add_result = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "source",
            "add",
            "--type",
            "web",
            "--display-name",
            "X",
            "--output-dir",
            "docs/x",
        ),
    )
    source_id = json.loads(add_result.stdout)["id"]
    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "--yes", "source", "remove", source_id)
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["removed"] is True


def test_source_test_connection_reports_ok_when_required_keys_present(tmp_root):
    add_result = runner.invoke(
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
            "esa",
            "--output-dir",
            "docs/esa",
            "--connection",
            '{"team": "abist", "access_token": "secret"}',
        ),
    )
    source_id = json.loads(add_result.stdout)["id"]
    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "source", "test", source_id)
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
