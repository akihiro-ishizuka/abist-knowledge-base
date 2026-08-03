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

_NAME_PREFIX_MAX_LEN = 64
"""環境変数名・JSON キー名の接頭辞に許す最大長(ReDoS 対策)。

以前は `[A-Za-z0-9_]*`(無制限の `*`)を使っていたが、この文字クラスは直後に必須の
リテラル `_` を要求する構造と重複していた(`_` はどちらにもマッチしうる)ため、
TOKEN/KEY/SECRET/PASSWORD を含まない長いアンダースコア連結文字列
(長い Windows パス・ドキュメントID列・base64url・幅広い JSON キー等、
日常的なログ行に現れうるもの)に対して二次関数的なバックトラックが発生し、
64,000文字の入力で実測 81.6 秒も応答が止まっていた。`{0,64}` のように上限を
設けることで、各開始位置でのバックトラック量を定数に抑え、全体の計算量を
線形に戻す(実測で 64,000 文字が 1 秒未満になることを確認済み)。
"""

_KEY_VALUE_RE = re.compile(
    rf"(?P<name>[A-Za-z_][A-Za-z0-9_]{{0,{_NAME_PREFIX_MAX_LEN}}}_{_SECRET_NAME_SUFFIX})"
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
    r"(?P<prefix>Cookie\s*:\s*)(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
"""`Cookie: <value>` の行末までを value とする。

実際の Cookie ヘッダは `a=1; b=2; ...` のように複数ペアを持つのが普通で、
以前の `\\S+`(最初の空白区切りトークンのみ)では2つ目以降のペアがログに
残っていた。行末(次の改行の直前)まで丸ごとマスクすることで取りこぼしを防ぐ。
"""

_URL_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:)(?P<password>[^@\s/]+)(?P<at_host>@)"
)
"""`scheme://user:pass@host` の pass 部のみ(scheme・user・host は保持)。"""

_JSON_SECRET_RE = re.compile(
    rf'(?P<key>"[A-Za-z_][A-Za-z0-9_]{{0,{_NAME_PREFIX_MAX_LEN}}}_{_SECRET_NAME_SUFFIX}")'
    r'(?P<sep>\s*:\s*)"(?P<value>[^"]*)"',
    re.IGNORECASE,
)
"""JSON の `"api_key": "..."` 系(キー名が `_key` 等で終わるもの)の値部分のみ。

`_KEY_VALUE_RE` と同様、suffix の直前に `_` を要求する語境界の制約を課している。
これが無いと `monkey` `donkey` `jockey` のように「たまたま key 等で終わる」だけの
無害な JSON キーの値まで `***` に潰してしまう(このツールは任意のユーザー/API の
JSON をログへ通すため、正当な内容の無言破壊は診断の信頼性を損なう)。
`{0,64}` の上限は `_KEY_VALUE_RE` と同じ ReDoS 対策(こちらは `"` に守られて
致命的ではなかったが、偶然の産物であり設計とは言えないため揃えて対策する)。
"""

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
