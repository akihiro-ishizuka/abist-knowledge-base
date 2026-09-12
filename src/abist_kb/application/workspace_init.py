"""ワークスペースの初期化(`abist-kb init`)。

新鮮なクローンには `docs/` も `data/` も `config/settings.toml` も存在しない。
このモジュールは **Settings が解決したパスにだけ** ディレクトリ・雛形ファイル・
`app.sqlite` のスキーマを用意する。固定文字列の `"docs"` 等でパスを組み立てては
ならない(`--root` や `ABIST_KB_DOCS_DIR` で既定からずらした環境で、意図しない
場所にワークスペースを作ってしまうため)。生成先は必ず `Settings` の属性から導く。

`Settings.ensure_directories()` はここでは使わない(あちらは doctor / MCP 起動時に
毎回呼ばれる副作用の小さいヘルパであり、docs や config を作る責務を足すと
「参照しただけのつもりが書き込んでいた」という驚きを全経路へ広げてしまう)。
init 専用の scaffold をこのモジュールへ隔離する。

**非破壊であることがこのモジュールの契約**:

- ディレクトリは `mkdir(parents=True, exist_ok=True)`。
- 雛形3種(`settings.toml` / `.env.example` / `docs/README.md`)は
  **パスがファイルとして未存在のときだけ** 書く。既存ファイルは1バイトも触らない。
- ファイルを書くべきパスに同名ディレクトリがある(またはその逆)場合は
  `AppError(INVALID_INPUT)` で停止する。上書きも削除もしない。
- **既知の対象パスの種別の食い違いは、書き込みを始める前にまとめて検査する**
  (1つ目を作った後で2つ目の衝突に気付く、という順序依存を無くすため)。

したがって再実行は冪等であり、欠けている項目だけが追加される。

**ロールバックはしない**。上記の事前検査が対象にするのは生成対象のパスそのものだけ
であり、検査を通った後の I/O エラー(権限・ディスク不足など)や、検査対象に含まれない
祖先パスの衝突では、途中まで作られたディレクトリ・ファイルがそのまま残る。ただし
生成はすべて「無ければ作る」であり既存物を書き換えないため、原因を取り除いてから
再実行すれば残りの項目が追加されて完全な状態になる。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from abist_kb import identity
from abist_kb.config import Settings
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode, wrap
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.infrastructure.search.corpus import REFERENCE_SUBTREE

#: `config/settings.toml` の雛形。**秘密情報は書かない**(§9.1 は `.env` を
#: 秘密情報の唯一の格納場所と定めている)。`load_settings` でそのまま再ロード
#: できる TOML であること。
SETTINGS_TOML_TEMPLATE = """\
# 秘密情報は .env へ書く(このファイルには書かない)
log_level = "INFO"
embedding_model = "intfloat/multilingual-e5-small"
missing_threshold = 3
"""

#: `docs/README.md` の雛形の元。`{cli}` はこのプロジェクトの CLI 名で埋める。
_DOCS_README_TEMPLATE = """\
# docs/

このディレクトリが Markdown の正本です(UTF-8、パスは docs 相対の POSIX 形式)。

- 実務コーパス向けの文書はこの直下に自由な階層で置く。
- 参照コーパス(B32doc)は `{reference_subtree}/` 配下に置く。

## 手置きした Markdown を検索できるようにする

**ファイルを置いただけでは索引されません。`index build` だけでも索引されません。**
索引対象は `app.sqlite` の `documents` テーブルに登録済みの文書だけなので、
手で置いたファイルはまず台帳へ登録する必要があります。

```bash
{cli} document register-disk          # 何が登録されるかを確認(dry-run)
{cli} document register-disk --apply  # documents へ登録
{cli} index build --corpus work       # 索引を構築
{cli} index embed --corpus work       # 意味検索が必要な場合
```

