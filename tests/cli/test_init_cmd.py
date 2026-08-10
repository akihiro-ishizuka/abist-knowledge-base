"""`abist-kb init` の受入テスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from abist_kb import identity
from abist_kb.domain.errors import ErrorCode, ExitCode
from abist_kb.presentation.cli.app import app

runner = CliRunner()


def _root_args(tmp_root: Path, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


def test_init_scaffolds_workspace(tmp_root: Path) -> None:
    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "init"))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["app_db_initialized"] is True
    assert payload["root_dir"] == str(tmp_root)
    assert (tmp_root / "docs" / "README.md").is_file()
    assert (tmp_root / "config" / "settings.toml").is_file()
    assert (tmp_root / ".env.example").is_file()
    assert (tmp_root / "data" / "app.sqlite").is_file()


def test_init_is_idempotent(tmp_root: Path) -> None:
    first = runner.invoke(app, _root_args(tmp_root, "init"))
    assert first.exit_code == 0, first.output
    settings_bytes = (tmp_root / "config" / "settings.toml").read_bytes()

    second = runner.invoke(app, _root_args(tmp_root, "--output", "json", "init"))

    assert second.exit_code == 0, second.output
    payload = json.loads(second.stdout)
    assert payload["created_dirs"] == []
    assert payload["created_files"] == []
    assert payload["app_db_initialized"] is False
    assert (tmp_root / "config" / "settings.toml").read_bytes() == settings_bytes


def test_init_reports_conflicting_path_as_invalid_input(tmp_root: Path) -> None:
    (tmp_root / "config" / "settings.toml").mkdir(parents=True)

    result = runner.invoke(app, _root_args(tmp_root, "init"))

    assert result.exit_code == int(ExitCode.INVALID_INPUT)
    assert ErrorCode.INVALID_INPUT.value in result.output


def test_init_shows_next_steps_in_human_output(tmp_root: Path) -> None:
    result = runner.invoke(app, _root_args(tmp_root, "init"))

    assert result.exit_code == 0, result.output
    assert "document register-disk" in result.output
    assert "index build" in result.output
    assert str(tmp_root / "docs") in result.output


def test_next_steps_point_at_the_configured_docs_dir(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """カスタム `docs_dir` で `docs/` と案内しない(索引されない場所へ置かせないため)。"""
    docs_dir = tmp_root / "別のドキュメント"
    monkeypatch.setenv(identity.env_var("docs_dir"), str(docs_dir))

    result = runner.invoke(app, _root_args(tmp_root, "init"))

    assert result.exit_code == 0, result.output
    assert str(docs_dir) in result.output
    assert str(docs_dir / "knowledge" / "B32doc") in result.output
    assert str(tmp_root / "docs") not in result.output
