"""`application.workspace_init`(`abist-kb init` の scaffold)。

固定文字列の `docs/` 等ではなく **`Settings` が解決したパスにだけ** 生成すること、
そして再実行が既存のファイル・DB行を1バイトも変えないことを検査する。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from abist_kb import identity
from abist_kb.application.workspace_init import init_workspace
from abist_kb.config import Settings, load_settings
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema

TEMPLATE_RELATIVE_PATHS = (
    Path("config") / "settings.toml",
    Path(".env.example"),
    Path("docs") / "README.md",
)


def test_init_creates_directories_templates_and_database(tmp_root: Path) -> None:
    result = init_workspace(load_settings(root=tmp_root))

    assert (tmp_root / "docs").is_dir()
    assert (tmp_root / "docs" / "knowledge" / "B32doc").is_dir()
    assert (tmp_root / "reports").is_dir()
    assert (tmp_root / "data").is_dir()
    assert (tmp_root / "data" / "cache").is_dir()
    assert (tmp_root / "config").is_dir()
    for relative in TEMPLATE_RELATIVE_PATHS:
        assert (tmp_root / relative).is_file(), relative
    assert (tmp_root / "data" / "app.sqlite").is_file()
    assert result["app_db_initialized"] is True


def test_init_writes_no_secrets_into_settings_toml(tmp_root: Path) -> None:
    init_workspace(load_settings(root=tmp_root))
    settings_toml = (tmp_root / "config" / "settings.toml").read_text(encoding="utf-8")
    for secret in ("esa_access_token", "openai_api_key", "git_token"):
        assert secret not in settings_toml


def test_env_example_lists_official_variable_names_commented_out(tmp_root: Path) -> None:
    init_workspace(load_settings(root=tmp_root))
    lines = (tmp_root / ".env.example").read_text(encoding="utf-8").splitlines()

    assert all(line.startswith("#") or not line for line in lines)
    for field_name in ("esa_access_token", "openai_api_key", "git_token"):
        assert f"# {identity.env_var(field_name)}=" in lines


def test_generated_settings_toml_can_be_reloaded(tmp_root: Path) -> None:
    init_workspace(load_settings(root=tmp_root))

    reloaded = load_settings(root=tmp_root)
    assert reloaded.log_level == "INFO"
    assert reloaded.missing_threshold == 3
    assert reloaded.embedding_model == "intfloat/multilingual-e5-small"
    assert reloaded.chat_model == "gpt-4o-mini"


def test_init_uses_settings_paths_not_hardcoded_defaults(tmp_root: Path) -> None:
    """既定からずらしたパスでも Settings 基準の場所にだけ生成する。"""
    settings = Settings(
        root_dir=tmp_root,
        docs_dir=tmp_root / "別のドキュメント",
        config_file=tmp_root / "設定" / "custom.toml",
        app_db_path=tmp_root / "db" / "custom.sqlite",
    )

    init_workspace(settings)

    assert (tmp_root / "別のドキュメント" / "README.md").is_file()
    assert (tmp_root / "別のドキュメント" / "knowledge" / "B32doc").is_dir()
    assert (tmp_root / "設定" / "custom.toml").is_file()
    assert (tmp_root / "db" / "custom.sqlite").is_file()

    assert not (tmp_root / "docs").exists()
    assert not (tmp_root / "config").exists()
    assert not (tmp_root / "data" / "app.sqlite").exists()


def test_rerun_leaves_existing_templates_byte_identical(tmp_root: Path) -> None:
    for relative in TEMPLATE_RELATIVE_PATHS:
        target = tmp_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # `#` 始まりにしておくのは settings.toml が TOML として解析可能である
        # 必要があるため(`load_settings` がこの後で読む)。内容は雛形と別物。
        target.write_bytes(f"# 固有の内容: {relative.as_posix()}\n".encode())
    before = {relative: (tmp_root / relative).read_bytes() for relative in TEMPLATE_RELATIVE_PATHS}

    result = init_workspace(load_settings(root=tmp_root))

    for relative in TEMPLATE_RELATIVE_PATHS:
        assert (tmp_root / relative).read_bytes() == before[relative], relative
    assert result["created_files"] == []
    assert len(result["skipped_files"]) == len(TEMPLATE_RELATIVE_PATHS)


def test_rerun_preserves_existing_database_rows(tmp_root: Path) -> None:
    app_db_path = tmp_root / "data" / "app.sqlite"
    app_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(app_db_path)
    try:
        ensure_app_schema(conn)
        DocumentRepository(conn).upsert({"path": "notes/keep.md", "title": "残る"})
    finally:
        conn.close()

    result = init_workspace(load_settings(root=tmp_root))

    assert result["app_db_initialized"] is False
    conn = connect(app_db_path)
    try:
        assert DocumentRepository(conn).get("notes/keep.md")["title"] == "残る"
    finally:
        conn.close()


def test_rerun_adds_only_missing_entries(tmp_root: Path) -> None:
    init_workspace(load_settings(root=tmp_root))
    shutil.rmtree(tmp_root / "docs")

    result = init_workspace(load_settings(root=tmp_root))

    assert str(tmp_root / "docs") in result["created_dirs"]
    assert str(tmp_root / "docs" / "knowledge" / "B32doc") in result["created_dirs"]
    assert result["created_files"] == [str(tmp_root / "docs" / "README.md")]
    assert str(tmp_root / "reports") in result["skipped_dirs"]
    assert str(tmp_root / "config" / "settings.toml") in result["skipped_files"]


def test_directory_where_a_template_file_belongs_is_an_error(tmp_root: Path) -> None:
    (tmp_root / "config" / "settings.toml").mkdir(parents=True)

    with pytest.raises(AppError) as excinfo:
        init_workspace(load_settings(root=tmp_root))

    assert excinfo.value.code is ErrorCode.INVALID_INPUT
    # 対象パスの衝突は書き込みを始める前に検出するため、この経路では何も作られない。
    assert not (tmp_root / "docs").exists()
    assert (tmp_root / "config" / "settings.toml").is_dir()


def test_file_where_a_directory_belongs_is_an_error(tmp_root: Path) -> None:
    (tmp_root / "docs").write_text("これはファイル\n", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        init_workspace(load_settings(root=tmp_root))

    assert excinfo.value.code is ErrorCode.INVALID_INPUT
    assert (tmp_root / "docs").read_text(encoding="utf-8") == "これはファイル\n"
    assert not (tmp_root / "reports").exists()


def test_result_is_json_serializable_and_stably_sorted(tmp_root: Path) -> None:
    result = init_workspace(load_settings(root=tmp_root))

    assert json.loads(json.dumps(result)) == result
    for key in ("created_dirs", "skipped_dirs", "created_files", "skipped_files"):
        assert result[key] == sorted(result[key]), key
