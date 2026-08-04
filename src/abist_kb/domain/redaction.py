"""秘密情報マスキングの純粋関数(設計書 §12)。

`mask_secrets` はログだけでなく、CLI のエラー表示(`presentation.console.presenter`)
など、秘密情報が混入しうるあらゆる文字列表示経路から共通で呼び出される。
`logging` パッケージに依存しない純粋関数としてここへ独立させ、`presentation/` からも
`infrastructure.observability.logging`(ロギング専用の関心事)を経由せずに直接
import できるようにする。`infrastructure.observability.logging` は後方互換のため
本モジュールから re-export する。
"""

from __future__ import annotations

import re

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

IMPORTANT 6 の修正でセパレータに `-` も許すようになった後も(`[A-Za-z0-9_-]`)、
この文字クラスと直後の必須セパレータ `[-_]` は依然として重複しているが、
`{0,64}` の上限がある限りバックトラック量は定数のままであり、ReDoS 再発の
条件(無制限の量指定子 + 重複する文字クラス)は満たさない。パターン変更のたびに
`test_mask_secrets_handles_long_underscore_runs_quickly` を再実行して確認すること。
"""

_KEY_VALUE_RE = re.compile(
    rf"(?P<name>[A-Za-z][A-Za-z0-9_-]{{0,{_NAME_PREFIX_MAX_LEN}}}[-_]{_SECRET_NAME_SUFFIX})"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\S+)",
    re.IGNORECASE,
)
"""`NAME=value` / `NAME: value` 形式(NAME が *_TOKEN 等)。

IMPORTANT 6 の修正: セパレータを `_` だけでなく `[-_]`(ハイフンも可)にした。
`X-Api-Key: SECRETVAL` / `x-api-key: SECRETVAL` / `X-Auth-Token: ...` /
`api-key=...` のようなハイフン区切りは HTTP ヘッダ名の実世界での主流の綴りであり、
httpx 等が `request.headers` をハイフン区切りでレンダリングするため、M3 の
esa/Web アダプタがヘッダをログへ流す経路でこれを見逃すと資格情報全体が漏れる。
"""

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
    rf"(?P<prefix>[A-Za-z][A-Za-z0-9+.-]{{0,{_NAME_PREFIX_MAX_LEN}}}://[^\s:/@]+:)"
    r"(?P<password>[^@\s/]+)(?P<at_host>@)"
)
"""`scheme://user:pass@host` の pass 部のみ(scheme・user・host は保持)。

I6 の作業中に発見した既存の ReDoS(このパターン自体は本レビューの指摘対象外だが、
I6 と全く同じ「区切り文字を含む文字クラスの無制限量指定子 + その直後に必須の
リテラル」という構造だった)。スキーム部の量指定子が無制限の `*` だったため、
`://` を含まない長いハイフン/ドット区切り文字列(バージョン文字列・kebab-case の
識別子等、ログに現れて不思議のないもの)に対して二次関数的なバックトラックが
発生し、実測で128,000文字が8.8秒も応答を止めていた(64,000文字でも2.1秒)。
`_NAME_PREFIX_MAX_LEN` と同じ上限で束縛し、線形の計算量に戻す。実在する URL
スキームがこの上限を超えることは無い。
"""

_JSON_SECRET_RE = re.compile(
    rf'(?P<key>"[A-Za-z][A-Za-z0-9_-]{{0,{_NAME_PREFIX_MAX_LEN}}}[-_]{_SECRET_NAME_SUFFIX}")'
    r'(?P<sep>\s*:\s*)"(?P<value>[^"]*)"',
    re.IGNORECASE,
)
"""JSON の `"api_key": "..."` / `"x-api-key": "..."` 系の値部分のみ。

`_KEY_VALUE_RE` と同様、suffix の直前に `-` または `_` を要求する語境界の制約を
課している。これが無いと `monkey` `donkey` `jockey` のように「たまたま key 等で
終わる」だけの無害な JSON キーの値まで `***` に潰してしまう(このツールは任意の
ユーザー/API の JSON をログへ通すため、正当な内容の無言破壊は診断の信頼性を
損なう)。`{0,64}` の上限は `_KEY_VALUE_RE` と同じ ReDoS 対策。
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
    ロギングに限らず、CLI のエラー表示・トレースバックなど、秘密情報が
    混入しうるあらゆる文字列表示経路から呼び出せる純粋関数(ログ設定や
    `logging` モジュールへの依存を持たない)。
    """
    masked = text
    for pattern, replacement in SECRET_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


__all__ = ["SECRET_PATTERNS", "mask_secrets"]
