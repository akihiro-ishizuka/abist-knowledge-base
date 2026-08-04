"""`b32doc_filter.py` の M1 ゴールデン(`tests/fixtures/kernel/b32doc-filter.json`)照合テスト。

fixture は旧 `tools/lib/b32doc-filter.js` を実行して得たゴールデン値であり、テストが
落ちたら疑うべきは実装で fixture ではない。

`js_vs_python_priority_order_divergence` / `js_vs_python_noise_filename_source_divergence`
の2ケースは、`tools/lib/b32doc-filter.js` と旧 `tools/knowledge-curator/filters.py` の
`decide()` 関数(死んだコード)との既知の食い違いを記録したものである。本実装は
`b32doc-filter.js`(= 実際に索引を生成する `build_index.py` 本体と一致する挙動)を
移植しているため、これらのケースは「JS側の挙動を再現できていること」を確認する
(`filters.py` の `decide()` の挙動とは意図的に一致しない)。詳細は
`b32doc_filter.py` のモジュール docstring と `tests/fixtures/PROVENANCE.md` §3。
"""

from __future__ import annotations

import base64

import pytest

from abist_kb.domain.b32doc_filter import (
    EMPTY_CONTENT_MARKER,
    EXCLUDED_PATH_SEGMENTS,
    INDEXABLE_CATEGORIES,
    INDEXABLE_LANGUAGES,
    MIN_CONTENT_CHARS,
    NOISE_FILENAME_SUBSTR,
    SECTION_HEADERS,
    decide_indexable,
    extract_summary_keys,
    extracted_content,
)
from abist_kb.domain.frontmatter import parse_frontmatter
from conftest import load_kernel_fixture

FIXTURE = load_kernel_fixture("b32doc-filter")
CASES = {case["id"]: case for case in FIXTURE["cases"]}
_case_count = len(FIXTURE["cases"])
# 43 -> 46: M1タスク7で real_doc_accept_00〜02(accept経路の実文書サンプル)を追加。
# 旧 real_docs_sample(先頭8件)が全件 reject だったギャップを埋めるための追加採取
# (tests/fixtures/PROVENANCE.md 参照)。
assert _case_count == 46, f"想定46ケースに対し {_case_count} 件しか読み込めていない"


def _b64d(s: str) -> str:
    return base64.b64decode(s).decode("utf-8")


# B32doc の実ファイルは全て CRLF。旧テスト(test/b32doc-filter.test.js)の SAMPLE と同一。
_SAMPLE_LINES = [
    "# Abaqus for CATIA V5 自動インタフェースを使用する例",
    "",
    "## Summary Keys",
    "",
    "- タイトル: Abaqus for CATIA V5 自動インタフェースを使用する例",
    "- 概要: この節の最後に示されているスクリプトは…",
    "",
    "## Extracted Content",
    "",
    "あ" * 300,
    "",
    "## Links",
    "",
    "- http://example.com",
    "",
]
SAMPLE = "\r\n".join(_SAMPLE_LINES)

_BASELINE = {
    "relPosix": "/knowledge/B32doc/md_out/online/Japanese/x_C2/a.htm.htm.abc.md",
    "sourceName": "a.htm",
    "category": "html",
    "language": "ja",
    "body": SAMPLE,
}


def _decide(**overrides: object):
    merged = {**_BASELINE, **overrides}
    return decide_indexable(
        rel_posix=merged["relPosix"],
        source_name=merged["sourceName"],
        category=merged["category"],
        language=merged["language"],
        body=merged["body"],
    )


# --- 定数 -----------------------------------------------------------------------


def test_excluded_path_segments() -> None:
    assert list(EXCLUDED_PATH_SEGMENTS) == CASES["excluded_path_segments"]["expected"]["value"]


def test_noise_filename_substr() -> None:
    assert list(NOISE_FILENAME_SUBSTR) == CASES["noise_filename_substr"]["expected"]["value"]


def test_indexable_categories() -> None:
    expected = CASES["indexable_categories"]["expected"]["value"]
    assert sorted(INDEXABLE_CATEGORIES) == sorted(expected)


def test_indexable_languages() -> None:
    assert sorted(INDEXABLE_LANGUAGES) == sorted(CASES["indexable_languages"]["expected"]["value"])


def test_empty_content_marker() -> None:
    assert CASES["empty_content_marker"]["expected"]["value"] == EMPTY_CONTENT_MARKER


def test_min_content_chars() -> None:
    assert CASES["min_content_chars"]["expected"]["value"] == MIN_CONTENT_CHARS


