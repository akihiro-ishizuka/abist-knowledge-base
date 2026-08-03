"""検索クエリからの語抽出(設計書 §9.2、`design/plans/M4-index-search.md` Task3 Step1)。

日本語は空白で区切られないため、漢字・カタカナの連なりと ASCII 語を正規表現で
切り出す。旧 `tools/lib/search-engine.js` の `extractQueryTerms`/`identifierTerms`/
`isPureIdentifierQuery` の移植であり、抽出順序・ストップワード・「ひらがなは
抽出しない」という仕様をそのまま踏襲する(旧版の実測に基づく調整であって、
ここで「直す」対象ではない)。

**`\\d` は使わない**: Python の `\\d` は Unicode の全角数字(`１２３`)にもマッチするが
JavaScript の `\\d` は ASCII の `0-9` にしかマッチしない。旧版と同じ挙動にするため
`[0-9]` を使う(M2 で全角数字が誤って ASCII 数字と同じ扱いを受け、文書の分類元が
黙って変わった実例がある)。
"""

from __future__ import annotations

import re

#: `#398` のような投稿番号参照。ASCII 数字のみ(`\d` は使わない)。
_HASH_NUMBER_RE = re.compile(r"#[0-9]+")

#: ドット/アンダースコア連結識別子(`Selection.Search`,
#: `shrink_clamp_bellow_overlap_mm` など)。1文字以上の区切りが1回以上続く形。
_DOTTED_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:[._][A-Za-z0-9_]+)+")

#: 英数語(`CATIA`, `PySide6` など)。
_ALNUM_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")

#: 漢字2文字以上(CJK統合漢字 U+4E00-U+9FFF)。
_KANJI_RE = re.compile(r"[一-鿿]{2,}")

#: カタカナ2文字以上(U+30A1-U+30F6 + 長音記号 U+30FC)。
_KATAKANA_RE = re.compile(r"[ァ-ヶー]{2,}")

#: `φ9`/`Φ 9.5` のような寸法表記。内部の空白は抽出後に除去する。ASCII 数字のみ。
_PHI_DIMENSION_RE = re.compile(r"[φΦ]\s*[0-9.]+")

#: 空白除去用(φ寸法表記の内部空白を潰す)。
_WHITESPACE_RE = re.compile(r"\s+")

#: 疑問表現・抽象語のストップワード(15語)。ひらがなは他の抽出パターンでは
#: 一切拾わないため、ここに列挙された語のみが理論上の対象になる。
STOPWORDS: frozenset[str] = frozenset(
    {
        "どう",
        "どこ",
        "なぜ",
        "いつ",
        "場合",
        "方法",
        "手段",
        "内容",
        "対応",
        "よい",
        "ある",
        "する",
        "いる",
        "こと",
        "もの",
        "ため",
    }
)

#: 日本語(漢字・ひらがな・カタカナ)を含むかの判定に使う。
_JAPANESE_CHAR_RE = re.compile(r"[一-鿿ぁ-んァ-ヶ]")

#: 識別子とみなせる語(完全一致ブーストの対象)。
_HASH_NUMBER_FULL_RE = re.compile(r"^#[0-9]+$")
_UNDERSCORE_OR_DOT_RE = re.compile(r"[_.]")
_ALNUM_WORD_FULL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def extract_query_terms(query: object) -> list[str]:
    """質問文から検索語を取り出す(順序を保って重複除去、長さ1とストップワードを除く)。

    抽出順序は固定(旧版の仕様どおり):
    1. `#数字`
    2. ドット/アンダースコア連結識別子
    3. 英数語
    4. 漢字2文字以上
    5. カタカナ2文字以上
    6. φ寸法表記
    """
    text = str(query)
    terms: list[str] = []

    terms.extend(m.group(0) for m in _HASH_NUMBER_RE.finditer(text))
    terms.extend(m.group(0) for m in _DOTTED_IDENTIFIER_RE.finditer(text))
    terms.extend(m.group(0) for m in _ALNUM_WORD_RE.finditer(text))
    terms.extend(m.group(0) for m in _KANJI_RE.finditer(text))
    terms.extend(m.group(0) for m in _KATAKANA_RE.finditer(text))
    terms.extend(_WHITESPACE_RE.sub("", m.group(0)) for m in _PHI_DIMENSION_RE.finditer(text))

    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        if len(term) >= 2 and term not in STOPWORDS:
            result.append(term)
    return result


def identifier_terms(query: object) -> list[str]:
    """識別子とみなせる語(完全一致ブーストの対象)。"""
    return [
        term
        for term in extract_query_terms(query)
        if _HASH_NUMBER_FULL_RE.match(term)
        or _UNDERSCORE_OR_DOT_RE.search(term)
        or _ALNUM_WORD_FULL_RE.match(term)
    ]


def is_pure_identifier_query(query: object) -> bool:
    """識別子だけのクエリか(`#398` / `PySide6` / `shrink_clamp_bellow_overlap_mm` など)。

    この種のクエリは「その語を含む文書」が欲しいのであって、意味が近い文書は
    要らない。ベクトル検索を混ぜると完全一致が意味的近傍に薄められ、旧版の実測で
    識別子クエリの Recall@5 が 1.000 から 0.917 に落ちた。ベクトル検索を丸ごと
    スキップするのはこの実測に基づく意図的なチューニングである。
    """
    text = str(query).strip()
    if not text:
        return False
    # 日本語が含まれていれば自然文として扱う
    if _JAPANESE_CHAR_RE.search(text):
        return False

    terms = extract_query_terms(text)
    if not terms:
        return False
    identifiers = set(identifier_terms(text))
    return all(term in identifiers for term in terms)


__all__ = [
    "STOPWORDS",
    "extract_query_terms",
    "identifier_terms",
    "is_pure_identifier_query",
]
