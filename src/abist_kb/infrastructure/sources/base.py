"""ソースアダプター共通のパス安全性ヘルパー(設計書 §9.2、task-3-brief Step 2)。

esa/web/git の各アダプターは出力先パスをカテゴリ・記事名から動的に組み立てる。
`domain.metadata_schema.sanitize_file_name`/`sanitize_category_path` は文字単位の
安全化(禁止文字の置換・Windows予約名対策・255バイト切詰)を行うが、`..` の
ようなトラバーサル列や絶対パスの断片はそもそも「禁止文字」に含まれないため
すり抜ける。ここではパスを実際に組み立てた *後* に、解決結果が `docs/` の外へ
出ていないかを最終防衛線として検証する。
"""

from __future__ import annotations

from pathlib import Path

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode


def docs_relative_path(file_path: Path, docs_dir: Path) -> str | None:
    """`file_path` の `docs_dir` からの相対 POSIX パス。`docs_dir` の外なら `None`。"""
    try:
        relative = file_path.resolve().relative_to(docs_dir.resolve())
    except ValueError:
        return None
    posix = relative.as_posix()
    return posix if posix != "." else None


def with_docs_prefix(directory: str) -> str:
    """出力先ディレクトリに `docs/` を強制付与する(esa/web/git 共通)。

    旧実装は esa バッチのみこの正規化を持ち(`download-batch.js`)、web/git の
    バッチ設定(`outputDir`)にはこの保証が無かった。web は `download-web.js`
    が `--output-dir` をそのままファイルパス組み立てに使うため、バッチ設定で
    `docs/` を含まないディレクトリ名を指定すると `docs/` の外へ書き込まれ、
    データベースからも見えなくなる不具合が実際にあった(`PROVENANCE.md` の
    `docs/knowledge/catiadoc` 孤児データの記録参照)。git は `download-git.js`
    自身が `if (!outputDir.startsWith('docs')) outputDir = join('docs',
    outputDir)` という部分的な保証を既に持っていた(`docs` で始まる別ディレクトリ
    名, 例: `docsx`, を誤って許してしまう緩さはあったが)。ここでは esa と同じ
    厳密な正規化(`docs` 完全一致 or `docs/` 始まり)に3ソースとも揃える。
    """
    normalized = directory.replace("\\", "/").rstrip("/")
    if normalized == "docs" or normalized.startswith("docs/"):
        return normalized
    return f"docs/{normalized}"


def ensure_within_docs(file_path: Path, docs_dir: Path) -> Path:
    """`file_path` が `docs_dir` 配下に収まっていることを保証する(最終防衛線)。

    カテゴリ名・記事名に `..` や絶対パスの断片が混入していた場合、文字単位の
    サニタイズをすり抜けて `docs/` の外への書き込みを許してしまう恐れがある
    ため、パスを組み立てた後にここで解決済みパスを検証する。
    """
    resolved = file_path.resolve()
    resolved_docs = docs_dir.resolve()
    if resolved != resolved_docs and resolved_docs not in resolved.parents:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"保存先パスが docs/ の外に出ています: {file_path}",
            hint="カテゴリ名・記事名にパス区切りやトラバーサル(..)を含めないでください。",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return resolved


__all__ = ["docs_relative_path", "ensure_within_docs", "with_docs_prefix"]
