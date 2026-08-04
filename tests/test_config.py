from __future__ import annotations

import re
from pathlib import Path

import pytest

from abist_kb.config import _SECRET_FIELDS, Settings, load_settings
from abist_kb.domain.errors import AppError, ErrorCode

_SECRET_SHAPED_FIELD_NAME_RE = re.compile(r".*(_token|_key|_secret|_password)$", re.IGNORECASE)


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


def test_validation_error_details_are_json_serializable_and_exclude_raw_input(tmp_root: Path):
    """小項目の回帰テスト: `config.py` は `exc.errors(include_url=False)` の
    戻り値をそのまま `details["errors"]` へ入れていたため、pydantic が返す
    `input`/`ctx` キーがそのまま機械可読出力へ漏れていた。TOML はネイティブ型
    (`datetime.date` 等)を持つため、クォート忘れのような単純な入力ミスで
    JSON化できないオブジェクトが `input` に紛れ込み、`--output json` で
    このエラー自体を報告しようとした `json.dumps` がその場で `TypeError` を
    送出してクラッシュしてしまう(エラー報告そのものが失敗するという
    最悪の失敗形態、実測で確認済み)。
    """
    import json

    cfg = tmp_root / "config" / "settings.toml"
    cfg.parent.mkdir(parents=True)
    # クォートを忘れた TOML の日付値。tomllib はこれを文字列ではなく
    # 本物の datetime.date として解析する(ありがちな入力ミス)。
    cfg.write_text("esa_access_token = 2023-01-01\n", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        load_settings(root=tmp_root)

    payload = excinfo.value.to_dict()
    # ここで TypeError が起きないことこそが本テストの主眼。
    json.dumps(payload, ensure_ascii=False)

    for error in payload["details"]["errors"]:
        assert "input" not in error
        assert "ctx" not in error
        assert "url" not in error


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
    monkeypatch.setenv("ABIST_KB_GIT_TOKEN", "ghp-super-secret")
    s = load_settings(root=tmp_root)
    dumped = s.redacted_dict()
    assert dumped["esa_access_token"] == "***"
    assert dumped["openai_api_key"] == "***"
    assert dumped["git_token"] == "***"
    assert "super-secret-token" not in repr(dumped)
    assert "sk-abcdef" not in repr(dumped)
    assert "ghp-super-secret" not in repr(dumped)


def test_secret_fields_guardrail_covers_every_secret_shaped_field_name():
    """デフォード#2の回帰テスト: `_SECRET_FIELDS` はハードコードされたクローズド
    セットなので、将来 `*_token`/`*_key`/`*_secret`/`*_password` という名前の
    フィールドを追加しても、このセットへの登録を忘れると `redacted_dict()` が
    無言でマスクし損なう。フィールド名の形だけから機械的に検査することで、
    今後10マイルストーンにわたってこの見落としを赤いテストへ変える。
    """
    secret_shaped = {
        name for name in Settings.model_fields if _SECRET_SHAPED_FIELD_NAME_RE.match(name)
    }
    missing = secret_shaped - _SECRET_FIELDS
    assert not missing, f"_SECRET_FIELDS に未登録の秘密情報らしきフィールド: {missing}"


def test_settings_is_constructible_directly_for_tests(tmp_root: Path):
    s = Settings(root_dir=tmp_root)
    assert s.docs_dir == tmp_root / "docs"


def test_root_none_uses_root_dir_env_var(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABIST_KB_ROOT_DIR", str(tmp_root))
    s = load_settings(root=None)
    assert s.root_dir == tmp_root
    assert s.docs_dir == tmp_root / "docs"
    assert s.config_file == tmp_root / "config" / "settings.toml"


def test_root_none_reads_toml_from_env_var_root(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_root / "config" / "settings.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('log_level = "WARNING"\n', encoding="utf-8")
    monkeypatch.setenv("ABIST_KB_ROOT_DIR", str(tmp_root))
    s = load_settings(root=None)
    assert s.log_level == "WARNING"


def test_root_none_falls_back_to_cwd(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_root)
    s = load_settings(root=None)
    assert s.root_dir == tmp_root


def test_root_param_overrides_root_dir_env_var(
    tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    other_root = tmp_path / "他のルート"
    other_root.mkdir()
    monkeypatch.setenv("ABIST_KB_ROOT_DIR", str(other_root))
    s = load_settings(root=tmp_root)
    assert s.root_dir == tmp_root


def test_root_dir_env_var_blank_is_treated_as_unset(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """デフォード#3: 空白のみの ABIST_KB_ROOT_DIR は「未設定」として扱う。

    素朴な `if env_root:` は空白文字列でも真になるため、`Path("   ")` という
    使い物にならないルートを組み立ててしまっていた。
    """
    monkeypatch.chdir(tmp_root)
    monkeypatch.setenv("ABIST_KB_ROOT_DIR", "   ")
    s = load_settings(root=None)
    assert s.root_dir == tmp_root


def test_env_file_is_read_from_root_not_from_cwd(
    tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """C2(CRITICAL)の回帰テスト: `.env` は `--root`/`ABIST_KB_ROOT_DIR` で
    指定された実効ルートから読まなければならない。`Settings.model_config` の
    `env_file=".env"` は pydantic-settings がプロセスの CWD を基準に解決するため、
    `root` を明示しても `.env` だけは呼び出し元の CWD から読まれてしまっていた
    (settings.toml は既に修正済みだったが、秘密情報を運ぶ .env 側は未修正だった)。

    2つの独立したナレッジベース(root ディレクトリ)を同じマシンで使う場合、
    `--root` で指定した側の資格情報ではなく、たまたま CWD にあった別プロジェクトの
    `.env` の資格情報で認証してしまう、というのがこの不具合の実害。
    """
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    (cwd_dir / ".env").write_text(
        "ABIST_KB_ESA_TEAM_NAME=from-cwd\nABIST_KB_ESA_ACCESS_TOKEN=WRONG-PROJECT-TOKEN\n",
        encoding="utf-8",
    )
    (tmp_root / ".env").write_text("ABIST_KB_ESA_TEAM_NAME=from-root\n", encoding="utf-8")
    monkeypatch.chdir(cwd_dir)

    s = load_settings(root=tmp_root)
    assert s.root_dir == tmp_root
    assert s.esa_team_name == "from-root"
    assert s.esa_access_token is None


def test_env_file_is_read_from_root_dir_env_var_when_root_param_is_none(
    tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """C2 の派生ケース: `root=None` かつ `ABIST_KB_ROOT_DIR` 経由の場合も同様に、
    `.env` はその実効ルートから読まれなければならない。
    """
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    (cwd_dir / ".env").write_text("ABIST_KB_ESA_TEAM_NAME=from-cwd\n", encoding="utf-8")
    (tmp_root / ".env").write_text("ABIST_KB_ESA_TEAM_NAME=from-root-env-var\n", encoding="utf-8")
    monkeypatch.chdir(cwd_dir)
    monkeypatch.setenv("ABIST_KB_ROOT_DIR", str(tmp_root))

    s = load_settings(root=None)
    assert s.esa_team_name == "from-root-env-var"
