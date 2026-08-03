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
