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
    assert {
        "python",
        "sqlite",
        "fts5",
        "unicode61",
        "trigram",
        "external_content",
        "bm25",
        "data_dir",
    } <= names
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
        external_content=True,
        bm25=True,
        problems=("trigram トークナイザを作成できません",),
        ok=False,
    )
    monkeypatch.setattr(doctor_cmd, "check_sqlite_capabilities", lambda: broken)
    result = runner.invoke(app, ["--root", str(tmp_root), "doctor"])
    assert result.exit_code == 3
    assert "FTS5_TRIGRAM_UNAVAILABLE" in result.output


def test_doctor_surfaces_the_actual_sqlite_error_reason_in_check_detail(tmp_root, monkeypatch):
    """テスト網羅の抜け: `CapabilityReport.problems`(実測時に握った本物の
    `sqlite3.Error` の説明文)は計算されるだけで、`doctor_cmd.py` はチェックの
    真偽値しか使わず、その理由を一切表示していなかった。ユーザーには
    「FTS5 拡張を利用できません。」という定型文しか見えず、実際に何が
    どう失敗したのか(§13.3 が要求する実測結果)が失われていた。
    """
    from abist_kb.infrastructure.db.connection import CapabilityReport
    from abist_kb.presentation.cli import doctor_cmd

    broken = CapabilityReport(
        sqlite_version="3.50.4",
        fts5=False,
        unicode61=True,
        trigram=True,
        external_content=True,
        bm25=True,
        problems=("FTS5 が利用できません: no such module: fts5(simulated detail)",),
        ok=False,
    )
    monkeypatch.setattr(doctor_cmd, "check_sqlite_capabilities", lambda: broken)
    result = runner.invoke(app, ["--root", str(tmp_root), "--output", "json", "doctor"])
    payload = json.loads(result.stdout)
    fts5_check = next(c for c in payload["checks"] if c["name"] == "fts5")
    assert fts5_check["status"] == "fail"
    assert "no such module: fts5(simulated detail)" in fts5_check["detail"]
