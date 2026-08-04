"""秘密情報マスキング付きロギング設定(設計書 §9.4)。

`*_TOKEN` `*_KEY` `*_SECRET` `*_PASSWORD` 形式の環境変数、`Authorization` /
`Cookie` ヘッダ、URL に埋め込まれた資格情報、JSON の `"api_key": "..."` 系の値を
ログへ書き出す前に伏せる。ロギングは常に stderr(および任意でファイル)へ出し、
stdout(`--output json` と MCP の JSON-RPC ストリームの専用領域)へは絶対に書かない。

`mask_secrets`/`SECRET_PATTERNS` 本体は `abist_kb.domain.redaction` へ移設した
(IMPORTANT 3: ロギングに依存しない純粋関数として `presentation/` からも直接
import できるようにするため)。ここでは既存の import パス
(`from abist_kb.infrastructure.observability.logging import mask_secrets`)を
壊さないよう re-export のみ行う。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TextIO

from abist_kb import identity
from abist_kb.domain.redaction import SECRET_PATTERNS, mask_secrets

_TRACEBACK_FORMATTER = logging.Formatter()
"""`formatException()` 専用に使う無地の `Formatter`。フォーマット文字列(`fmt`)は
例外整形には関与しないため、ハンドラごとに異なる `Formatter` を持っていても
ここで生成したものだけで十分。"""


class SecretMaskingFilter(logging.Filter):
    """ログレコードのメッセージ・トレースバックから秘密情報を除去する `logging.Filter`。

    `record.getMessage()` で `%` 書式化を先に済ませてからマスクし、
    `record.args` を空タプルにして `record.msg` を再フォーマットさせない。
    (`LogRecord.getMessage()` は `self.args` が偽値のときは `%` 演算を行わないため、
    マスク後の文字列にたまたま `%` が含まれていても安全。)

    `record.exc_info` がある場合(`logger.exception(...)` 等)は、それを整形した
    トレースバック文字列もマスクして `record.exc_text` に書き込む。
    `logging.Formatter.format()` は `record.exc_text` が既に設定されていれば
    (`if not record.exc_text: record.exc_text = self.formatException(...)`)
    自分の `formatException()` を呼ばずそれをそのまま使うため、ハンドラの
    `Formatter` が独自の `formatException()` を持っていてもこのマスク済みキャッシュが
    優先される。ここでマスクしないと、`wrap()` が `__cause__`/`details` に保持する
    捕捉例外の生メッセージ(`AppError.to_dict()` が機械可読出力から意図的に
    除外しているもの)が、`logger.exception(...)` の連鎖トレースバック経由で
    生ログへそのまま漏れてしまう。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = mask_secrets(_TRACEBACK_FORMATTER.formatException(record.exc_info))
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
