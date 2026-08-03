import json

from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

runner = CliRunner()


def test_doctor_succeeds_on_this_machine(tmp_root):
    result = runner.invoke(app, ["--root", str(tmp_root), "doctor"])
    assert result.exit_code == 0, result.output


def test_doctor_reports_the_required_checks(tmp_root):
    result = runner.invoke(app, ["--root", str(tmp_root), "--output", "json", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    names = {c["name"] for c in payload["checks"]}
    assert {"python", "sqlite", "fts5", "unicode61", "trigram", "data_dir"} <= names
    assert payload["ok"] is True


def test_doctor_json_reports_actual_sqlite_version(tmp_root):
    import sqlite3

    result = runner.invoke(app, ["--root", str(tmp_root), "--output", "json", "doctor"])
    payload = json.loads(result.stdout)
    sqlite_check = next(c for c in payload["checks"] if c["name"] == "sqlite")
    assert sqlite_check["detail"].startswith(sqlite3.sqlite_version)
    assert sqlite_check["status"] == "ok"


def test_doctor_plain_output_has_no_ansi(tmp_root):
    result = runner.invoke(app, ["--root", str(tmp_root), "--output", "plain", "doctor"])
    assert "\x1b[" not in result.output


def test_doctor_fails_with_config_exit_code_when_trigram_unavailable(tmp_root, monkeypatch):
    from abist_kb.infrastructure.db.connection import CapabilityReport
    from abist_kb.presentation.cli import doctor_cmd

    broken = CapabilityReport(
        sqlite_version="3.30.0",
        fts5=True,
        unicode61=True,
        trigram=False,
        problems=("trigram トークナイザを作成できません",),
        ok=False,
    )
    monkeypatch.setattr(doctor_cmd, "check_sqlite_capabilities", lambda: broken)
    result = runner.invoke(app, ["--root", str(tmp_root), "doctor"])
    assert result.exit_code == 3
    assert "FTS5_TRIGRAM_UNAVAILABLE" in result.output
