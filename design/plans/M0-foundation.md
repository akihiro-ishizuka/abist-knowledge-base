# M0 基盤構築 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development でタスク単位に実行する。ステップは `- [ ]` で進捗管理。

**Goal:** ABIST Knowledge Base の Python プロジェクト骨格を作り、以降の全マイルストーンが依存する横断基盤(識別情報・設定・エラー型・Console 出力・SQLite 基盤・ロギング・CLI 骨格・doctor・CI)を動作する状態で確立する。

**Architecture:** `src/abist_kb/` レイヤード配置。`identity.py` に名前系4値を隔離し他モジュールは必ず経由。人間向け出力は `presentation/console` の Presenter 経由のみ(素の `print()` 禁止)。全例外は `AppError` に正規化し終了コードへ写像。SQLite は接続ファクトリ+連番マイグレーションで統一。

**Tech Stack:** Python 3.12(uv 管理)/ uv / Typer + Rich / Pydantic v2 + pydantic-settings / 標準 `sqlite3` / pytest + Hypothesis / ruff

## Context

親計画: `C:\Users\a1199118\.claude\plans\design-system-design-md-push-glowing-tulip.md`(全体ロードマップ M0〜M10)。設計の正は [../system-design.md](../system-design.md)。

M0 は設計書 §14 手順1「新リポジトリ、`pyproject.toml`、共通設定、Richテーマ、エラー型、SQLiteマイグレーション基盤を作る」に対応する。ここで作る Console・AppError・DB 基盤は M1 以降のすべてのタスクが直接利用するため、API を先に固定する必要がある。

### 環境実測(2026-08-03、本 Windows 実機)

| 項目 | 実測値 |
|---|---|
| uv | 0.11.6 |
| CPython(uv 導入済み) | 3.12.13 (`%APPDATA%\uv\python\cpython-3.12-windows-x86_64-none\python.exe`) |
| 同梱 SQLite | **3.50.4**(要件 ≥3.34 を満たす) |
| FTS5 / unicode61 / trigram / bm25 | **すべて利用可**(trigram の2文字 MATCH が0件なのは trigram 仕様どおり。だから2文字和語 LIKE 補助が必要) |
| Node.js | v22.18.0(M1 の fixture 採取に使用) |
| git | 2.55.0 |

→ ロードマップのリスク R2(trigram 不可)は**解消**。`pysqlite3-binary` 代替は不要。ただし doctor による実測チェックは他環境のために実装する。

### PyPI 安定版(2026-08-03 確認、本計画で採用)

Rich 15.0.0 / Typer 0.27.0 / Pydantic 2.13.4 / pydantic-settings 2.14.2 / pytest 9.1.1 / pytest-asyncio 1.4.0 / Hypothesis 6.165.0 / ruff 0.16.1

## Global Constraints(全タスク共通)

- Python 3.12 固定。依存は `pyproject.toml` に互換範囲、`uv.lock` に実版。`uv sync --locked` で再現すること。
- 名前系の値(`abist_kb` / `abist-kb` / `ABIST_KB_` / `ABIST Knowledge Base`)は `src/abist_kb/identity.py` にのみ書く。他モジュールにリテラルで書かない。
- 人間向け出力は Console Presenter 経由のみ。ライブラリコード(`domain/` `application/` `infrastructure/`)で `print()` および `rich.print` を呼ばない。
- `--output plain|json` および非TTY・`NO_COLOR`・`TERM=dumb` では ANSI 制御文字とアニメーションを一切出さない。`json` は stdout に単一 JSON / JSON Lines のみ、診断は stderr。
- 例外は `AppError(code, message, hint, details, retryable)` に正規化。終了コード: 0成功 / 1処理失敗 / 2入力不正 / 3設定不備 / 4外部サービス失敗 / 5競合・部分成功 / 130キャンセル。
- 秘密情報(`*_TOKEN`, `*_KEY`, `Authorization`, `Cookie`, URL 内資格情報)をログ・DB・テストスナップショットへ書かない。
- SQLite 接続は WAL、`foreign_keys=ON`、`busy_timeout=5000`。スキーマ変更は連番マイグレーション+`schema_migrations` 記録。
- Windows 第一対象。パス操作は `pathlib`、日本語パスを含むテストを入れる。ファイル書込は UTF-8 明示。
- TDD: 失敗するテストを先に書き、失敗を確認してから実装。各タスク末尾でコミット。
- コミットメッセージ末尾に `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` を付ける。

## ファイル構成

```
pyproject.toml                                  # プロジェクト定義・依存・ツール設定
uv.lock                                         # 依存実版(Git管理)
src/abist_kb/
  __init__.py
  identity.py                                   # T1: 名前系4値+表示名
  config.py                                     # T1: Settings / パス解決
  domain/
    __init__.py
    errors.py                                   # T1: AppError / ExitCode / ErrorCode
  presentation/
    __init__.py
    console/
      __init__.py
      theme.py                                  # T2: セマンティックトークン・Richテーマ
      output.py                                 # T2: 出力モード解決
      presenter.py                              # T2: Presenter(表・パネル・エラー・JSON)
      progress.py                               # T2: 進捗表示
    cli/
      __init__.py
      app.py                                    # T4: Typerアプリ・共通オプション・例外ハンドラ
      context.py                                # T4: CLIコンテキスト(Presenter等の受け渡し)
      doctor_cmd.py                             # T4: doctor
      config_cmd.py                             # T4: config show|path|validate
  infrastructure/
    __init__.py
    db/
      __init__.py
      connection.py                             # T3: 接続ファクトリ・PRAGMA
      migrations.py                             # T3: 連番マイグレーションランナー
    observability/
      __init__.py
      logging.py                                # T3: ロギング設定・秘密マスク
  application/__init__.py                       # T1: 空パッケージ(以降のM用)
  migration/__init__.py                         # T1: 空パッケージ(M8用)
tests/
  conftest.py                                   # T1
  test_identity.py                              # T1
  test_errors.py                                # T1
  test_config.py                                # T1
  console/test_theme.py                         # T2
  console/test_output.py                        # T2
  console/test_presenter.py                     # T2
  console/test_output_purity.py                 # T2: 出力純度契約テスト
  db/test_connection.py                         # T3
  db/test_migrations.py                         # T3
  observability/test_logging.py                 # T3
  cli/test_app.py                               # T4
  cli/test_doctor.py                            # T4
  cli/test_config_cmd.py                        # T4
.github/workflows/ci.yml                        # T4
```

---

## Task 1: プロジェクト骨格・identity・config・エラー型

**Files:**
- Create: `pyproject.toml`, `src/abist_kb/__init__.py`, `src/abist_kb/identity.py`, `src/abist_kb/config.py`, `src/abist_kb/domain/__init__.py`, `src/abist_kb/domain/errors.py`, `src/abist_kb/application/__init__.py`, `src/abist_kb/migration/__init__.py`, `src/abist_kb/presentation/__init__.py`, `src/abist_kb/infrastructure/__init__.py`
- Test: `tests/conftest.py`, `tests/test_identity.py`, `tests/test_errors.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `abist_kb.identity`: `PACKAGE_NAME: str`, `DISTRIBUTION_NAME: str`, `CLI_NAME: str`, `ENV_PREFIX: str`, `DISPLAY_NAME: str`, `env_var(suffix: str) -> str`
  - `abist_kb.domain.errors`: `ExitCode(IntEnum)`, `ErrorCode(StrEnum)`, `AppError(Exception)` — 属性 `code/message/hint/details/retryable/exit_code`、`AppError.to_dict() -> dict[str, Any]`、`wrap(exc: BaseException, *, code, message, hint=None, exit_code=...) -> AppError`
  - `abist_kb.config`: `Settings(BaseSettings)`(フィールド `root_dir`, `docs_dir`, `reports_dir`, `data_dir`, `config_file`, `app_db_path`, `work_index_path`, `reference_index_path`, `cache_dir`, `log_level`, `esa_team_name`, `esa_access_token`, `openai_api_key`, `embedding_model`, `missing_threshold`)、`load_settings(root: Path | None = None, config_file: Path | None = None) -> Settings`、`Settings.ensure_directories() -> None`

- [ ] **Step 1: `pyproject.toml` を作成**

```toml
[project]
name = "abist-kb"
version = "0.1.0"
description = "ABIST Knowledge Base - multi-source knowledge base with CLI, TUI, Web and MCP interfaces"
readme = "README.md"
requires-python = "==3.12.*"
license = { text = "Proprietary" }

dependencies = [
    "rich>=15.0,<16",
    "typer>=0.27,<0.28",
    "pydantic>=2.13,<3",
    "pydantic-settings>=2.14,<3",
]

