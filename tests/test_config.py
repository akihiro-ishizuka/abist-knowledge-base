from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.config import Settings, load_settings
from abist_kb.domain.errors import AppError, ErrorCode


def test_defaults_derive_all_paths_from_root(tmp_root: Path):
    s = load_settings(root=tmp_root)
    assert s.root_dir == tmp_root
    assert s.docs_dir == tmp_root / "docs"
    assert s.reports_dir == tmp_root / "reports"
    assert s.data_dir == tmp_root / "data"
    assert s.app_db_path == tmp_root / "data" / "app.sqlite"
    assert s.work_index_path == tmp_root / "data" / "work-index.sqlite"
    assert s.reference_index_path == tmp_root / "data" / "reference-index.sqlite"
    assert s.cache_dir == tmp_root / "data" / "cache"
    assert s.config_file == tmp_root / "config" / "settings.toml"


def test_defaults_for_non_path_settings(tmp_root: Path):
    s = load_settings(root=tmp_root)
    assert s.log_level == "INFO"
    assert s.embedding_model == "intfloat/multilingual-e5-small"
    assert s.missing_threshold == 3
    assert s.esa_team_name is None
    assert s.esa_access_token is None
    assert s.openai_api_key is None


def test_environment_overrides_use_the_prefix(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABIST_KB_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ABIST_KB_MISSING_THRESHOLD", "5")
    monkeypatch.setenv("ABIST_KB_ESA_TEAM_NAME", "abist")
    s = load_settings(root=tmp_root)
    assert s.log_level == "DEBUG"
    assert s.missing_threshold == 5
    assert s.esa_team_name == "abist"


def test_unprefixed_environment_variables_are_ignored(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s = load_settings(root=tmp_root)
    assert s.log_level == "INFO"


def test_toml_file_overrides_defaults_and_env_wins_over_toml(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = tmp_root / "config" / "settings.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('log_level = "WARNING"\nmissing_threshold = 7\n', encoding="utf-8")

    s = load_settings(root=tmp_root)
    assert s.log_level == "WARNING"
    assert s.missing_threshold == 7

    monkeypatch.setenv("ABIST_KB_LOG_LEVEL", "ERROR")
    s2 = load_settings(root=tmp_root)
    assert s2.log_level == "ERROR"
    assert s2.missing_threshold == 7


def test_malformed_toml_raises_app_error_with_config_code(tmp_root: Path):
    cfg = tmp_root / "config" / "settings.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("log_level = \n", encoding="utf-8")
    with pytest.raises(AppError) as excinfo:
        load_settings(root=tmp_root)
    assert excinfo.value.code == ErrorCode.CONFIG_ERROR


def test_invalid_log_level_raises_app_error(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABIST_KB_LOG_LEVEL", "LOUD")
    with pytest.raises(AppError) as excinfo:
        load_settings(root=tmp_root)
    assert excinfo.value.code == ErrorCode.CONFIG_ERROR


def test_ensure_directories_creates_data_and_reports(tmp_root: Path):
    s = load_settings(root=tmp_root)
    s.ensure_directories()
    assert s.data_dir.is_dir()
    assert s.reports_dir.is_dir()
    assert s.cache_dir.is_dir()


def test_redacted_dict_masks_secrets(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABIST_KB_ESA_ACCESS_TOKEN", "super-secret-token")
    monkeypatch.setenv("ABIST_KB_OPENAI_API_KEY", "sk-abcdef")
    s = load_settings(root=tmp_root)
    dumped = s.redacted_dict()
    assert dumped["esa_access_token"] == "***"
    assert dumped["openai_api_key"] == "***"
    assert "super-secret-token" not in repr(dumped)
    assert "sk-abcdef" not in repr(dumped)


def test_settings_is_constructible_directly_for_tests(tmp_root: Path):
    s = Settings(root_dir=tmp_root)
    assert s.docs_dir == tmp_root / "docs"