def test_section_headers() -> None:
    assert sorted(SECTION_HEADERS) == sorted(CASES["section_headers"]["expected"]["value"])


# --- extract_summary_keys / extracted_content -------------------------------------


def test_extract_summary_keys_crlf_sample() -> None:
    case = CASES["extract_summary_keys_crlf_sample"]
    body = _b64d(case["input_b64"])
    result = extract_summary_keys(body)
    assert result.title == case["expected"]["title"]
    assert result.summary == case["expected"]["summary"]


def test_extract_summary_keys_lf_equivalence() -> None:
    case = CASES["extract_summary_keys_lf_equivalence"]
    body = _b64d(case["input_b64"])
    result = extract_summary_keys(body)
    assert result.title == case["expected"]["title"]
    assert result.summary == case["expected"]["summary"]
    crlf_result = extract_summary_keys(body.replace("\n", "\r\n"))
    assert (result.title, result.summary) == (crlf_result.title, crlf_result.summary)
    assert case["expected"]["equalsCrlfResult"] is True


def test_extract_summary_keys_missing_section() -> None:
    case = CASES["extract_summary_keys_missing_section"]
    body = _b64d(case["input_b64"])
    result = extract_summary_keys(body)
    assert result.title == case["expected"]["title"]
    assert result.summary == case["expected"]["summary"]


def test_extracted_content_basic() -> None:
    case = CASES["extracted_content_basic"]
    body = _b64d(case["input_b64"])
    result = extracted_content(body)
    assert result == _b64d(case["expected"]["extracted_b64"])
    assert len(result) == case["expected"]["length"]


def test_extracted_content_not_cut_by_inline_heading() -> None:
    case = CASES["extracted_content_not_cut_by_inline_heading"]
    body = _b64d(case["input_b64"])
    result = extracted_content(body)
    assert result == _b64d(case["expected"]["extracted_b64"])
    assert case["expected"]["includesInlineHeadingLine"] is True


def test_extracted_content_missing_section() -> None:
    case = CASES["extracted_content_missing_section"]
    body = _b64d(case["input_b64"])
    result = extracted_content(body)
    assert result == _b64d(case["expected"]["extracted_b64"])


# --- decide_indexable(ルール別) ---------------------------------------------------

DECIDE_CASE_IDS = [
    "decide_accept_baseline",
    "decide_reject_path_excluded_0_icons_C2",
    "decide_reject_path_excluded_1_images",
    "decide_reject_path_excluded_2_samples",
    "decide_reject_path_excluded_3_control",
    "decide_reject_path_excluded_4_navigation",
    "decide_accept_path_not_excluded",
    "decide_reject_toc_filename",
    "decide_reject_default_filename",
    "decide_accept_normal_filename",
    "decide_reject_not_html_category",
    "decide_accept_html_category",
    "decide_reject_language_en",
    "decide_accept_language_mixed",
    "decide_accept_language_ja",
    "decide_reject_empty_content_marker",
    "decide_reject_no_extracted_content_section",
    "decide_reject_too_short_boundary_minus_1",
    "decide_accept_too_short_boundary_exact",
    "decide_reject_too_short_despite_long_summary_keys",
]


@pytest.mark.parametrize("case_id", DECIDE_CASE_IDS)
def test_decide_indexable(case_id: str) -> None:
    case = CASES[case_id]
    input_data = {k: v for k, v in case["input"].items() if not k.startswith("_")}
    result = _decide(**input_data)
    assert result.indexable == case["expected"]["indexable"]
    assert result.reason == case["expected"]["reason"]


# --- JS/Python の既知の食い違い ----------------------------------------------------


def test_js_vs_python_priority_order_divergence() -> None:
    """JS(=build_index.py本体の実際の優先順位)は path_excluded を最初に判定する。

    filters.py の未使用 decide() は not_html_category を最初に判定するため、
    この入力(category='text' かつ path_excluded対象)では両者の理由が食い違う。
    本実装は JS 側の理由(path_excluded)を返さなければならない。
    """
    case = CASES["js_vs_python_priority_order_divergence"]
    input_data = case["input"]
    result = decide_indexable(
        rel_posix=input_data["relPosix"],
        source_name=input_data.get("sourceName"),
        category=input_data["category"],
        language=input_data["language"],
        body=input_data["body"],
    )
    expected = case["expected"]
    assert result.reason == expected["js_decideIndexable_reason"]
    assert result.indexable is False
    # 記録されている食い違いの構造そのものも確認しておく(再発見時の手がかり)。
    assert expected["python_build_index_actual_order_reason"] == "path_excluded"
    assert expected["python_filters_decide_reason"] == "not_html_category"
    assert expected["divergence"] == {
        "js_vs_build_index_actual": False,
        "js_vs_filters_decide": True,
    }