[project.scripts]
abist-kb = "abist_kb.presentation.cli.app:main"

[dependency-groups]
dev = [
    "pytest>=9.1,<10",
    "pytest-asyncio>=1.4,<2",
    "hypothesis>=6.165,<7",
    "ruff>=0.16,<0.17",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/abist_kb"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
markers = [
    "slow: 実行時間の長いテスト",
    "requires_node: 旧Nodeシステムを必要とするテスト",
]

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "PTH", "T20"]

[tool.ruff.lint.per-file-ignores]
"src/abist_kb/presentation/console/*" = ["T20"]
"tests/*" = ["T20"]
```

`T20`(`print` 禁止)を全体に効かせ、Console 実装とテストのみ除外する。これが「素の print 排除」の機械的な担保になる。

- [ ] **Step 2: 環境を作り、空パッケージを置いて `uv sync` を通す**

```bash
cd /c/Temp/abist-knowledge-base
mkdir -p src/abist_kb/domain src/abist_kb/application src/abist_kb/migration \
         src/abist_kb/presentation src/abist_kb/infrastructure tests
for d in src/abist_kb src/abist_kb/domain src/abist_kb/application src/abist_kb/migration \
         src/abist_kb/presentation src/abist_kb/infrastructure; do : > "$d/__init__.py"; done
printf '# ABIST Knowledge Base\n\n%s\n' "Python 移植版。設計書は design/system-design.md を参照。" > README.md
uv python pin 3.12
uv sync
```

Run: `uv run python -c "import abist_kb; print('ok')"` → `ok`

- [ ] **Step 3: identity のテストを書く**

`tests/test_identity.py`:

```python
import re

from abist_kb import identity


def test_identity_values_are_fixed():
    assert identity.PACKAGE_NAME == "abist_kb"
    assert identity.DISTRIBUTION_NAME == "abist-kb"
    assert identity.CLI_NAME == "abist-kb"
    assert identity.ENV_PREFIX == "ABIST_KB_"
    assert identity.DISPLAY_NAME == "ABIST Knowledge Base"


def test_env_prefix_shape():
    assert identity.ENV_PREFIX.endswith("_")
    assert re.fullmatch(r"[A-Z][A-Z0-9_]*_", identity.ENV_PREFIX)


def test_env_var_builds_prefixed_name():
    assert identity.env_var("ESA_ACCESS_TOKEN") == "ABIST_KB_ESA_ACCESS_TOKEN"
    assert identity.env_var("log_level") == "ABIST_KB_LOG_LEVEL"
```

- [ ] **Step 4: テストが失敗することを確認**

Run: `uv run pytest tests/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'abist_kb.identity'`

- [ ] **Step 5: `src/abist_kb/identity.py` を実装**

```python
"""製品名・パッケージ名・CLI名・環境変数接頭辞の唯一の定義場所。

設計書 §16 の保留事項をここへ隔離する。名称変更時はこのファイルだけを変更する。
他モジュールはこれらの値をリテラルで書かず、必ず本モジュールを経由すること。
"""

from __future__ import annotations

PACKAGE_NAME = "abist_kb"
DISTRIBUTION_NAME = "abist-kb"
CLI_NAME = "abist-kb"
ENV_PREFIX = "ABIST_KB_"
DISPLAY_NAME = "ABIST Knowledge Base"


def env_var(suffix: str) -> str:
    """設定キー名から環境変数名を組み立てる。"""
    return f"{ENV_PREFIX}{suffix.upper()}"
```

- [ ] **Step 6: テストが通ることを確認**

Run: `uv run pytest tests/test_identity.py -v`
Expected: PASS(3件)

- [ ] **Step 7: エラー型のテストを書く**

`tests/test_errors.py`:

```python
import pytest

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode, wrap


def test_exit_codes_match_design():
    assert ExitCode.SUCCESS == 0
    assert ExitCode.FAILURE == 1
    assert ExitCode.INVALID_INPUT == 2
    assert ExitCode.CONFIG_ERROR == 3
    assert ExitCode.EXTERNAL_SERVICE == 4
    assert ExitCode.CONFLICT == 5
    assert ExitCode.CANCELLED == 130


def test_app_error_is_an_exception_with_message():
    err = AppError(code=ErrorCode.INVALID_INPUT, message="パスが不正です")
    assert isinstance(err, Exception)
    assert str(err) == "パスが不正です"
    assert err.hint is None
    assert err.details == {}
    assert err.retryable is False
    assert err.exit_code == ExitCode.FAILURE


def test_app_error_carries_hint_details_and_exit_code():
    err = AppError(
        code=ErrorCode.FTS5_TRIGRAM_UNAVAILABLE,
        message="trigram トークナイザが利用できません",
        hint="SQLite 3.34.0 以上を含む Python で再実行してください",
        details={"sqlite_version": "3.30.0"},
        retryable=False,
        exit_code=ExitCode.CONFIG_ERROR,
    )
    assert err.exit_code == ExitCode.CONFIG_ERROR
    assert err.details["sqlite_version"] == "3.30.0"


def test_to_dict_is_json_serializable_and_stable():
    err = AppError(
        code=ErrorCode.EXTERNAL_SERVICE,
        message="esa API に接続できません",
        hint="ネットワークとトークンを確認してください",
        details={"status": 503},
        retryable=True,
    )
    assert err.to_dict() == {
        "code": "EXTERNAL_SERVICE",
        "message": "esa API に接続できません",
        "hint": "ネットワークとトークンを確認してください",
        "details": {"status": 503},
        "retryable": True,
    }


def test_wrap_preserves_cause_and_does_not_leak_raw_type_into_message():
    original = ValueError("boom")
    err = wrap(original, code=ErrorCode.FAILURE, message="処理に失敗しました")
    assert isinstance(err, AppError)
    assert err.__cause__ is original
    assert err.message == "処理に失敗しました"
    assert err.details["cause_type"] == "ValueError"


def test_error_code_values_are_screaming_snake_strings():
    for member in ErrorCode:
        assert member.value == member.name
        assert member.value.isupper()


def test_app_error_can_be_raised_and_caught():
    with pytest.raises(AppError) as excinfo:
        raise AppError(code=ErrorCode.CANCELLED, message="中断しました",
                       exit_code=ExitCode.CANCELLED)
    assert excinfo.value.exit_code == ExitCode.CANCELLED
```

- [ ] **Step 8: テストが失敗することを確認**

Run: `uv run pytest tests/test_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'abist_kb.domain.errors'`

- [ ] **Step 9: `src/abist_kb/domain/errors.py` を実装**

```python
"""アプリケーション共通のエラー型と終了コード(設計書 §8)。

外部API例外やSQLite例外をUIへ直接露出させず、必ず AppError へ正規化する。
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    """プロセス終了コード(設計書 §8)。"""

    SUCCESS = 0
    FAILURE = 1
    INVALID_INPUT = 2
    CONFIG_ERROR = 3
    EXTERNAL_SERVICE = 4
    CONFLICT = 5
    CANCELLED = 130


class ErrorCode(StrEnum):
    """安定したエラーコード。UIとMCP応答で同じ値を使う。

    値は名前と一致させる(StrEnum の auto は小文字になるため明示指定)。
    """

    FAILURE = "FAILURE"
    INVALID_INPUT = "INVALID_INPUT"
    CONFIG_ERROR = "CONFIG_ERROR"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    CONFLICT = "CONFLICT"
    CANCELLED = "CANCELLED"
    NOT_FOUND = "NOT_FOUND"
    FTS5_TRIGRAM_UNAVAILABLE = "FTS5_TRIGRAM_UNAVAILABLE"
    SQLITE_TOO_OLD = "SQLITE_TOO_OLD"
    MIGRATION_FAILED = "MIGRATION_FAILED"


_EXIT_CODE_BY_ERROR: dict[ErrorCode, ExitCode] = {
    ErrorCode.INVALID_INPUT: ExitCode.INVALID_INPUT,
    ErrorCode.CONFIG_ERROR: ExitCode.CONFIG_ERROR,
    ErrorCode.EXTERNAL_SERVICE: ExitCode.EXTERNAL_SERVICE,
    ErrorCode.CONFLICT: ExitCode.CONFLICT,
    ErrorCode.CANCELLED: ExitCode.CANCELLED,
}


class AppError(Exception):
    """UIへ提示できる正規化済みエラー。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        exit_code: ExitCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details: dict[str, Any] = dict(details) if details else {}
        self.retryable = retryable
        self.exit_code = exit_code if exit_code is not None else ExitCode.FAILURE

    def to_dict(self) -> dict[str, Any]:
        """`--output json` と MCP 応答で使う辞書表現。"""
        return {
            "code": str(self.code),
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
            "retryable": self.retryable,
        }


