"""B32doc(参照コーパス)の索引対象判定。

旧実装 `tools/lib/b32doc-filter.js` の移植。

**なぜ `tools/knowledge-curator/filters.py` ではなく `b32doc-filter.js` を移植するのか。**
`b32doc-filter.js` の冒頭コメントは「元の判定と食い違いが出たら `filters.py` 側を正と
すること」と自称している。しかし `tests/fixtures/PROVENANCE.md` §3 が実測で記録した
とおり、これは誤りである:

  1. `filters.py` が持つ `decide()` 関数(判定優先順位を1つにまとめた関数)は、実際に
     `procedures-index.jsonl` を生成する `build_index.py` から一度も呼ばれない
     **死んだコード**であり、`not_html_category` を最初に判定する優先順位を持つ。
  2. 実際にインデックスを生成する `build_index.py` 本体は、`decide()` とは異なる
     優先順位(`is_excluded_path` を最初に呼ぶ)で個々の判定関数を直接呼んでいる。
  3. `b32doc-filter.js` の `decide_indexable()` はこの (2) の**実際に実行される**
     優先順位と一致し、(1) の未使用の `decide()` とは一致しない。

したがって「JS が Python の正を移植し損ねている」のではなく、「JS は実際に実行される
Python の挙動と一致しており、コメントが指す `filters.py` 内の `decide()` の方が
死んだコードで参照する価値がない」。M4 の reference-index 選定はこの JS の優先順位
(= `build_index.py` 本体の実際の挙動)を再現しなければならない。この事実は
`tests/fixtures/kernel/b32doc-filter.json` の `js_vs_python_priority_order_divergence` /
`js_vs_python_noise_filename_source_divergence` ケースに、両実装を実際に実行して得た
値として記録されている。**将来 `filters.py` を読んで「JSの方が間違っている」と早合点し、
ここを `decide()` の優先順位へ「修正」しないこと。**

もう1点、目次/ランディングページ判定(`toc_or_default`)は frontmatter の
`source_name`(無ければディスク上のファイル名にフォールバック)を見る。これは
`build_index.py` が実際に見ているディスク上のファイル名だけの判定とも食い違いうるが
(`js_vs_python_noise_filename_source_divergence` 参照)、本移植は `b32doc-filter.js` の
フォールバック込みの挙動をそのまま再現する(呼び出し側が `source_name` を渡さなければ
`path.basename` 相当にフォールバックする)。

B32doc の実ファイルは全て CRLF。改行を正規化(`\\r\\n` -> `\\n` など)すると、本文の
区切りに使う固定セクション見出し(`## Summary Keys` 等)の行末に `\\r` が残ったまま
`$` を含む判定に失敗する経路が JS 側にあったため、Python でも行分割時に各行末の `\\r`
を明示的に落とす(改行コード自体の正規化はしない)。
"""

from __future__ import annotations

from dataclasses import dataclass

# パスに含まれていたら除外するセグメント(アイコン・画像・サンプル・ナビゲーション)
EXCLUDED_PATH_SEGMENTS: tuple[str, ...] = (
    "/icons_C2/",
    "/images/",
    "/samples/",
    "/control/",
    "/navigation/",
)

# ファイル名で除外する部分一致(目次・ランディングページ)
NOISE_FILENAME_SUBSTR: tuple[str, ...] = ("toc.htm", "default.htm")

INDEXABLE_CATEGORIES: frozenset[str] = frozenset({"html"})
INDEXABLE_LANGUAGES: frozenset[str] = frozenset({"ja", "mixed"})
EMPTY_CONTENT_MARKER = "(抽出されたコンテンツなし)"
MIN_CONTENT_CHARS = 200

# 本文を区切る固定セクション見出し(build_index.py の SECTION_HEADERS と同一)
SECTION_HEADERS: frozenset[str] = frozenset(
    {
        "## Summary Keys",
        "## Extracted Content",
        "## Structured Data",
        "## Links",
    }
)


