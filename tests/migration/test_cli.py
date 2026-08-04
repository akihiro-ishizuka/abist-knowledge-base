"""`migrate` CLI サブコマンドの受入テスト。"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

runner = CliRunner()


def _root_args(tmp_root: Path, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


def test_migrate_inspect_json(old_repo: Path, tmp_root: Path) -> None:
    result = runner.invoke(
        app,
        _root_args(tmp_root, "--output", "json", "migrate", "inspect", "--from", str(old_repo)),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["markdown_count"] == 5
    assert payload["orphan_count"] == 3


def test_migrate_plan_writes_file(old_repo: Path, tmp_root: Path, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    to_root = tmp_path / "new-repo"
    result = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "migrate",
            "plan",
            "--from",
            str(old_repo),
            "--to",
            str(to_root),
            "--output",
            str(plan_path),
        ),
    )
    assert result.exit_code == 0, result.output
    assert plan_path.exists()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["summary"]["copy"] == 3
    assert plan["summary"]["exclude"] == 2


def test_migrate_rejects_same_from_and_to(old_repo: Path, tmp_root: Path) -> None:
    result = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "migrate",
            "plan",
            "--from",
            str(old_repo),
            "--to",
            str(old_repo),
        ),
    )
    assert result.exit_code != 0


def test_migrate_run_and_verify_round_trip(old_repo: Path, tmp_root: Path, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    manifest_path = tmp_path / "manifest.json"
    to_root = tmp_path / "new-repo"

    plan_result = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "migrate",
            "plan",
            "--from",
            str(old_repo),
            "--to",
            str(to_root),
            "--output",
            str(plan_path),
        ),
    )
    assert plan_result.exit_code == 0, plan_result.output

    run_result = runner.invoke(
        app,
        _root_args(
            tmp_root,
            "--output",
            "json",
            "migrate",
            "run",
            "--plan",
            str(plan_path),
            "--manifest",
            str(manifest_path),
        ),
    )
    assert run_result.exit_code == 0, run_result.output
    manifest = json.loads(run_result.output)
    assert manifest["steps"]["copy_docs"]["status"] == "completed"

    verify_result = runner.invoke(
        app,
        _root_args(
            tmp_root, "--output", "json", "migrate", "verify", "--manifest", str(manifest_path)
        ),
    )
    # search_quality is intentionally unverified without an injected baseline,
    # so overall verify must fail closed rather than report success.
    assert verify_result.exit_code != 0
    first_line = verify_result.output.splitlines()[0]
    payload = json.loads(first_line)
    assert payload["ok"] is False
    assert any(c["name"] == "markdown_bytes" and c["passed"] for c in payload["conditions"])