def default_exit_code(code: ErrorCode) -> ExitCode:
    """エラーコードに対応する既定の終了コード。"""
    return _EXIT_CODE_BY_ERROR.get(code, ExitCode.FAILURE)


def wrap(
    exc: BaseException,
    *,
    code: ErrorCode,
    message: str,
    hint: str | None = None,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
    exit_code: ExitCode | None = None,
) -> AppError:
    """外部例外を AppError へ包む。元例外は __cause__ と details に保持する。

    例外の生メッセージは message へ混ぜない(利用者向け文言を壊さないため)。
    詳細は --debug 時に details と traceback から辿る。
    """
    merged: dict[str, Any] = dict(details) if details else {}
    merged.setdefault("cause_type", type(exc).__name__)
    merged.setdefault("cause_message", str(exc))
    err = AppError(
        code=code,
        message=message,
        hint=hint,
        details=merged,
        retryable=retryable,
        exit_code=exit_code if exit_code is not None else default_exit_code(code),
    )
    err.__cause__ = exc
    return err
```

- [ ] **Step 10: テストが通ることを確認**

Run: `uv run pytest tests/test_errors.py -v`
Expected: PASS(7件)

- [ ] **Step 11: 設定のテストを書く**

`tests/conftest.py`:

```python
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from abist_kb import identity


@pytest.fixture(autouse=True)
def _clear_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト間で本アプリの環境変数が漏れないようにする。"""
    for key in list(os.environ):
        if key.startswith(identity.ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)


@pytest.fixture
def tmp_root(tmp_path: Path) -> Iterator[Path]:
    """日本語を含む一時ルートディレクトリ(Windows の日本語パス検証を兼ねる)。"""
    root = tmp_path / "作業ルート"
    root.mkdir()
    yield root
```

`tests/test_config.py`:

```python
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
```

- [ ] **Step 12: テストが失敗することを確認**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'abist_kb.config'`

- [ ] **Step 13: `src/abist_kb/config.py` を実装**

パス系は `root_dir` からの派生を既定とし、明示指定があればそれを優先する。`pydantic_settings` の優先順位は「引数 > 環境変数 > .env > TOML > 既定」。

```python
"""設定の読み込みとパス解決(設計書 §9.1)。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from abist_kb import identity
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode, wrap

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_SECRET_FIELDS = frozenset({"esa_access_token", "openai_api_key"})