def _to_lines(body: str | None) -> list[str]:
    """本文を行に分ける。各行末の `\\r` を落とす(B32docは全CRLFのため)。"""
    text = body if isinstance(body, str) else ""
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def extracted_content(body: str | None) -> str:
    """本文から `## Extracted Content` セクションを取り出す。

    区切りは固定セクション見出しのみ。抽出本文の中に `##` 見出しが現れても切らない。
    """
    lines = _to_lines(body)
    start = -1
    for i, line in enumerate(lines):
        if line.strip() == "## Extracted Content":
            start = i
            break
    if start == -1:
        return ""

    rest = lines[start + 1 :]
    end = -1
    for i, line in enumerate(rest):
        if line.strip() in SECTION_HEADERS:
            end = i
            break
    section = rest if end == -1 else rest[:end]
    joined = "\n".join(section)
    return joined.strip("\n")


@dataclass(frozen=True, slots=True)
class SummaryKeys:
    """`extract_summary_keys` の戻り値。"""

    title: str | None
    summary: str | None


_TITLE_PREFIX = "タイトル:"
_SUMMARY_PREFIX = "概要:"


def extract_summary_keys(body: str | None) -> SummaryKeys:
    """`## Summary Keys` からタイトルと概要を取り出す。

    B32doc の frontmatter には title が無い(source_name などのみ)。実際のタイトルは
    Summary Keys セクションに書かれている。
    """
    lines = _to_lines(body)
    start = -1
    for i, line in enumerate(lines):
        if line.strip() == "## Summary Keys":
            start = i
            break
    if start == -1:
        return SummaryKeys(title=None, summary=None)

    rest = lines[start + 1 :]
    end = -1
    for i, line in enumerate(rest):
        if line.strip() in SECTION_HEADERS:
            end = i
            break
    section = rest if end == -1 else rest[:end]

    title: str | None = None
    summary: str | None = None
    for line in section:
        stripped = line.strip()
        if stripped.startswith("-"):
            item = stripped[1:].strip()
        else:
            continue
        if item.startswith(_TITLE_PREFIX):
            value = item[len(_TITLE_PREFIX) :].strip()
            title = value or None
            continue
        if item.startswith(_SUMMARY_PREFIX):
            value = item[len(_SUMMARY_PREFIX) :].strip()
            summary = value or None

    return SummaryKeys(title=title, summary=summary)


@dataclass(frozen=True, slots=True)
class IndexDecision:
    """`decide_indexable` の戻り値。"""

    indexable: bool
    reason: str | None


def decide_indexable(
    *,
    rel_posix: str,
    source_name: str | None = None,
    category: str | None = None,
    language: str | None = None,
    body: str | None = None,
) -> IndexDecision:
    """索引対象かを判定する。判定順序は `build_index.py` 本体の実際の呼び出し順と一致させる
    (モジュール docstring 参照。`filters.py` の未使用 `decide()` の順とは異なる)。
    """
    for segment in EXCLUDED_PATH_SEGMENTS:
        if segment in rel_posix:
            return IndexDecision(indexable=False, reason="path_excluded")

    name = str(source_name) if source_name else rel_posix.rsplit("/", 1)[-1]
    name = name.lower()
    for substr in NOISE_FILENAME_SUBSTR:
        if substr in name:
            return IndexDecision(indexable=False, reason="toc_or_default")

    if str(category) not in INDEXABLE_CATEGORIES:
        return IndexDecision(indexable=False, reason="not_html_category")
    if str(language) not in INDEXABLE_LANGUAGES:
        return IndexDecision(indexable=False, reason="language_excluded")

    extracted = extracted_content(body).strip()
    if not extracted or extracted == EMPTY_CONTENT_MARKER:
        return IndexDecision(indexable=False, reason="empty_content")
    if len(extracted) < MIN_CONTENT_CHARS:
        return IndexDecision(indexable=False, reason="too_short")

    return IndexDecision(indexable=True, reason=None)


__all__ = [
    "EMPTY_CONTENT_MARKER",
    "EXCLUDED_PATH_SEGMENTS",
    "INDEXABLE_CATEGORIES",
    "INDEXABLE_LANGUAGES",
    "MIN_CONTENT_CHARS",
    "NOISE_FILENAME_SUBSTR",
    "SECTION_HEADERS",
    "IndexDecision",
    "SummaryKeys",
    "decide_indexable",
    "extract_summary_keys",
    "extracted_content",
]
