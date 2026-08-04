"""`application.audit.parallel_compare`(M9 task 9.2: 並行稼働比較ハーネス)。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from abist_kb.application.audit.parallel_compare import (
    ParallelCompareReport,
    PytestSectionResult,
    run_parallel_compare,
)


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_parallel_compare_reports_passed_sections(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, cwd, capture_output, text, check):  # noqa: ANN001
        calls.append(cmd)
        return _FakeCompletedProcess(0, "1 passed in 0.01s\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    report = run_parallel_compare(repo_root=tmp_path)

    assert isinstance(report, ParallelCompareReport)
    assert report.sync_and_collection.status == "passed"
    assert report.mcp_contract.status == "passed"
    assert report.search_quality["status"] == "skipped"
    assert report.all_ok() is True
    assert len(calls) == 2
    assert "test_e2e_byte_identity.py" in calls[0][-1]
    assert "test_all_server_tools_list_diff.py" in calls[1][-1]


def test_run_parallel_compare_detects_skip_when_old_repo_absent(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_run(cmd, cwd, capture_output, text, check):  # noqa: ANN001
        if "test_e2e_byte_identity.py" in cmd[-1]:
            return _FakeCompletedProcess(0, "1 skipped in 0.01s\n")
        return _FakeCompletedProcess(0, "1 passed in 0.01s\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    report = run_parallel_compare(repo_root=tmp_path)

    assert report.sync_and_collection.status == "skipped"
    assert report.mcp_contract.status == "passed"
    assert report.all_ok() is True


def test_run_parallel_compare_surfaces_failure(monkeypatch, tmp_path: Path) -> None:
    def fake_run(cmd, cwd, capture_output, text, check):  # noqa: ANN001
        if "test_e2e_byte_identity.py" in cmd[-1]:
            return _FakeCompletedProcess(1, "1 failed in 0.01s\n", stderr="AssertionError")
        return _FakeCompletedProcess(0, "1 passed in 0.01s\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    report = run_parallel_compare(repo_root=tmp_path)

    assert report.sync_and_collection.status == "failed"
    assert report.all_ok() is False


def test_pytest_section_result_as_dict_roundtrips() -> None:
    section = PytestSectionResult(name="x", status="passed", detail="ok")
    assert section.as_dict() == {"name": "x", "status": "passed", "detail": "ok"}
