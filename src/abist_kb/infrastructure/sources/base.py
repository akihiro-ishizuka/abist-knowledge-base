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


__all__ = ["docs_relative_path", "ensure_within_docs"]