def test_js_vs_python_noise_filename_source_divergence() -> None:
    """toc/default 判定は frontmatter の source_name を見る(無ければファイル名)。

    ディスク上のファイル名(disk_name)自体は noisy でなくても、frontmatter の
    source_name(fm_source_name)が noisy な値であれば、本実装は source_name を
    優先して noisy と判定する(b32doc-filter.js の decideIndexable() の挙動どおり)。
    """
    case = CASES["js_vs_python_noise_filename_source_divergence"]
    input_data = case["input"]

    via_disk = decide_indexable(
        rel_posix=_BASELINE["relPosix"],
        source_name=input_data["disk_name"],
        category="html",
        language="ja",
        body=SAMPLE,
    )
    via_frontmatter = decide_indexable(
        rel_posix=_BASELINE["relPosix"],
        source_name=input_data["fm_source_name"],
        category="html",
        language="ja",
        body=SAMPLE,
    )

    expected = case["expected"]
    disk_expected = expected["python_noise_via_disk_filename"]
    fm_expected = expected["python_noise_via_frontmatter_source_name"]
    assert via_disk.reason == disk_expected["reason"]
    assert via_disk.indexable == (not disk_expected["noisy"])
    assert via_frontmatter.reason == fm_expected["reason"]
    assert via_frontmatter.indexable == (not fm_expected["noisy"])


# --- 実文書8件 ---------------------------------------------------------------------

REAL_DOC_CASE_IDS = [f"real_doc_{i:02d}" for i in range(8)]
# M1タスク7で追加: real_docs_sample(先頭8件)が全件 reject だったギャップを埋める、
# accept経路(indexable=True)の実文書サンプル3件。
REAL_DOC_ACCEPT_CASE_IDS = [f"real_doc_accept_{i:02d}" for i in range(3)]


def _resolve_title(summary_title: str | None, frontmatter_title: object, path: str) -> str:
    if summary_title:
        return summary_title
    if isinstance(frontmatter_title, str):
        return frontmatter_title
    basename = path.rsplit("/", 1)[-1]
    return basename[:-3] if basename.endswith(".md") else basename


@pytest.mark.parametrize("case_id", [*REAL_DOC_CASE_IDS, *REAL_DOC_ACCEPT_CASE_IDS])
def test_real_doc(case_id: str) -> None:
    case = CASES[case_id]
    content = _b64d(case["content_b64"])
    assert len(content.encode("utf-8")) == case["byte_size"]

    parsed = parse_frontmatter(content)
    data = parsed.data
    assert str(data.get("category")) == case["frontmatter_category"] or (
        data.get("category") is None and case["frontmatter_category"] is None
    )
    assert data.get("language") == case["frontmatter_language"]
    assert data.get("source_name") == case["frontmatter_source_name"]

    decision = decide_indexable(
        rel_posix=case["path"],
        source_name=data.get("source_name"),
        category=data.get("category"),
        language=data.get("language"),
        body=parsed.body,
    )
    expected = case["expected"]
    assert decision.indexable == expected["decision"]["indexable"]
    assert decision.reason == expected["decision"]["reason"]

    summary = extract_summary_keys(parsed.body)
    assert summary.title == expected["summaryTitle"]

    resolved_title = _resolve_title(summary.title, data.get("title"), case["path"])
    assert resolved_title == expected["resolvedTitle"]


def test_all_fixture_cases_covered() -> None:
    """fixture の全46ケースがこのテストファイルで参照されていることの網羅性チェック。"""
    covered = {
        "excluded_path_segments",
        "noise_filename_substr",
        "indexable_categories",
        "indexable_languages",
        "empty_content_marker",
        "min_content_chars",
        "section_headers",
        "extract_summary_keys_crlf_sample",
        "extract_summary_keys_lf_equivalence",
        "extract_summary_keys_missing_section",
        "extracted_content_basic",
        "extracted_content_not_cut_by_inline_heading",
        "extracted_content_missing_section",
        *DECIDE_CASE_IDS,
        "js_vs_python_priority_order_divergence",
        "js_vs_python_noise_filename_source_divergence",
        *REAL_DOC_CASE_IDS,
        *REAL_DOC_ACCEPT_CASE_IDS,
    }
    assert covered == set(CASES)