`document register-disk` は未登録のファイルだけを登録し、esa / web / git が
管理している既存の行には触れません。
"""

#: `.env.example` の見出し。実環境の値は決して転記しない。
_ENV_EXAMPLE_HEADER = """\
# 秘密情報。このファイルを .env にコピーし、コメントを外して値を入れる。
"""

#: `.env.example` に並べる環境変数(`identity.env_var` 由来の正式名のみ)と説明。
_ENV_EXAMPLE_ENTRIES: tuple[tuple[str, str | None], ...] = (
    ("esa_access_token", None),
    ("esa_team_name", "任意: esa チーム名は settings.toml の esa_team_name でも可"),
)


def env_example_content() -> str:
    """`.env.example` の内容を組み立てる(値は空、全行コメントアウト)。"""
    lines = [_ENV_EXAMPLE_HEADER.rstrip("\n")]
    for field_name, note in _ENV_EXAMPLE_ENTRIES:
        if note:
            lines.append(f"# {note}")
        lines.append(f"# {identity.env_var(field_name)}=")
    return "\n".join(lines) + "\n"


def docs_readme_content() -> str:
    """`docs/README.md` の内容を組み立てる。"""
    return _DOCS_README_TEMPLATE.format(cli=identity.CLI_NAME, reference_subtree=REFERENCE_SUBTREE)


def reference_corpus_dir(settings: Settings) -> Path:
    """参照コーパス(B32doc)の置き場所。案内文言もここから作る(固定文字列にしない)。"""
    docs_dir = settings.docs_dir
    assert docs_dir is not None  # `Settings._derive_paths` が必ず埋める
    return docs_dir / REFERENCE_SUBTREE


def _dedupe(paths: list[Path]) -> list[Path]:
    """初出順を保ったまま重複を除く(既定設定では `data` が2回現れるため)。"""
    seen: dict[str, None] = {}
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen[key] = None
            unique.append(path)
    return unique


def _directory_targets(settings: Settings) -> list[Path]:
    """作成対象のディレクトリ(すべて `Settings` から導く)。"""
    docs_dir = settings.docs_dir
    assert docs_dir is not None  # `Settings._derive_paths` が必ず埋める(以下同様)
    app_db_path = settings.app_db_path
    assert app_db_path is not None
    config_file = settings.config_file
    assert config_file is not None
    reports_dir = settings.reports_dir
    assert reports_dir is not None
    data_dir = settings.data_dir
    assert data_dir is not None
    cache_dir = settings.cache_dir
    assert cache_dir is not None

    return _dedupe(
        [
            docs_dir,
            reference_corpus_dir(settings),
            reports_dir,
            data_dir,
            cache_dir,
            config_file.parent,
            app_db_path.parent,
        ]
    )


def _file_targets(settings: Settings) -> list[tuple[Path, str]]:
    """作成対象の雛形ファイルと内容(未存在のときだけ書く)。"""
    config_file = settings.config_file
    assert config_file is not None
    docs_dir = settings.docs_dir
    assert docs_dir is not None

    return [
        (config_file, SETTINGS_TOML_TEMPLATE),
        (settings.root_dir / ".env.example", env_example_content()),
        (docs_dir / "README.md", docs_readme_content()),
    ]


def _check_conflicts(directories: list[Path], files: list[Path], app_db_path: Path) -> None:
    """生成対象のパス自身の種別の食い違いを、書き込みを始める前にまとめて検出する。

    検査するのは列挙された対象パスだけで、その祖先までは辿らない
    (祖先の衝突は `mkdir` 時の `OSError` として `FAILURE` へ正規化される)。
    """
    for path in directories:
        if path.exists() and not path.is_dir():
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"ディレクトリを作成できません(同名のファイルが存在します): {path}",
                hint="このパスのファイルを退避するか、別のルート/パス設定を指定してください。",
                exit_code=ExitCode.INVALID_INPUT,
            )
    for path in (*files, app_db_path):
        if path.exists() and not path.is_file():
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"ファイルを作成できません(同名のディレクトリが存在します): {path}",
                hint="このパスのディレクトリを退避するか、別のルート/パス設定を指定してください。",
                exit_code=ExitCode.INVALID_INPUT,
            )


def _sorted_strings(paths: list[Path]) -> list[str]:
    """JSON へそのまま載せられる、安定ソート済みのパス文字列。"""
    return sorted(str(path) for path in paths)


def init_workspace(settings: Settings) -> dict[str, Any]:
    """ワークスペースを scaffold し、何を作って何を飛ばしたかを返す。

    戻り値は `json.dumps` 可能で、リストは安定ソート済み。
    `app_db_initialized` は「この呼び出しで DB ファイルを新規作成したか」であり、
    既存 DB を開いてスキーマだけ保証した場合は `False` になる(既存行は保持される)。
    """
    app_db_path = settings.app_db_path
    assert app_db_path is not None

    directories = _directory_targets(settings)
    files = _file_targets(settings)
    _check_conflicts(directories, [path for path, _ in files], app_db_path)

    created_dirs: list[Path] = []
    skipped_dirs: list[Path] = []
    for path in directories:
        if path.is_dir():
            skipped_dirs.append(path)
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise wrap(
                exc,
                code=ErrorCode.FAILURE,
                message=f"ディレクトリを作成できませんでした: {path}",
            ) from exc
        created_dirs.append(path)

    created_files: list[Path] = []
    skipped_files: list[Path] = []
    for path, content in files:
        if path.is_file():
            skipped_files.append(path)
            continue
        try:
            # newline="\n" 固定: Windows で生成しても雛形のバイト列を同じにする
            # (冪等性テストがバイト単位の不変を検査する)。
            path.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise wrap(
                exc,
                code=ErrorCode.FAILURE,
                message=f"ファイルを作成できませんでした: {path}",
            ) from exc
        created_files.append(path)

    app_db_existed = app_db_path.is_file()
    conn = open_app_db(app_db_path)
    conn.close()

    return {
        "root_dir": str(settings.root_dir),
        "created_dirs": _sorted_strings(created_dirs),
        "skipped_dirs": _sorted_strings(skipped_dirs),
        "created_files": _sorted_strings(created_files),
        "skipped_files": _sorted_strings(skipped_files),
        "app_db_path": str(app_db_path),
        "app_db_initialized": not app_db_existed,
    }


__all__ = [
    "SETTINGS_TOML_TEMPLATE",
    "docs_readme_content",
    "env_example_content",
    "init_workspace",
    "reference_corpus_dir",
]
