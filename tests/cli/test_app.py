import json

from typer.testing import CliRunner

from abist_kb import identity
from abist_kb.presentation.cli.app import app

runner = CliRunner()


def test_help_shows_the_cli_name_and_display_name():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert identity.DISPLAY_NAME in result.output


def test_global_options_are_declared():
    result = runner.invoke(app, ["--help"])
    for flag in ("--output", "--color", "--quiet", "--verbose", "--debug", "--yes"):
        assert flag in result.output


def test_invalid_output_mode_exits_with_input_error():
    result = runner.invoke(app, ["--output", "fancy", "doctor"])
    assert result.exit_code == 2


def test_unknown_command_exits_with_input_error():
    result = runner.invoke(app, ["nosuchcommand"])
    assert result.exit_code == 2


def test_version_flag_prints_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_json_output_from_a_command_is_parseable(tmp_root):
    result = runner.invoke(app, ["--root", str(tmp_root), "--output", "json", "config", "path"])
    assert result.exit_code == 0
    json.loads(result.stdout)
