"""`audit parallel-compare`(M9 task 9.2)の CLI smoke test。"""

from __future__ import annotations

import json
import subprocess

from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

runner = CliRunner()


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_parallel_compare_skips_search_quality_without_index(monkeypatch, tmp_root):
    def fake_run(cmd, cwd, capture_output, text, check):  # noqa: ANN001
        return _FakeCompletedProcess(0, "1 passed in 0.01s\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.invoke(
        app,
        ["--root", str(tmp_root), "--output", "json", "audit", "parallel-compare"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["sync_and_collection"]["status"] == "passed"
    assert payload["mcp_contract"]["status"] == "passed"
    assert payload["search_quality"]["status"] == "skipped"


def test_parallel_compare_plain_output_has_no_ansi(monkeypatch, tmp_root):
    def fake_run(cmd, cwd, capture_output, text, check):  # noqa: ANN001
        return _FakeCompletedProcess(0, "1 passed in 0.01s\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.invoke(
        app,
        ["--root", str(tmp_root), "--output", "plain", "audit", "parallel-compare"],
    )
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output
