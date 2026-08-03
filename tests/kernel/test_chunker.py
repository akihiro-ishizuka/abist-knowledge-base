"""`chunker.py` の M1 ゴールデン(`tests/fixtures/kernel/chunker.json`)照合テスト。

fixture は旧 `tools/lib/chunker.js` を実行して得たゴールデン値であり、テストが
落ちたら疑うべきは実装であって fixture ではない。

fixture のキーは Node 由来の camelCase(`maxTokens` 等)、Python 側 API は
snake_case のため、ここで明示的にマッピングする。

24ケースの内訳:
  - 構造ケース(`input_b64` + `input_options` + `expected.chunkCount`/`chunks`) 17件
  - `default_chunk_options`(既定値そのものの確認) 1件
  - `estimate_tokens_*`(トークン推定の単体確認) 6件

加えて、合成ケースだけでは拾えない実データ固有の構造(深い見出し、混在フェンス、
巨大表、BOM+CRLF)を検証するため、`tests/fixtures/real-docs/samples.json` の
40件についても `chunker` の出力を照合する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from abist_kb.infrastructure.search.chunker import (
    DEFAULT_CHUNK_OPTIONS,
    Chunk,
    ChunkOptions,
    chunk_markdown,
    estimate_tokens,
)
from conftest import b64d, load_kernel_fixture

FIXTURE = load_kernel_fixture("chunker")
CASES = {case["id"]: case for case in FIXTURE["cases"]}

STRUCTURE_CASE_IDS = [
    "heading_structure",
    "start_end_line",
    "frontmatter_line_count",
    "no_heading",
    "empty_string",
    "empty_frontmatter_only",
    "empty_whitespace_only",
    "code_block_not_split",
    "table_not_split",
    "code_block_fake_heading",
    "long_section_split",
    "maxtokens_small_100",
    "maxtokens_large_2000",
    "heading_path_consistency",
    "identifiers_preserved",
    "duplicate_content_hash",
    "crlf_line_numbers",
]

ESTIMATE_TOKENS_CASE_IDS = [
    "estimate_tokens_empty",
    "estimate_tokens_japanese",
    "estimate_tokens_english",
    "estimate_tokens_japanese_100",
    "estimate_tokens_japanese_10",
    "estimate_tokens_mixed_cjk_ascii",
]


def _options(input_options: dict[str, Any] | None) -> ChunkOptions:
    input_options = input_options or {}
    return ChunkOptions(
        max_tokens=input_options.get("maxTokens", DEFAULT_CHUNK_OPTIONS.max_tokens),
        hard_max_tokens=input_options.get("hardMaxTokens", DEFAULT_CHUNK_OPTIONS.hard_max_tokens),
        min_tokens=input_options.get("minTokens", DEFAULT_CHUNK_OPTIONS.min_tokens),
    )


def _assert_chunk_matches(chunk: Chunk, expected: dict[str, Any]) -> None:
    assert chunk.index == expected["index"]
    assert chunk.heading_path == expected["heading_path"]
    assert chunk.text == b64d(expected["text_b64"])
    assert chunk.start_line == expected["start_line"]
    assert chunk.end_line == expected["end_line"]
    assert chunk.token_estimate == expected["token_estimate"]
    assert chunk.content_hash == expected["content_hash"]
    assert chunk.split_by_size == expected["split_by_size"]


@pytest.mark.parametrize("case_id", STRUCTURE_CASE_IDS)
def test_structure_cases(case_id: str) -> None:
    case = CASES[case_id]
    text = b64d(case["input_b64"]) if case["input_b64"] else ""
    options = _options(case.get("input_options"))
    expected = case["expected"]

    chunks = chunk_markdown(text, options)

    assert len(chunks) == expected["chunkCount"]
    for chunk, expected_chunk in zip(chunks, expected["chunks"], strict=True):
        _assert_chunk_matches(chunk, expected_chunk)


@pytest.mark.parametrize("case_id", ESTIMATE_TOKENS_CASE_IDS)
def test_estimate_tokens_cases(case_id: str) -> None:
    case = CASES[case_id]
    text = b64d(case["input_b64"]) if case["input_b64"] else ""
    assert estimate_tokens(text) == case["expected"]["tokens"]


def test_default_chunk_options() -> None:
    case = CASES["default_chunk_options"]
    expected = case["expected"]["value"]
    assert DEFAULT_CHUNK_OPTIONS.max_tokens == expected["maxTokens"]
    assert DEFAULT_CHUNK_OPTIONS.hard_max_tokens == expected["hardMaxTokens"]
    assert DEFAULT_CHUNK_OPTIONS.min_tokens == expected["minTokens"]


def test_all_fixture_cases_covered() -> None:
    """fixture の全24ケースがこのテストファイルで参照されていることの網羅性チェック。"""
    covered = set(STRUCTURE_CASE_IDS) | set(ESTIMATE_TOKENS_CASE_IDS) | {"default_chunk_options"}
    assert set(CASES) == covered
    assert len(FIXTURE["cases"]) == 24


# ---------------------------------------------------------------------------
# 実データ40件(`tests/fixtures/real-docs/samples.json`)によるチャンク検証。
#
# 合成ケースでは現れない実データ固有の構造(深い見出しネスト、混在フェンス、
# 巨大な表、BOM+CRLF の組み合わせ)を突く。ここで落ちる場合、実装を疑うこと
# (fixture は旧実装を実行して得たゴールデン値のため)。
# ---------------------------------------------------------------------------

REAL_DOCS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "real-docs" / "samples.json"
REAL_DOCS = json.loads(REAL_DOCS_PATH.read_text(encoding="utf-8"))
REAL_DOCS_CASES = {case["id"]: case for case in REAL_DOCS["cases"]}


@pytest.mark.parametrize("case_id", sorted(REAL_DOCS_CASES))
def test_real_docs_chunker(case_id: str) -> None:
    case = REAL_DOCS_CASES[case_id]
    raw = b64d(case["expected"]["frontmatter"]["raw_b64"])
    expected = case["expected"]["chunker"]

    chunks = chunk_markdown(raw, DEFAULT_CHUNK_OPTIONS)

    assert len(chunks) == expected["chunkCount"], case["path"]
    for chunk, expected_chunk in zip(chunks, expected["chunks"], strict=True):
        _assert_chunk_matches(chunk, expected_chunk)


def test_real_docs_fixture_has_40_cases() -> None:
    assert len(REAL_DOCS["cases"]) == 40
