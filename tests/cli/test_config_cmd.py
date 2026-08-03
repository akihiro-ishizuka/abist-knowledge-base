import json

from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

runner = CliRunner()


def test_config_path_prints_the_settings_file_path(tmp_root):
    result = runner.invoke(app, ["--root", str(tmp_root), "config", "path"])
    assert result.exit_code == 0
    assert "settings.toml" in result.output


def test_config_show_masks_secrets(tmp_root, monkeypatch):
    monkeypatch.setenv("ABIST_KB_ESA_ACCESS_TOKEN", "top-secret-value")
    result = runner.invoke(app, ["--root", str(tmp_root), "--output", "json", "config", "show"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["esa_access_token"] == "***"
    assert "top-secret-value" not in result.output


def test_config_validate_succeeds_on_defaults(tmp_root):
    result = runner.invoke(app, ["--root", str(tmp_root), "config", "validate"])
    assert result.exit_code == 0


def test_config_validate_fails_with_config_exit_code_on_bad_toml(tmp_root):
    cfg = tmp_root / "config" / "settings.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("missing_threshold = -1\n", encoding="utf-8")
    result = runner.invoke(app, ["--root", str(tmp_root), "config", "validate"])
    assert result.exit_code == 3
