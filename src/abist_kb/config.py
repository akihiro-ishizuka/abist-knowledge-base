"""設定の読み込みとパス解決(設計書 §9.1)。"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from abist_kb import identity
from abist_kb.domain.errors import ErrorCode, ExitCode, wrap

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_SECRET_FIELDS = frozenset({"esa_access_token", "openai_api_key", "git_token", "teams_webhook_url"})


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

    @field_validator("root_dir", mode="before")
    @classmethod
    def _blank_root_dir_is_unset(cls, value: Any) -> Any:
        """空白のみの `ABIST_KB_ROOT_DIR`(例: `"   "`)を未設定として扱う。

        `pydantic_settings` は環境変数を生の文字列として渡すため、素朴な
        `Path(value)` 変換だと `Path("   ")` という使い物にならないルートが
        できてしまう(デフォード#3)。空白のみの文字列が来た場合はフィールド未指定
        として扱い、`default_factory=Path.cwd` にフォールバックさせる。
        """
        if isinstance(value, str) and not value.strip():
            return Path.cwd()
        return value

    docs_dir: Path | None = None
    reports_dir: Path | None = None
    data_dir: Path | None = None
    config_file: Path | None = None
    app_db_path: Path | None = None
    work_index_path: Path | None = None
    reference_index_path: Path | None = None
    cache_dir: Path | None = None
    teams_state_path: Path | None = None

    log_level: LogLevel = "INFO"
    embedding_model: str = "intfloat/multilingual-e5-small"
    missing_threshold: int = Field(default=3, ge=1)

    esa_team_name: str | None = None
    esa_access_token: str | None = None
    #: private リポジトリ用の Git 認証トークン(§12: DB/ログ/移行成果物へは書かない)。
    #: `sources.connection` の `token` キーがチーム/リポジトリ単位の上書きとして
    #: 優先され、これは単一トークンで足りる一般的な運用向けのフォールバック。
    git_token: str | None = None
    openai_api_key: str | None = None
    chat_model: str = "gpt-4o-mini"
    #: 連絡チャットへの投稿に使う Power Automate Workflows Webhook。
    #: 送信専用であり読み取りには使えない(設計 §2)。
    teams_webhook_url: str | None = None
    teams_chat_id: str = "19:meeting_Yzc0Yjc4OWUtOGE3Ny00ODYxLWJiZjMtZDI2YzgyMDBlNzBh@thread.v2"
    #: 検索の probe。チャットIDで絞れずクエリ必須のため、高頻度のかなを複数投げて
    #: 結果を統合する(設計 §5.1)。コードへ埋め込まず設定値として持つ。
    teams_search_probes: tuple[str, ...] = ("い", "の", "す")
    teams_overlap_minutes: int = Field(default=30, ge=1)
    teams_reminder_business_hours: int = Field(default=4, ge=1)
    teams_max_posts_per_tick: int = Field(default=3, ge=1)
    teams_retention_days: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _derive_paths(self) -> Settings:
        root = self.root_dir.expanduser()
        self.root_dir = root
        defaults: dict[str, Path] = {
            "docs_dir": root / "docs",
            "reports_dir": root / "reports",
            "data_dir": root / "data",
            "config_file": root / "config" / "settings.toml",
        }
        for name, value in defaults.items():
            if getattr(self, name) is None:
                setattr(self, name, value)

        data = self.data_dir
        assert data is not None  # 直上で必ず埋まる
        derived_from_data: dict[str, Path] = {
            "app_db_path": data / "app.sqlite",
            "work_index_path": data / "work-index.sqlite",
            "reference_index_path": data / "reference-index.sqlite",
            "cache_dir": data / "cache",
            "teams_state_path": data / "teams-watch-state.json",
        }
        for name, value in derived_from_data.items():
            if getattr(self, name) is None:
                setattr(self, name, value)
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


def _resolve_effective_root(root: Path | None) -> Path:
    """`load_settings` が TOML 探索・`.env` 探索・root_dir 上書きの3箇所すべてに使う唯一の root。

    優先順位: 明示引数 `root` > `ABIST_KB_ROOT_DIR` 環境変数 > カレントディレクトリ。
    空白のみの `ABIST_KB_ROOT_DIR`(例: `"   "`)は未設定として扱う(`Path("   ")`
    という使い物にならないルートを組み立てないため)。
    `Settings` モデル自身も root_dir 未指定時にこの環境変数を同じ優先順位で解決するため、
    ここで一度だけ解決した値を3箇所(TOML の場所、`.env` の場所、overrides の root_dir)に
    使い、「TOML と .env はここ、Settings.root_dir はあそこ」というズレが起きないようにする。
    """
    if root is not None:
        return root.expanduser()
    env_root = os.environ.get(identity.env_var("root_dir"), "").strip()
    if env_root:
        return Path(env_root).expanduser()
    return Path.cwd()


def load_settings(root: Path | None = None, config_file: Path | None = None) -> Settings:
    """設定を読み込む。優先順位は 環境変数 > .env > settings.toml > 既定。"""
    effective_root = _resolve_effective_root(root)
    toml_path = config_file or (effective_root / "config" / "settings.toml")

    file_values: dict[str, Any] = {}
    if toml_path.is_file():
        file_values = {k: v for k, v in _read_toml(toml_path).items() if v is not None}
    # ファイル値は既定値の置き換えであり、環境変数より弱い。
    # BaseSettings は「引数 > 環境変数」の順なので、ファイル値は引数として渡さず
    # 既定値の上書きとして扱うため、環境変数に存在するキーは除外する。
    for key in list(file_values):
        if identity.env_var(key) in os.environ:
            del file_values[key]

    overrides: dict[str, Any] = dict(file_values)
    # root が明示されていない場合は root_dir を overrides に入れない。
    # pydantic-settings の優先順位は「引数 > 環境変数」なので、ここで root_dir を
    # 常に渡すと ABIST_KB_ROOT_DIR 環境変数が無視されてしまう
    # (Settings 自身が同じ環境変数を読んで解決する)。
    if root is not None:
        overrides["root_dir"] = effective_root
    if config_file is not None:
        overrides["config_file"] = config_file

    # `model_config` の `env_file=".env"` は pydantic-settings が呼び出しプロセスの
    # カレントディレクトリを基準に解決するため、`root`/`ABIST_KB_ROOT_DIR` を指定しても
    # `.env` だけは無関係な CWD から読まれてしまう(settings.toml は上で既に
    # `effective_root` から明示的に読んでいるが、秘密情報を運ぶ `.env` はこの
    # `_env_file` 上書きが無いと同じ扱いにならない)。`_env_file` は pydantic-settings
    # が受け付ける init kwarg であり、`model_config` の既定値を1回の呼び出し限りで
    # 上書きできる。§9.1 は `.env` を秘密情報の唯一の格納場所と定めており、2つの
    # ナレッジベースを同一マシンで扱う運用では取り違えがそのまま資格情報の誤用になる。
    overrides["_env_file"] = effective_root / ".env"

    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise wrap(
            exc,
            code=ErrorCode.CONFIG_ERROR,
            message="設定値が不正です。",
            hint=f"`{identity.CLI_NAME} config validate` で詳細を確認してください。",
            # `include_input=False`/`include_context=False`: pydantic の生の入力値
            # (`input`)や検証コンテキスト(`ctx`)を機械可読出力へそのまま漏らさない。
            # TOML はネイティブ型(`datetime.date` 等)を持つため、クォート忘れの
            # ような単純な入力ミスで `input` に JSON化できないオブジェクトが
            # 紛れ込み、`--output json` でこのエラー自体を報告しようとした
            # `json.dumps` がその場で `TypeError` を送出してクラッシュしていた
            # (エラー報告そのものが失敗するという最悪の失敗形態、実測で確認済み)。
            details={
                "errors": exc.errors(include_url=False, include_input=False, include_context=False)
            },
            exit_code=ExitCode.CONFIG_ERROR,
        ) from exc
