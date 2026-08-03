"""秘密情報マスキング付きロギング設定(設計書 §9.4)。

`*_TOKEN` `*_KEY` `*_SECRET` `*_PASSWORD` 形式の環境変数、`Authorization` /
`Cookie` ヘッダ、URL に埋め込まれた資格情報、JSON の `"api_key": "..."` 系の値を
ログへ書き出す前に伏せる。ロギングは常に stderr(および任意でファイル)へ出し、
stdout(`--output json` と MCP の JSON-RPC ストリームの専用領域)へは絶対に書かない。
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import TextIO

from abist_kb import identity

_SECRET_NAME_SUFFIX = r"(?:TOKEN|KEY|SECRET|PASSWORD)"

_KEY_VALUE_RE = re.compile(
    rf"(?P<name>[A-Za-z_][A-Za-z0-9_]*_{_SECRET_NAME_SUFFIX})"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\S+)",
    re.IGNORECASE,
)
"""`NAME=value` / `NAME: value` 形式(NAME が *_TOKEN 等)。"""

_AUTHORIZATION_RE = re.compile(
    r"(?P<prefix>Authorization\s*:\s*\S+\s+)(?P<value>\S+)",
    re.IGNORECASE,
)
"""`Authorization: <scheme> <value>` の value 部のみ。"""

_COOKIE_RE = re.compile(
    r"(?P<prefix>Cookie\s*:\s*)(?P<value>\S+)",
    re.IGNORECASE,
)
"""`Cookie: <value>` の value 部のみ。"""

_URL_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:)(?P<password>[^@\s/]+)(?P<at_host>@)"
)
"""`scheme://user:pass@host` の pass 部のみ(scheme・user・host は保持)。"""

_JSON_SECRET_RE = re.compile(
    rf'(?P<key>"[A-Za-z0-9_]*{_SECRET_NAME_SUFFIX}")(?P<sep>\s*:\s*)"(?P<value>[^"]*)"',
    re.IGNORECASE,
)
"""JSON の `"api_key": "..."` 系(キー名が *_key 等で終わるもの)の値部分のみ。"""

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_KEY_VALUE_RE, r"\g<name>\g<sep>***"),
    (_AUTHORIZATION_RE, r"\g<prefix>***"),
    (_COOKIE_RE, r"\g<prefix>***"),
    (_URL_CREDENTIAL_RE, r"\g<prefix>***\g<at_host>"),
    (_JSON_SECRET_RE, r'\g<key>\g<sep>"***"'),
)
"""適用順に並んだ (正規表現, 置換テンプレート) の組。

キー名・スキーム・ホスト名は診断のため保持し、値だけを `***` に置換する。
各パターンは互いに排他的な文字列構造(区切り文字・引用符・`://` の有無)を
前提にしているため、順序を入れ替えても既知のテストケースには影響しない。
"""


def mask_secrets(text: str) -> str:
    """既知の秘密情報パターンを `***` に置換したテキストを返す。

    元のテキストに一致箇所が無ければ、そのままの文字列を返す
    (`test_mask_secrets_leaves_innocent_text_alone` が保証する)。
    """
    masked = text
    for pattern, replacement in SECRET_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


class SecretMaskingFilter(logging.Filter):
    """ログレコードのメッセージから秘密情報を除去する `logging.Filter`。

    `record.getMessage()` で `%` 書式化を先に済ませてからマスクし、
    `record.args` を空タプルにして `record.msg` を再フォーマットさせない。
    (`LogRecord.getMessage()` は `self.args` が偽値のときは `%` 演算を行わないため、
    マスク後の文字列にたまたま `%` が含まれていても安全。)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = ()
        return True


_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(
    *,
    level: str,
    stream: TextIO | None = None,
    log_file: Path | None = None,
) -> None:
    """`abist_kb` ロガー(ルートロガーではない)へハンドラを設定する。

    複数回呼んでも安全なように、既存ハンドラを一旦クリアしてから追加し直す(冪等)。
    `stream` 未指定時は呼び出し時点の `sys.stderr` を使う
    (デフォルト引数として束縛すると capsys 等によるストリーム差し替えより先に
    評価されてしまうため、必ず関数本体内で解決する)。stdout へは絶対に書かない。
    """
    logger = logging.getLogger(identity.PACKAGE_NAME)
    logger.setLevel(level)

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT)
    masking_filter = SecretMaskingFilter()

    target_stream = stream if stream is not None else sys.stderr
    stream_handler = logging.StreamHandler(target_stream)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(masking_filter)
    logger.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(masking_filter)
        logger.addHandler(file_handler)

    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """指定した名前のロガーを返す(`abist_kb` の下位ロガーを想定)。"""
    return logging.getLogger(name)


__all__ = [
    "SECRET_PATTERNS",
    "SecretMaskingFilter",
    "configure_logging",
    "get_logger",
    "mask_secrets",
]