class Settings(BaseSettings):
    """アプリケーション設定。

    パス系フィールドは未指定なら root_dir から派生する。
    秘密情報は .env / 環境変数からのみ読み、settings.toml へは書かない。
    """

    model_config = SettingsConfigDict(
        env_prefix=identity.ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    root_dir: Path = Field(default_factory=Path.cwd)

    docs_dir: Path | None = None
    reports_dir: Path | None = None
    data_dir: Path | None = None
    config_file: Path | None = None
    app_db_path: Path | None = None
    work_index_path: Path | None = None
    reference_index_path: Path | None = None
    cache_dir: Path | None = None

    log_level: LogLevel = "INFO"
    embedding_model: str = "intfloat/multilingual-e5-small"
    missing_threshold: int = Field(default=3, ge=1)

    esa_team_name: str | None = None
    esa_access_token: str | None = None
    openai_api_key: str | None = None

    @model_validator(mode="after")
    def _derive_paths(self) -> Settings:
        root = self.root_dir.expanduser()
        object.__setattr__(self, "root_dir", root)
        defaults: dict[str, Path] = {
            "docs_dir": root / "docs",
            "reports_dir": root / "reports",
            "data_dir": root / "data",
            "config_file": root / "config" / "settings.toml",
        }
        for name, value in defaults.items():
            if getattr(self, name) is None:
                object.__setattr__(self, name, value)

        data = self.data_dir
        assert data is not None  # 直上で必ず埋まる
        derived_from_data: dict[str, Path] = {
            "app_db_path": data / "app.sqlite",
            "work_index_path": data / "work-index.sqlite",
            "reference_index_path": data / "reference-index.sqlite",
            "cache_dir": data / "cache",
        }
        for name, value in derived_from_data.items():
            if getattr(self, name) is None:
                object.__setattr__(self, name, value)
        return self

    def ensure_directories(self) -> None:
        """書込先ディレクトリを作成する(docs は移行時に作られるため対象外)。"""
        for path in (self.data_dir, self.reports_dir, self.cache_dir):
            if path is not None:
                path.mkdir(parents=True, exist_ok=True)

    def redacted_dict(self) -> dict[str, Any]:
        """秘密情報を伏せた表示用辞書(config show / 診断出力で使う)。"""
        dumped = self.model_dump(mode="json")
        for name in _SECRET_FIELDS:
            if dumped.get(name):
                dumped[name] = "***"
        return dumped


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise wrap(
            exc,
            code=ErrorCode.CONFIG_ERROR,
            message=f"設定ファイルを解析できません: {path}",
            hint="TOML の構文を確認してください。",
            exit_code=ExitCode.CONFIG_ERROR,
        ) from exc
    except OSError as exc:
        raise wrap(
            exc,
            code=ErrorCode.CONFIG_ERROR,
            message=f"設定ファイルを読み込めません: {path}",
            exit_code=ExitCode.CONFIG_ERROR,
        ) from exc


def load_settings(root: Path | None = None, config_file: Path | None = None) -> Settings:
    """設定を読み込む。優先順位は 環境変数 > .env > settings.toml > 既定。"""
    root_dir = (root or Path.cwd()).expanduser()
    toml_path = config_file or (root_dir / "config" / "settings.toml")

    file_values: dict[str, Any] = {}
    if toml_path.is_file():
        file_values = {k: v for k, v in _read_toml(toml_path).items() if v is not None}
    # ファイル値は既定値の置き換えであり、環境変数より弱い。
    # BaseSettings は「引数 > 環境変数」の順なので、ファイル値は引数として渡さず
    # 既定値の上書きとして扱うため、環境変数に存在するキーは除外する。
    import os

    for key in list(file_values):
        if identity.env_var(key) in os.environ:
            del file_values[key]

    overrides: dict[str, Any] = {"root_dir": root_dir, **file_values}
    if config_file is not None:
        overrides["config_file"] = config_file

    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise wrap(
            exc,
            code=ErrorCode.CONFIG_ERROR,
            message="設定値が不正です。",
            hint=f"`{identity.CLI_NAME} config validate` で詳細を確認してください。",
            details={"errors": exc.errors(include_url=False)},
            exit_code=ExitCode.CONFIG_ERROR,
        ) from exc
```

注意: `Settings(**overrides)` に `root_dir` を渡すと環境変数 `ABIST_KB_ROOT_DIR` より優先される。テストは `root=` 明示前提なので問題ないが、CLI からは `root` 未指定で呼び出し環境変数を効かせる。実装時に `root is None` の場合は `root_dir` を overrides に入れないこと。

- [ ] **Step 14: テストが通ることを確認**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS(10件)

- [ ] **Step 15: lint と全テスト**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: lint クリーン、全テスト PASS

- [ ] **Step 16: コミット**

```bash
git add pyproject.toml uv.lock README.md .python-version src tests
git commit -m "$(cat <<'EOF'
feat(m0): プロジェクト骨格・identity・設定・エラー型を追加

- pyproject.toml(Python 3.12 固定、ruff の T20 で print を機械的に禁止)
- identity.py に名前系4値を隔離(設計書 §16)
- AppError / ExitCode / ErrorCode(設計書 §8)
- Settings とパス解決、秘密情報のマスク付きダンプ

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Console サービス(セマンティックトークン・出力モード・Presenter)

**Files:**
- Create: `src/abist_kb/presentation/console/__init__.py`, `theme.py`, `output.py`, `presenter.py`, `progress.py`
- Test: `tests/console/test_theme.py`, `tests/console/test_output.py`, `tests/console/test_presenter.py`, `tests/console/test_output_purity.py`

**Interfaces:**
- Consumes: `abist_kb.domain.errors.AppError`, `abist_kb.identity`
- Produces:
  - `theme.py`: `SemanticToken(StrEnum)`(PRIMARY/SUCCESS/WARNING/DANGER/INFO/MUTED/ACCENT)、`TOKEN_STYLES: dict[SemanticToken, TokenStyle]`(`TokenStyle` は `rich_style: str`, `web_hex: str`, `symbol: str` を持つ frozen dataclass)、`build_theme() -> rich.theme.Theme`
  - `output.py`: `OutputMode(StrEnum)`(RICH/PLAIN/JSON)、`ColorMode(StrEnum)`(AUTO/ALWAYS/NEVER)、`resolve_output_mode(requested: str, *, stream, env: Mapping[str, str]) -> OutputMode`、`resolve_color_system(mode: OutputMode, color: ColorMode, *, stream, env) -> str | None`
  - `presenter.py`: `Presenter` — `__init__(mode, *, stdout=None, stderr=None, color_system=..., width=None, quiet=False, verbose=False, debug=False)`、メソッド `line(text, token=None)`, `success/warning/danger/info/muted(text)`, `table(title, columns, rows)`, `panel(title, body)`, `markdown(text)`, `json_result(payload)`, `error(err: AppError)`, `confirm(prompt, *, assume_yes)`；プロパティ `mode`, `is_json`, `console`, `err_console`
  - `progress.py`: `progress_scope(presenter, *, description, total=None) -> ContextManager[ProgressHandle]`、`ProgressHandle.advance(n=1, *, item=None)`, `.set_total(n)`, `.fail(item)`

**設計上の要点(実装前に把握すること)**
- `OutputMode.RICH` は色・罫線・スピナー可。`PLAIN` は ANSI もアニメーションも一切なし。`JSON` は stdout へ payload のみ、その他すべて stderr。
- `auto` の解決: stdout が TTY → RICH、非TTY → PLAIN。`--output rich` は非TTYでも RICH を選ぶが `NO_COLOR` は尊重(色は落とし、罫線とレイアウトは残す)。`TERM=dumb` は RICH 要求でも PLAIN へ落とす。
- 進捗は `JSON`/`PLAIN`/`quiet` では出さない(`PLAIN` では開始・完了の1行のみ)。
- エラー提示順は固定: エラーコード → 概要 → 原因 → 回復手順 → `--debug` 案内(設計書 §6.2)。`--debug` 時のみ Rich Traceback を stderr へ。

- [ ] **Step 1: テーマのテストを書く**

`tests/console/test_theme.py`:

```python
from rich.theme import Theme

from abist_kb.presentation.console.theme import TOKEN_STYLES, SemanticToken, build_theme


def test_all_seven_tokens_exist():
    assert {t.value for t in SemanticToken} == {
        "primary", "success", "warning", "danger", "info", "muted", "accent",
    }


def test_token_styles_match_design_table():
    expected = {
        SemanticToken.PRIMARY: ("bold cyan", "#0891B2", "●"),
        SemanticToken.SUCCESS: ("bold green", "#15803D", "✓"),
        SemanticToken.WARNING: ("bold yellow", "#B45309", "!"),
        SemanticToken.DANGER: ("bold red", "#B91C1C", "×"),
        SemanticToken.INFO: ("blue", "#1D4ED8", "i"),
        SemanticToken.MUTED: ("dim", "#64748B", "-"),
        SemanticToken.ACCENT: ("magenta", "#A21CAF", "◆"),
    }
    for token, (style, web, symbol) in expected.items():
        assert TOKEN_STYLES[token].rich_style == style
        assert TOKEN_STYLES[token].web_hex == web
        assert TOKEN_STYLES[token].symbol == symbol


def test_every_token_has_a_symbol_so_colour_is_never_the_only_signal():
    for token in SemanticToken:
        assert TOKEN_STYLES[token].symbol
        assert len(TOKEN_STYLES[token].symbol) == 1


def test_build_theme_registers_every_token_name():
    theme = build_theme()
    assert isinstance(theme, Theme)
    for token in SemanticToken:
        assert token.value in theme.styles
```

- [ ] **Step 2: 失敗を確認 → `theme.py` を実装**

Run: `uv run pytest tests/console/test_theme.py -v` → FAIL(モジュール未作成)

```python
"""セマンティックトークン(設計書 §6.1)。

色だけで状態を伝えないため、各トークンは必ず記号を伴う。
Web(NiceGUI)は web_hex、Rich/Textual は rich_style を使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rich.theme import Theme


class SemanticToken(StrEnum):
    PRIMARY = "primary"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"
    INFO = "info"
    MUTED = "muted"
    ACCENT = "accent"


@dataclass(frozen=True, slots=True)
class TokenStyle:
    rich_style: str
    web_hex: str
    symbol: str


TOKEN_STYLES: dict[SemanticToken, TokenStyle] = {
    SemanticToken.PRIMARY: TokenStyle("bold cyan", "#0891B2", "●"),
    SemanticToken.SUCCESS: TokenStyle("bold green", "#15803D", "✓"),
    SemanticToken.WARNING: TokenStyle("bold yellow", "#B45309", "!"),
    SemanticToken.DANGER: TokenStyle("bold red", "#B91C1C", "×"),
    SemanticToken.INFO: TokenStyle("blue", "#1D4ED8", "i"),
    SemanticToken.MUTED: TokenStyle("dim", "#64748B", "-"),
    SemanticToken.ACCENT: TokenStyle("magenta", "#A21CAF", "◆"),
}


def build_theme() -> Theme:
    """Rich Console 用のテーマ。"""
    return Theme({token.value: style.rich_style for token, style in TOKEN_STYLES.items()})
```

Run: `uv run pytest tests/console/test_theme.py -v` → PASS(4件)

- [ ] **Step 3: 出力モード解決のテストを書く**

`tests/console/test_output.py`:

```python
import io

import pytest

from abist_kb.presentation.console.output import (
    ColorMode,
    OutputMode,
    resolve_color_system,
    resolve_output_mode,
)


class FakeStream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("requested", "tty", "env", "expected"),
    [
        ("auto", True, {}, OutputMode.RICH),
        ("auto", False, {}, OutputMode.PLAIN),
        ("rich", False, {}, OutputMode.RICH),
        ("rich", True, {}, OutputMode.RICH),
        ("plain", True, {}, OutputMode.PLAIN),
        ("json", True, {}, OutputMode.JSON),
        ("json", False, {}, OutputMode.JSON),
        ("auto", True, {"TERM": "dumb"}, OutputMode.PLAIN),
        ("rich", True, {"TERM": "dumb"}, OutputMode.PLAIN),
        ("auto", True, {"NO_COLOR": "1"}, OutputMode.RICH),
    ],
)
def test_resolve_output_mode(requested, tty, env, expected):
    assert resolve_output_mode(requested, stream=FakeStream(tty), env=env) == expected


def test_unknown_output_mode_is_rejected():
    with pytest.raises(ValueError):
        resolve_output_mode("fancy", stream=FakeStream(True), env={})


@pytest.mark.parametrize(
    ("mode", "color", "tty", "env", "expected"),
    [
        (OutputMode.RICH, ColorMode.AUTO, True, {}, "auto"),
        (OutputMode.RICH, ColorMode.AUTO, False, {}, None),
        (OutputMode.RICH, ColorMode.AUTO, True, {"NO_COLOR": "1"}, None),
        (OutputMode.RICH, ColorMode.ALWAYS, True, {"NO_COLOR": "1"}, None),
        (OutputMode.RICH, ColorMode.ALWAYS, False, {}, "auto"),
        (OutputMode.RICH, ColorMode.NEVER, True, {}, None),
        (OutputMode.PLAIN, ColorMode.ALWAYS, True, {}, None),
        (OutputMode.JSON, ColorMode.ALWAYS, True, {}, None),
    ],
)
def test_resolve_color_system(mode, color, tty, env, expected):
    assert resolve_color_system(mode, color, stream=FakeStream(tty), env=env) == expected
```

`NO_COLOR` は `--color always` より強い(設計書「`rich` は Rich を強制するが `NO_COLOR` は尊重する」)。`plain`/`json` では常に色なし。

- [ ] **Step 4: 失敗を確認 → `output.py` を実装**

Run: `uv run pytest tests/console/test_output.py -v` → FAIL

```python
"""出力モードと色の解決(設計書 §6.3)。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from typing import IO, Any


class OutputMode(StrEnum):
    RICH = "rich"
    PLAIN = "plain"
    JSON = "json"


class ColorMode(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


_VALID_REQUESTS = frozenset({"auto", "rich", "plain", "json"})


def _is_tty(stream: IO[Any] | None) -> bool:
    if stream is None:
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _is_dumb_terminal(env: Mapping[str, str]) -> bool:
    return env.get("TERM", "").strip().lower() == "dumb"


def _no_color(env: Mapping[str, str]) -> bool:
    # NO_COLOR 仕様: 値の内容によらず、設定されていれば色を出さない。
    return "NO_COLOR" in env


def resolve_output_mode(
    requested: str,
    *,
    stream: IO[Any] | None,
    env: Mapping[str, str] | None = None,
) -> OutputMode:
    """`--output` の要求値と実行環境から実際の出力モードを決める。"""
    env = os.environ if env is None else env
    value = requested.strip().lower()
    if value not in _VALID_REQUESTS:
        raise ValueError(f"未知の出力モード: {requested!r}(有効値: auto, rich, plain, json)")

    if value == "json":
        return OutputMode.JSON
    if value == "plain":
        return OutputMode.PLAIN
    if _is_dumb_terminal(env):
        return OutputMode.PLAIN
    if value == "rich":
        return OutputMode.RICH
    return OutputMode.RICH if _is_tty(stream) else OutputMode.PLAIN


def resolve_color_system(
    mode: OutputMode,
    color: ColorMode,
    *,
    stream: IO[Any] | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Rich Console へ渡す color_system。None は色なしを意味する。"""
    env = os.environ if env is None else env
    if mode is not OutputMode.RICH:
        return None
    if _no_color(env) or color is ColorMode.NEVER:
        return None
    if color is ColorMode.ALWAYS:
        return "auto"
    return "auto" if _is_tty(stream) else None
```

Run: `uv run pytest tests/console/test_output.py -v` → PASS(19件)

- [ ] **Step 5: Presenter のテストを書く**

`tests/console/test_presenter.py`:

```python
import json

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.presenter import Presenter
from abist_kb.presentation.console.theme import SemanticToken


def make(mode: OutputMode, **kwargs) -> Presenter:
    return Presenter(mode, width=80, **kwargs)


def test_success_line_carries_symbol_not_only_colour():
    p = make(OutputMode.PLAIN)
    p.success("同期が完了しました")
    out = p.stdout_value()
    assert "✓" in out
    assert "同期が完了しました" in out


def test_plain_mode_emits_no_ansi():
    p = make(OutputMode.PLAIN)
    p.success("完了")
    p.warning("注意")
    p.danger("失敗")
    p.table("文書", ["パス", "状態"], [["docs/a.md", "synced"]])
    assert "\x1b[" not in p.stdout_value()


def test_table_renders_all_cells_in_plain_mode():
    p = make(OutputMode.PLAIN)
    p.table("文書一覧", ["パス", "状態"], [["docs/蛇腹/a.md", "synced"], ["docs/b.md", "conflict"]])
    out = p.stdout_value()
    for fragment in ("文書一覧", "パス", "状態", "docs/蛇腹/a.md", "synced", "conflict"):
        assert fragment in out


def test_json_mode_writes_only_payload_to_stdout():
    p = make(OutputMode.JSON)
    p.success("これは stdout に出てはいけない")
    p.info("これも")
    p.json_result({"ok": True, "count": 2})
    assert json.loads(p.stdout_value()) == {"ok": True, "count": 2}


def test_json_mode_routes_human_messages_to_stderr():
    p = make(OutputMode.JSON)
    p.warning("索引が古い可能性があります")
    assert "索引が古い可能性があります" in p.stderr_value()


def test_json_result_is_utf8_not_escaped():
    p = make(OutputMode.JSON)
    p.json_result({"title": "蛇腹形状"})
    assert "蛇腹形状" in p.stdout_value()


def test_error_presentation_order_is_code_summary_cause_recovery():
    p = make(OutputMode.PLAIN)
    err = AppError(
        code=ErrorCode.FTS5_TRIGRAM_UNAVAILABLE,
        message="trigram トークナイザが利用できません",
        hint="SQLite 3.34.0 以上の Python で再実行してください",
        details={"sqlite_version": "3.30.0"},
        exit_code=ExitCode.CONFIG_ERROR,
    )
    p.error(err)
    out = p.stderr_value()
    i_code = out.index("FTS5_TRIGRAM_UNAVAILABLE")
    i_msg = out.index("trigram トークナイザが利用できません")
    i_cause = out.index("sqlite_version")
    i_hint = out.index("SQLite 3.34.0 以上")
    assert i_code < i_msg < i_cause < i_hint
    assert "--debug" in out


def test_error_goes_to_stderr_never_stdout():
    p = make(OutputMode.PLAIN)
    p.error(AppError(code=ErrorCode.FAILURE, message="失敗"))
    assert "失敗" not in p.stdout_value()
    assert "失敗" in p.stderr_value()


def test_json_mode_error_is_machine_readable_on_stderr():
    p = make(OutputMode.JSON)
    p.error(AppError(code=ErrorCode.NOT_FOUND, message="文書が見つかりません"))
    payload = json.loads(p.stderr_value())
    assert payload["code"] == "NOT_FOUND"
    assert payload["message"] == "文書が見つかりません"
    assert p.stdout_value() == ""


def test_quiet_suppresses_info_and_success_but_not_errors():
    p = make(OutputMode.PLAIN, quiet=True)
    p.success("完了")
    p.info("補足")
    p.danger("失敗")
    out = p.stdout_value()
    assert "完了" not in out
    assert "補足" not in out
    assert "失敗" in out


def test_confirm_returns_true_without_prompting_when_assume_yes():
    p = make(OutputMode.PLAIN)
    assert p.confirm("削除しますか", assume_yes=True) is True


def test_confirm_raises_when_non_interactive_and_not_assumed():
    p = make(OutputMode.JSON)
    err = None
    try:
        p.confirm("削除しますか", assume_yes=False)
    except AppError as exc:
        err = exc
    assert err is not None
    assert err.code == ErrorCode.INVALID_INPUT
    assert "--yes" in (err.hint or "")


def test_token_helper_prefixes_symbol():
    p = make(OutputMode.PLAIN)
    p.line("進行中", token=SemanticToken.INFO)
    assert "i" in p.stdout_value()
    assert "進行中" in p.stdout_value()
```

- [ ] **Step 6: 失敗を確認 → `presenter.py` を実装**

Run: `uv run pytest tests/console/test_presenter.py -v` → FAIL

要件:
- コンストラクタで stdout/stderr 用に `rich.console.Console` を2つ作る。テストから内容を取れるよう、`stdout`/`stderr` が None のときは内部で `io.StringIO` を使い、`stdout_value()`/`stderr_value()` で取得できるようにする。
- `PLAIN`/`JSON` では `color_system=None`、`no_color=True`、`highlight=False`、`soft_wrap=True`、`safe_box=True`。アニメーションは使わない。
- `JSON` モードでは `success/info/warning/danger/line/table/panel/markdown` の出力先を stderr にする。`json_result(payload)` は `json.dumps(payload, ensure_ascii=False)` を stdout へ1行(または `indent=2`)で書き、末尾に改行。
- `error(err)` は `JSON` モードなら `err.to_dict()` を stderr へ JSON で、それ以外は「`× <CODE>`」「概要」「原因(details があれば key: value 列)」「回復手順(hint)」「`--debug` を付けると詳細を表示します」の順で stderr へ。`debug=True` のときは加えて `rich.traceback` を stderr へ出す(`err.__cause__` がある場合)。
- `quiet=True` は `success/info/line/table/panel/markdown` を抑止するが `warning/danger/error/json_result` は抑止しない。
- `confirm(prompt, assume_yes)` は `assume_yes` なら即 True。対話不可(`mode is JSON` または stdin が非TTY)なら `AppError(ErrorCode.INVALID_INPUT, hint="非対話実行では --yes を指定してください")` を送出。対話可能なら `rich.prompt.Confirm.ask`。

Run: `uv run pytest tests/console/test_presenter.py -v` → PASS(13件)

- [ ] **Step 7: 出力純度の契約テストを書く**

`tests/console/test_output_purity.py`:

```python
import json
import re

import pytest

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.presenter import Presenter
from abist_kb.presentation.console.progress import progress_scope

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def exercise(p: Presenter) -> None:
    """Presenter の人間向けAPIを一通り叩く。"""
    p.line("行")
    p.success("成功")
    p.warning("警告")
    p.danger("失敗")
    p.info("情報")
    p.muted("補助")
    p.table("表", ["列A", "列B"], [["値1", "値2"]])
    p.panel("見出し", "本文")
    p.markdown("# 見出し\n\n本文\n")
    with progress_scope(p, description="処理中", total=3) as handle:
        handle.advance(item="docs/a.md")
        handle.advance(item="docs/b.md")
        handle.advance(item="docs/c.md")
    p.error(AppError(code=ErrorCode.FAILURE, message="エラー"))


@pytest.mark.parametrize("mode", [OutputMode.PLAIN, OutputMode.JSON])
def test_no_ansi_anywhere_in_non_rich_modes(mode):
    p = Presenter(mode, width=80)
    exercise(p)
    assert not ANSI.search(p.stdout_value())
    assert not ANSI.search(p.stderr_value())


def test_json_mode_stdout_stays_empty_until_a_result_is_emitted():
    p = Presenter(OutputMode.JSON, width=80)
    exercise(p)
    assert p.stdout_value() == ""


def test_json_mode_stdout_is_exactly_one_json_document():
    p = Presenter(OutputMode.JSON, width=80)
    exercise(p)
    p.json_result({"ok": True})
    assert json.loads(p.stdout_value()) == {"ok": True}


def test_plain_mode_progress_emits_no_animation_frames():
    p = Presenter(OutputMode.PLAIN, width=80)
    with progress_scope(p, description="索引作成", total=2) as handle:
        handle.advance()
        handle.advance()
    out = p.stdout_value()
    assert "\r" not in out
    assert not ANSI.search(out)
    assert "索引作成" in out


def test_quiet_plain_mode_emits_nothing_for_progress():
    p = Presenter(OutputMode.PLAIN, width=80, quiet=True)
    with progress_scope(p, description="索引作成", total=2) as handle:
        handle.advance()
    assert p.stdout_value() == ""


def test_rich_mode_does_produce_ansi_when_colour_enabled():
    """対照テスト: RICH では色が出ること(純度テストが常に真にならない担保)。"""
    p = Presenter(OutputMode.RICH, width=80, color_system="truecolor", force_terminal=True)
    p.success("成功")
    assert ANSI.search(p.stdout_value())
```

- [ ] **Step 8: 失敗を確認 → `progress.py` を実装**

Run: `uv run pytest tests/console/test_output_purity.py -v` → FAIL(`progress` 未作成)

要件:
- `progress_scope(presenter, *, description, total=None)` はコンテキストマネージャ。
- `RICH` かつ `quiet=False`: `rich.progress.Progress` で「説明・進捗バー・完了数/総数・経過・残り」を表示(`total=None` なら Spinner)。終了時に静的な最終行へ置換。
- `PLAIN` かつ `quiet=False`: 開始時に `description` の1行、終了時に「完了 N件 / 失敗 M件 / 経過 X秒」の1行のみ。`\r` を使わない。
- `JSON` または `quiet=True`: 何も出さない。
- `ProgressHandle.advance(n=1, *, item=None)` / `.set_total(n)` / `.fail(item)` を持ち、内部で完了数・失敗数を数える。

Run: `uv run pytest tests/console/test_output_purity.py -v` → PASS(7件)

- [ ] **Step 9: 全テストと lint**

Run: `uv run ruff check . && uv run pytest -q`
Expected: 全 PASS

- [ ] **Step 10: コミット**

```bash
git add src/abist_kb/presentation/console tests/console
git commit -m "$(cat <<'EOF'
feat(m0): Console サービス(セマンティックトークン・出力モード・Presenter・進捗)

- 設計書 §6.1 の7トークンを記号付きで定義(色だけで状態を伝えない)
- --output auto|rich|plain|json と --color、NO_COLOR、TERM=dumb の解決
- plain/json で ANSI とアニメーションを出さない契約テスト
- エラー提示順(コード→概要→原因→回復手順→--debug 案内)を固定

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: SQLite 接続基盤・マイグレーション・ロギング

**Files:**
- Create: `src/abist_kb/infrastructure/db/__init__.py`, `connection.py`, `migrations.py`, `src/abist_kb/infrastructure/observability/__init__.py`, `logging.py`
- Test: `tests/db/test_connection.py`, `tests/db/test_migrations.py`, `tests/observability/test_logging.py`

**Interfaces:**
- Consumes: `abist_kb.domain.errors.{AppError, ErrorCode, ExitCode, wrap}`
- Produces:
  - `connection.py`: `MIN_SQLITE_VERSION: tuple[int, int, int] = (3, 34, 0)`、`connect(path: Path | str, *, read_only: bool = False, timeout_ms: int = 5000) -> sqlite3.Connection`、`sqlite_version_tuple() -> tuple[int, int, int]`、`check_sqlite_capabilities() -> CapabilityReport`(`CapabilityReport` は frozen dataclass: `sqlite_version: str`, `fts5: bool`, `unicode61: bool`, `trigram: bool`, `problems: tuple[str, ...]`, `ok: bool`)、`transaction(conn) -> ContextManager[sqlite3.Connection]`(`BEGIN IMMEDIATE`)
  - `migrations.py`: `Migration`(frozen dataclass: `version: int`, `name: str`, `sql: str`)、`apply_migrations(conn, migrations: Sequence[Migration]) -> list[int]`(適用したバージョン一覧を返す)、`current_version(conn) -> int`、`load_migrations(directory: Path) -> list[Migration]`(`NNNN_name.sql` 形式)
  - `logging.py`: `SECRET_PATTERNS`、`mask_secrets(text: str) -> str`、`SecretMaskingFilter(logging.Filter)`、`configure_logging(*, level: str, stream=None, log_file: Path | None = None) -> None`、`get_logger(name: str) -> logging.Logger`

- [ ] **Step 1: 接続基盤のテストを書く**

`tests/db/test_connection.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError
from abist_kb.infrastructure.db.connection import (
    MIN_SQLITE_VERSION,
    check_sqlite_capabilities,
    connect,
    sqlite_version_tuple,
    transaction,
)


def test_sqlite_version_meets_minimum():
    assert sqlite_version_tuple() >= MIN_SQLITE_VERSION


def test_connect_applies_required_pragmas(tmp_root: Path):
    db = tmp_root / "data" / "app.sqlite"
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_connect_creates_parent_directories(tmp_root: Path):
    db = tmp_root / "深い" / "階層" / "app.sqlite"
    conn = connect(db)
    conn.close()
    assert db.is_file()


def test_rows_are_accessible_by_column_name(tmp_root: Path):
    conn = connect(tmp_root / "a.sqlite")
    try:
        conn.execute("CREATE TABLE t (path TEXT, n INTEGER)")
        conn.execute("INSERT INTO t VALUES ('docs/蛇腹.md', 3)")
        row = conn.execute("SELECT path, n FROM t").fetchone()
        assert row["path"] == "docs/蛇腹.md"
        assert row["n"] == 3
    finally:
        conn.close()


def test_read_only_connection_rejects_writes(tmp_root: Path):
    db = tmp_root / "ro.sqlite"
    w = connect(db)
    w.execute("CREATE TABLE t (x INTEGER)")
    w.commit()
    w.close()

    r = connect(db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            r.execute("INSERT INTO t VALUES (1)")
    finally:
        r.close()


def test_read_only_connection_to_missing_file_raises_app_error(tmp_root: Path):
    with pytest.raises(AppError):
        connect(tmp_root / "ない.sqlite", read_only=True)


def test_transaction_commits_on_success(tmp_root: Path):
    conn = connect(tmp_root / "t.sqlite")
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        with transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_transaction_rolls_back_on_error(tmp_root: Path):
    conn = connect(tmp_root / "t.sqlite")
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_capability_report_on_this_machine():
    report = check_sqlite_capabilities()
    assert report.fts5 is True
    assert report.unicode61 is True
    assert report.trigram is True
    assert report.ok is True
    assert report.problems == ()
    assert report.sqlite_version == sqlite3.sqlite_version
```

- [ ] **Step 2: 失敗を確認 → `connection.py` を実装**

Run: `uv run pytest tests/db/test_connection.py -v` → FAIL

要件:
- `connect`: 親ディレクトリを `mkdir(parents=True, exist_ok=True)`(read_only 時は作らない)、`sqlite3.connect(uri, uri=True)`、`row_factory = sqlite3.Row`、`isolation_level=None`(明示トランザクション制御)、`PRAGMA journal_mode=WAL` / `foreign_keys=ON` / `busy_timeout=<timeout_ms>`。read_only は `file:...?mode=ro` URI。ファイル不在や接続失敗は `wrap(..., ErrorCode.CONFIG_ERROR or FAILURE)`。
  - Windows のパスを URI にするときは `Path.as_uri()` を使わず、`sqlite3.connect(f"file:{path}?mode=ro", uri=True)` でバックスラッシュを `/` に置換し、`?` `#` をパーセントエンコードすること。
  - `journal_mode=WAL` は read_only 接続では設定しない(書込になるため)。
- `check_sqlite_capabilities`: メモリDBで FTS5 の `unicode61` と `trigram` の CREATE を試し、trigram は3文字語の MATCH まで実測する。バージョン不足・機能不足は `problems` に日本語の説明を積む。例外は握り潰さず `problems` へ。
- `transaction`: `BEGIN IMMEDIATE` を発行し、正常終了で COMMIT、例外で ROLLBACK して再送出。

Run: `uv run pytest tests/db/test_connection.py -v` → PASS(9件)

- [ ] **Step 3: マイグレーションのテストを書く**

`tests/db/test_migrations.py`:

```python
from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.migrations import (
    Migration,
    apply_migrations,
    current_version,
    load_migrations,
)

M1 = Migration(version=1, name="core", sql="CREATE TABLE a (id INTEGER PRIMARY KEY);")
M2 = Migration(version=2, name="jobs", sql="CREATE TABLE b (id INTEGER PRIMARY KEY);")


def open_db(tmp_root: Path):
    return connect(tmp_root / "m.sqlite")


def test_applies_migrations_in_order(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        assert apply_migrations(conn, [M1, M2]) == [1, 2]
        assert current_version(conn) == 2
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"a", "b"} <= names
    finally:
        conn.close()


def test_is_idempotent(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        apply_migrations(conn, [M1, M2])
        assert apply_migrations(conn, [M1, M2]) == []
        assert current_version(conn) == 2
    finally:
        conn.close()


def test_applies_only_new_migrations(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        apply_migrations(conn, [M1])
        assert apply_migrations(conn, [M1, M2]) == [2]
    finally:
        conn.close()


def test_records_history_in_schema_migrations(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        apply_migrations(conn, [M1, M2])
        rows = conn.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [r["version"] for r in rows] == [1, 2]
        assert [r["name"] for r in rows] == ["core", "jobs"]
        assert all(r["applied_at"] for r in rows)
    finally:
        conn.close()


def test_current_version_is_zero_on_empty_database(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        assert current_version(conn) == 0
    finally:
        conn.close()


def test_failed_migration_rolls_back_and_is_not_recorded(tmp_root: Path):
    bad = Migration(version=2, name="bad", sql="CREATE TABLE b (x); SELECT bogus();")
    conn = open_db(tmp_root)
    try:
        with pytest.raises(AppError) as excinfo:
            apply_migrations(conn, [M1, bad])
        assert excinfo.value.code == ErrorCode.MIGRATION_FAILED
        assert current_version(conn) == 1
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "b" not in names
    finally:
        conn.close()


def test_duplicate_versions_are_rejected(tmp_root: Path):
    dup = Migration(version=1, name="other", sql="CREATE TABLE c (x INTEGER);")
    conn = open_db(tmp_root)
    try:
        with pytest.raises(AppError):
            apply_migrations(conn, [M1, dup])
    finally:
        conn.close()


def test_database_newer_than_known_migrations_is_refused(tmp_root: Path):
    conn = open_db(tmp_root)
    try:
        apply_migrations(conn, [M1, M2])
        with pytest.raises(AppError) as excinfo:
            apply_migrations(conn, [M1])
        assert excinfo.value.code == ErrorCode.MIGRATION_FAILED
        assert "新しい" in excinfo.value.message
    finally:
        conn.close()


def test_load_migrations_reads_numbered_sql_files(tmp_root: Path):
    d = tmp_root / "migrations"
    d.mkdir()
    (d / "0002_jobs.sql").write_text("CREATE TABLE b (x INTEGER);", encoding="utf-8")
    (d / "0001_core.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
    (d / "README.md").write_text("無視される", encoding="utf-8")
    loaded = load_migrations(d)
    assert [(m.version, m.name) for m in loaded] == [(1, "core"), (2, "jobs")]
```

- [ ] **Step 4: 失敗を確認 → `migrations.py` を実装**

Run: `uv run pytest tests/db/test_migrations.py -v` → FAIL

要件:
- `schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)` を必要時に作成。
- 各マイグレーションは `BEGIN IMMEDIATE` 〜 `executescript` 〜 記録 〜 `COMMIT` を1トランザクションで。失敗時 ROLLBACK して `AppError(ErrorCode.MIGRATION_FAILED)`。
  - 注意: `sqlite3` の `executescript` は暗黙に COMMIT する。`isolation_level=None` で明示 `BEGIN IMMEDIATE` した後に `executescript` を使うと未定義動作になるため、SQL は `sqlparse` 相当の素朴な分割ではなく、**マイグレーション SQL を `;` で分割して `execute` を順に呼ぶ**か、`executescript` を使わず1文ずつ実行する方式にする。分割は文字列リテラル・トリガ本体を壊しうるので、`Migration.sql` は「1文ずつのタプル」を許容する設計にしてもよい(その場合 `sql: str | tuple[str, ...]` とし、テストは文字列版のまま通ること)。
- `applied_at` は UTC ISO-8601(`datetime.now(UTC).isoformat()`)。
- 既知の最大バージョンより DB が新しい場合は「データベースのスキーマが本バージョンより新しいため開けません」旨の `AppError`。
- `load_migrations`: `NNNN_name.sql` にマッチするファイルのみ、バージョン昇順。

Run: `uv run pytest tests/db/test_migrations.py -v` → PASS(9件)

- [ ] **Step 5: ロギングのテストを書く**

`tests/observability/test_logging.py`:

```python
import io
import logging

import pytest

from abist_kb.infrastructure.observability.logging import (
    SecretMaskingFilter,
    configure_logging,
    get_logger,
    mask_secrets,
)


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("ESA_ACCESS_TOKEN=abcdef123456", "abcdef123456"),
        ("OPENAI_API_KEY=sk-proj-XYZ987", "sk-proj-XYZ987"),
        ("ABIST_KB_ESA_ACCESS_TOKEN: nekopunch", "nekopunch"),
        ("Authorization: Bearer eyJhbGciOi.J9.sig", "eyJhbGciOi.J9.sig"),
        ("Cookie: session=deadbeef", "deadbeef"),
        ("https://user:hunter2@example.com/repo.git", "hunter2"),
        ('{"api_key": "kkk-111"}', "kkk-111"),
    ],
)
def test_mask_secrets_removes_the_value(raw, must_not_contain):
    masked = mask_secrets(raw)
    assert must_not_contain not in masked
    assert "***" in masked


def test_mask_secrets_keeps_the_key_name_for_diagnosis():
    assert "ESA_ACCESS_TOKEN" in mask_secrets("ESA_ACCESS_TOKEN=abcdef123456")


def test_mask_secrets_leaves_innocent_text_alone():
    text = "docs/蛇腹形状の自動設計/議事録.md を同期しました(3件)"
    assert mask_secrets(text) == text


def test_mask_secrets_masks_git_url_but_keeps_host():
    masked = mask_secrets("https://user:hunter2@github.com/abist/repo.git")
    assert "hunter2" not in masked
    assert "github.com/abist/repo.git" in masked


def test_filter_masks_message_and_args():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretMaskingFilter())
    logger = logging.getLogger("test.masking")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("token=%s", "ESA_ACCESS_TOKEN=abcdef123456")
    logger.info("Authorization: Bearer secret-value-here")

    out = stream.getvalue()
    assert "abcdef123456" not in out
    assert "secret-value-here" not in out


def test_configure_logging_writes_to_stderr_stream_with_level():
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)
    logger = get_logger("abist_kb.test")
    logger.info("これは出ない")
    logger.warning("これは出る")
    out = stream.getvalue()
    assert "これは出ない" not in out
    assert "これは出る" in out


def test_configure_logging_is_idempotent_and_does_not_duplicate_handlers():
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    configure_logging(level="INFO", stream=stream)
    get_logger("abist_kb.test").info("一度だけ")
    assert stream.getvalue().count("一度だけ") == 1


def test_configure_logging_never_writes_to_stdout(capsys):
    configure_logging(level="DEBUG")
    get_logger("abist_kb.test").error("エラーです")
    captured = capsys.readouterr()
    assert captured.out == ""
```

- [ ] **Step 6: 失敗を確認 → `logging.py` を実装**

Run: `uv run pytest tests/observability/test_logging.py -v` → FAIL

要件:
- `mask_secrets`: 正規表現で (a) `<NAME>=<value>` / `<NAME>: <value>` 形式で NAME が `*_TOKEN` `*_KEY` `*_SECRET` `*_PASSWORD` にマッチするもの、(b) `Authorization: <scheme> <value>`、(c) `Cookie: <value>`、(d) URL の `scheme://user:pass@host` の pass 部、(e) JSON の `"api_key": "..."` 系。値を `***` に置換し、キー名とホスト名は残す。
- `SecretMaskingFilter.filter(record)`: `record.msg` と `record.args` の各要素を文字列化してマスク。マスク後は `record.args = ()` にして二重フォーマットを避ける(あるいは `record.msg = mask_secrets(record.getMessage()); record.args = ()`)。
- `configure_logging`: ルートではなく `abist_kb` ロガーに設定。既存ハンドラをクリアしてから追加(冪等)。`stream` 未指定なら `sys.stderr`。**stdout へは絶対に書かない**。`log_file` 指定時はファイルハンドラも追加(UTF-8)。全ハンドラに `SecretMaskingFilter`。`propagate = False`。
- `get_logger(name)`: `logging.getLogger(name)`。

Run: `uv run pytest tests/observability/test_logging.py -v` → PASS(14件)

- [ ] **Step 7: 全テストと lint**

Run: `uv run ruff check . && uv run pytest -q` → 全 PASS

- [ ] **Step 8: コミット**

```bash
git add src/abist_kb/infrastructure tests/db tests/observability
git commit -m "$(cat <<'EOF'
feat(m0): SQLite 接続基盤・マイグレーション・秘密マスク付きロギング

- WAL / foreign_keys / busy_timeout を強制する接続ファクトリと BEGIN IMMEDIATE
- SQLite 版数・FTS5・unicode61・trigram の実測チェック
- 連番マイグレーションと schema_migrations(冪等・失敗時ロールバック)
- *_TOKEN / *_KEY / Authorization / Cookie / URL 資格情報のマスク

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: CLI 骨格・doctor・config コマンド・CI

**Files:**
- Create: `src/abist_kb/presentation/cli/__init__.py`, `context.py`, `app.py`, `doctor_cmd.py`, `config_cmd.py`, `.github/workflows/ci.yml`
- Test: `tests/cli/test_app.py`, `tests/cli/test_doctor.py`, `tests/cli/test_config_cmd.py`

**Interfaces:**
- Consumes: `abist_kb.config.load_settings`, `abist_kb.domain.errors.{AppError, ErrorCode, ExitCode}`, `abist_kb.presentation.console.{output, presenter}`, `abist_kb.infrastructure.db.connection.check_sqlite_capabilities`, `abist_kb.infrastructure.observability.logging.configure_logging`
- Produces:
  - `context.py`: `CliContext`(frozen dataclass: `settings: Settings`, `presenter: Presenter`, `debug: bool`, `assume_yes: bool`)、`get_context(ctx: typer.Context) -> CliContext`
  - `app.py`: `app: typer.Typer`、`main() -> None`(entry point。`AppError` を捕捉し Presenter で提示して `sys.exit(err.exit_code)`)
  - `doctor_cmd.py`: `doctor(ctx: typer.Context) -> None`
  - `config_cmd.py`: `config_app: typer.Typer`(`show` / `path` / `validate`)

**doctor の仕様**
- 検査項目: Python 版数 / SQLite 版数(≥3.34)/ FTS5 / unicode61 / trigram(3文字 MATCH まで実測)/ 設定ファイル存在 / データディレクトリ書込可否 / ffmpeg 有無(警告のみ)/ Manim 有無(警告のみ)。
- 各項目は `ok` / `warn` / `fail`。`fail` が1つでもあれば `AppError` を送出し終了コード3(`CONFIG_ERROR`)。trigram 不可のときは `ErrorCode.FTS5_TRIGRAM_UNAVAILABLE` と再構築手順を hint に載せる(黙って縮退しない)。
- `--output json` では検査結果の配列を stdout の JSON へ。

- [ ] **Step 1: CLI 骨格のテストを書く**

`tests/cli/test_app.py`:

```python
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
```

`CliRunner` は既定で stderr を stdout に混ぜる版があるため、`CliRunner()` 構築時に stderr 分離が可能なら分離する。分離できない Typer/Click 版では、JSON 純度の検証は `test_output_purity.py` 側(Presenter 単体)を正とし、CLI 側は `json.loads(result.stdout)` が通ることのみ確認する。

`tests/cli/test_doctor.py`:

```python
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
    )
    monkeypatch.setattr(doctor_cmd, "check_sqlite_capabilities", lambda: broken)
    result = runner.invoke(app, ["--root", str(tmp_root), "doctor"])
    assert result.exit_code == 3
    assert "FTS5_TRIGRAM_UNAVAILABLE" in result.output
```

`tests/cli/test_config_cmd.py`:

```python
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
```

- [ ] **Step 2: 失敗を確認**

Run: `uv run pytest tests/cli -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'abist_kb.presentation.cli.app'`

- [ ] **Step 3: `context.py` と `app.py` を実装**

`app.py` の要件:
- `typer.Typer(name=identity.CLI_NAME, help=f"{identity.DISPLAY_NAME} — ...", no_args_is_help=True, add_completion=False)`。
- コールバックで共通オプションを受ける: `--root PATH`(既定 None → cwd)、`--output` (str, 既定 `"auto"`)、`--color` (str, 既定 `"auto"`)、`--quiet/-q`、`--verbose/-v`、`--debug`、`--yes/-y`、`--version`。
- コールバック内で: 出力モード解決 → Presenter 構築 → `load_settings(root=...)` → `configure_logging(level=..., stream=sys.stderr)` → `CliContext` を `ctx.obj` へ格納。
  - `--verbose` は log_level を DEBUG へ、`--quiet` は WARNING へ引き上げる(`--debug` は DEBUG かつ Rich Traceback 有効)。
  - 不正な `--output` / `--color` 値は `AppError(ErrorCode.INVALID_INPUT, exit_code=ExitCode.INVALID_INPUT)`。
- `main()`: `app()` を `try/except AppError` で囲み、Presenter が構築済みならそれで、未構築なら最小の Presenter を作って `error(err)` を出し `sys.exit(int(err.exit_code))`。`KeyboardInterrupt` は `ExitCode.CANCELLED`。`typer.Exit` / `click.exceptions` はそのまま通す。
- `app.add_typer(config_app, name="config")`、`app.command("doctor")(doctor)`。
- Typer の未知コマンド・引数エラーは Click が終了コード2を返すため、既定のままで `INVALID_INPUT` と一致する。

- [ ] **Step 4: `doctor_cmd.py` と `config_cmd.py` を実装**

`doctor_cmd.py` は `check_sqlite_capabilities` をモジュール属性として import する(テストが `monkeypatch.setattr(doctor_cmd, "check_sqlite_capabilities", ...)` で差し替えるため、`from ... import check_sqlite_capabilities` の形にすること)。

チェック結果は `{"name": str, "status": "ok"|"warn"|"fail", "detail": str, "hint": str | None}` の配列。JSON 出力は `{"ok": bool, "checks": [...]}`。Rich/plain 出力は表で `記号 名前 状態 詳細` を並べ、`fail`/`warn` があれば hint を続けて表示。

- [ ] **Step 5: テストが通ることを確認**

Run: `uv run pytest tests/cli -v`
Expected: PASS(15件)

- [ ] **Step 6: 実機で doctor を動かす(M0 ゲートの実測)**

Run: `uv run abist-kb doctor`
Expected: 終了コード0。SQLite 3.50.4、FTS5・unicode61・trigram がすべて `✓`。

Run: `uv run abist-kb --output json doctor | python -c "import json,sys; d=json.load(sys.stdin); print(d['ok'])"`
Expected: `True`

- [ ] **Step 7: CI ワークフローを作成**

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [windows-latest, ubuntu-latest]
    runs-on: ${{ matrix.os }}
    env:
      PYTHONUTF8: "1"
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Sync dependencies
        run: uv sync --locked

      - name: Lint
        run: uv run ruff check .

      - name: Format check
        run: uv run ruff format --check .

      - name: Test
        run: uv run pytest -q

      - name: Doctor smoke
        run: uv run abist-kb doctor
```

- [ ] **Step 8: 全テスト・lint・整形を通す**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: 全 PASS

- [ ] **Step 9: コミット**

```bash
git add src/abist_kb/presentation/cli tests/cli .github
git commit -m "$(cat <<'EOF'
feat(m0): CLI 骨格・doctor・config コマンド・CI

- Typer アプリと共通オプション(--output/--color/--quiet/--verbose/--debug/--yes)
- AppError を終了コードへ写像する例外ハンドラ
- doctor: SQLite 版数・FTS5・unicode61・trigram を実測(不可なら黙って縮退せず失敗)
- config show|path|validate(秘密情報はマスク)
- GitHub Actions で Windows/Linux 両方の uv sync --locked + ruff + pytest

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

## Verification(M0 ゲート)

1. `uv sync --locked` が成功する(再現性)。
2. `uv run ruff check . && uv run ruff format --check .` がクリーン。
3. `uv run pytest -q` が全 PASS(Windows 実機)。
4. `uv run abist-kb doctor` が終了コード0で、SQLite ≥3.34・FTS5・unicode61・trigram をすべて `ok` と報告する。
5. `uv run abist-kb --output json doctor` の stdout が単一 JSON として parse でき、ANSI を含まない。
6. `tests/console/test_output_purity.py` が PASS(plain/json で ANSI・アニメーション皆無、RICH では色が出る対照テストも含む)。
7. CI が windows-latest と ubuntu-latest の両方で green。

## 次のマイルストーン

M0 完了後は **M1(Node 版からの fixture 採取)** へ進む。M1 は旧システムが無傷であるうちに実施する必要があるため、遅延させない。
