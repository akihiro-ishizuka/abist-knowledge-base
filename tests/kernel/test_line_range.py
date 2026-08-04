"""`line_range.py` の M1 ゴールデン(`tests/fixtures/kernel/line-range.json`)照合テスト。

fixture は旧 `tools/lib/line-range.js` を実行して得たゴールデン値であり、
テストが落ちたら疑うべきは実装であって fixture ではない。
"""

from __future__ import annotations

import pytest

from abist_kb.domain.line_range import range_hash, slice_range, split_doc_lines
from conftest import b64d, load_kernel_fixture

FIXTURE = load_kernel_fixture("line-range")
CASES = {case["id"]: case for case in FIXTURE["cases"]}


def _text(case: dict) -> str:
    return b64d(case["input_b64"])


@pytest.mark.parametrize("case_id", ["crlf_lf_equivalence_lf", "crlf_lf_equivalence_crlf"])
def test_lines_and_range_hash_1_3(case_id: str) -> None:
    case = CASES[case_id]
    text = _text(case)
    expected = case["expected"]

    assert split_doc_lines(text) == expected["lines"]

    result = range_hash(text, 1, 3)
    expected_hash = expected["rangeHash1_3"]
    assert result.ok is expected_hash["ok"]
    assert result.hash == expected_hash["hash"]
    assert result.total_lines == expected_hash["totalLines"]


def test_bom_stripped_once() -> None:
    case = CASES["bom_stripped_once"]
    text = _text(case)
    expected = case["expected"]

    assert split_doc_lines(text) == expected["lines"]

    result = range_hash(text, 1, 1)
    expected_hash = expected["rangeHash1_1"]
    assert result.ok is expected_hash["ok"]
    assert result.hash == expected_hash["hash"]
    assert result.total_lines == expected_hash["totalLines"]


def test_slice_range_inclusive() -> None:
    case = CASES["slice_range_inclusive"]
    text = _text(case)
    expected = case["expected"]["slice2_3"]

    result = slice_range(text, 2, 3)
    assert result.ok is expected["ok"]
    assert result.text == expected["text"]
    assert result.total_lines == expected["totalLines"]


def test_hash_no_trim() -> None:
    case = CASES["hash_no_trim"]
    text = _text(case)
    expected = case["expected"]["rangeHash1_2"]

    result = range_hash(text, 1, 2)
    assert result.ok is expected["ok"]
    assert result.hash == expected["hash"]
    assert result.total_lines == expected["totalLines"]


def test_single_line_range() -> None:
    case = CASES["single_line_range"]
    text = _text(case)
    expected = case["expected"]["rangeHash1_1"]

    result = range_hash(text, 1, 1)
    assert result.ok is expected["ok"]
    assert result.hash == expected["hash"]
    assert result.total_lines == expected["totalLines"]


def test_out_of_bounds() -> None:
    case = CASES["out_of_bounds"]
    text = _text(case)
    for entry in case["expected"]["results"]:
        expected = entry["result"]
        result = range_hash(text, entry["start"], entry["end"])
        assert result.ok is expected["ok"], (entry["start"], entry["end"])
        assert result.total_lines == expected["totalLines"]
        assert result.reason == expected["reason"]


def test_kb_search_line_split() -> None:
    case = CASES["kb_search_line_split"]
    text = _text(case)
    assert split_doc_lines(text) == case["expected"]["lines"]


@pytest.mark.parametrize(
    "case_id",
    [c["id"] for c in FIXTURE["cases"] if c["id"].startswith("matrix_")],
)
def test_matrix_cases(case_id: str) -> None:
    case = CASES[case_id]
    text = _text(case)
    expected = case["expected"]["rangeHash"]

    # 入力は 4 行の本文 + 末尾改行により split_doc_lines 後は 5 行(5行目は空文字列)。
    if case_id.endswith("_first_line"):
        start, end = 1, 1
    elif case_id.endswith("_last_line"):
        start, end = 5, 5
    elif case_id.endswith("_single_middle_line"):
        start, end = 2, 2
    elif case_id.endswith("_full_range"):
        start, end = 1, 5
    else:
        raise AssertionError(f"unrecognized matrix case id: {case_id}")

    result = range_hash(text, start, end)
    assert result.ok is expected["ok"]
    assert result.hash == expected["hash"]
    assert result.total_lines == expected["totalLines"]


def test_all_fixture_cases_covered() -> None:
    """fixture の全ケースがこのテストファイルで参照されていることの網羅性チェック。"""
    covered_prefixes = (
        "crlf_lf_equivalence_",
        "bom_stripped_once",
        "slice_range_inclusive",
        "hash_no_trim",
        "single_line_range",
        "out_of_bounds",
        "kb_search_line_split",
        "matrix_",
    )
    for case_id in CASES:
        assert case_id.startswith(covered_prefixes), case_id
    assert len(FIXTURE["cases"]) == 24
