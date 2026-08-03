"""`worker` CLI コマンド群の受入テスト。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

runner = CliRunner()


def _root_args(tmp_root, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


def test_worker_status_reports_no_leader_initially(tmp_root):
    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "worker", "status"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["leader"] is None


def test_worker_run_once_becomes_leader_and_processes_queued_job(tmp_root):
    submit_result = runner.invoke(app, _root_args(tmp_root, "jobs", "submit", "noop", "--detach"))
    assert submit_result.exit_code != 0  # まだワーカーが居ないので WORKER_UNAVAILABLE

    result = runner.invoke(app, _root_args(tmp_root, "worker", "run", "--once"))
    assert result.exit_code == 0, result.output

    status_result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "worker", "status"))
    payload = json.loads(status_result.stdout)
    assert payload["live"] is True


def test_worker_run_once_then_detach_submit_succeeds(tmp_root):
    """`--once` がリーダー権を確立していれば、以降の `--detach` は成功するはず。"""
    runner.invoke(app, _root_args(tmp_root, "worker", "run", "--once"))
    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "jobs", "submit", "noop", "--detach")
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state"] == "queued"
