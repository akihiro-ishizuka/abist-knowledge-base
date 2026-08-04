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


# ---------------------------------------------------------------------------
# I4 レビュー指摘の回帰テスト: fixture 非依存の CR-only / 二重CR ケース。
#
# JS の正規表現エンジンは `\r` を `\n` と並ぶ行終端文字として扱うため、
# multiline でなくても `.` は `\r` を跨がない。`chunk_markdown` の冒頭で
# 行ごとに末尾の `\r` を1個だけ剥がす処理(JS `chunker.js:248` と同一)の後、
# 二重CR(`\r\r\n` のような入力)を含む行には `\r` が1個残ったまま行末に来る。
# Python の `.` は既定で `\r` を跨ぐため、この残存 `\r` を見出しタイトルに
# 取り込んでしまい、JS では見出しとして認識されない行を見出しとして扱って
# しまう(構造そのものが分岐し `content_hash` も変わる)。
# ---------------------------------------------------------------------------


def test_doubled_cr_before_heading_is_not_recognized_as_heading() -> None:
    """二重CR(`# Title\\r\\r\\n`)の見出しは、1回だけのCR除去後も `\\r` が
    残るため、JS 同様に見出しとして認識されない(先頭の空節に落ちる)。
    """
    doc = "# Title\r\r\nbody text\r\n"
    chunks = chunk_markdown(doc, DEFAULT_CHUNK_OPTIONS)
    assert len(chunks) == 1
    assert chunks[0].heading_path == ""
    # 見出し行として解釈されなかった分、本文にそのまま含まれる(残存 \r ごと)。
    assert chunks[0].text == "# Title\r\nbody text"


def test_doubled_cr_interior_to_heading_breaks_recognition_after_first_occurrence() -> None:
    """複数の見出しがあるとき、二重CRを含む見出し行だけが認識されなくなり、
    それ以外の(単一CR/CRLFの)見出しは通常通り認識される。
    """
    doc = "# Top\r\n## Sub\r\r\nbody\r\n"
    chunks = chunk_markdown(doc, DEFAULT_CHUNK_OPTIONS)
    # "# Top" は通常のCRLFなので正しく見出しとして認識される。
    # "## Sub\r\r\n" は1回のCR除去後も "## Sub\r" が残り、見出しとして
    # 認識されない(JSの `.` が `\r` を跨がないのと同じ理由)。
    assert [c.heading_path for c in chunks] == ["Top"]
    assert chunks[0].text == "# Top\n## Sub\r\nbody"


def test_bom_only_content_yields_no_chunks_like_js_trim() -> None:
    """レビュー指摘(Minor): JS の `trim()` は U+FEFF(BOM)も空白として扱うため
    `chunkMarkdown('\\ufeff')` は0チャンクになるが、Python の素の `str.strip()`
    はBOMを空白と見なさないため、直したエンティティが `content.strip() == ''`
    をすり抜けて1チャンクになっていた。`js_is_blank` へ寄せることで揃える。
    """
    assert chunk_markdown("﻿", DEFAULT_CHUNK_OPTIONS) == []


def test_content_hash_trims_trailing_whitespace_before_interior_cr() -> None:
    """`_content_hash` の trailing-whitespace除去は、Pythonの `re.MULTILINE`
    の `$`(`\\n` の直前にしか反応しない)ではなく、JS 同様 `\\r`/`\\n`/`\\r\\n`/
    文字列末尾のいずれの直前にも反応しなければならない。
    """
    with_trailing_ws_before_cr = chunk_markdown("# H\r\nline  \r\nmore\r\n", DEFAULT_CHUNK_OPTIONS)
    without_trailing_ws = chunk_markdown("# H\r\nline\r\nmore\r\n", DEFAULT_CHUNK_OPTIONS)
    assert with_trailing_ws_before_cr[0].content_hash == without_trailing_ws[0].content_hash


# ---------------------------------------------------------------------------
# レビュー指摘: mutation testing で未検出のまま生き残った4箇所への直接テスト。
# fixture の合成ケースはたまたま境界値をちょうど突いていなかったため、
# 実装を壊す変異(`>=` <-> `>`、テーブル行ガードの削除)を入れても既存の
# 24ケース + 実データ40件がすべて緑のままだった。ここでは境界値そのものを
# 狙って固定する。
# ---------------------------------------------------------------------------


def test_table_row_split_guard_prevents_split_right_before_table() -> None:
    """`_split_section` の「空行の直後がテーブル行なら分割候補にしない」ガード。

    このガードを外す変異(`not _is_table_row(line)` を削除)をしても、
    fixture の合成ケースだけでは検出できなかった——テーブルを跨ぐ分割が
    起きても `chunkCount` が偶然一致してしまうケースしか無かったため。
    ここでは同一内容で「直後がテーブル行」と「直後が通常行」を比較し、
    ガードが効いているときだけ1チャンクのまま(テーブル境界で割れない)に
    なることを直接示す。
    """
    table_after_blank = (
        "# H\n" + "a" * 40 + "\n\n" + "| col1 | col2 |\n" + "| --- | --- |\n" + "b" * 40 + "\n"
    )
    plain_after_blank = "# H\n" + "a" * 40 + "\n\n" + "not a table row\n" + "b" * 40 + "\n"
    opts = ChunkOptions(max_tokens=12, hard_max_tokens=100000, min_tokens=0)

    table_chunks = chunk_markdown(table_after_blank, opts)
    plain_chunks = chunk_markdown(plain_after_blank, opts)

    # テーブル直前では割れない(ガードが効いている) -> 1チャンクのまま。
    assert len(table_chunks) == 1
    # 通常行なら同じ閾値で普通に割れる(ガードが常に無効化されているわけ
    # ではないことの対照)。
    assert len(plain_chunks) == 2


def test_split_section_max_tokens_ge_boundary() -> None:
    """`_split_section` の候補チェック `estimate_tokens(candidate) >= max_tokens`。

    候補のトークン数がちょうど `max_tokens` と等しいとき、`>=` なら分割される
    (`>` に変異すると分割されなくなる)。`==` に変異した場合との違いを見る
    ため、境界のすぐ下(11トークン)でも分割されることも併せて確認する。
    """
    para1 = "a" * 40
    para2 = "b" * 40
    doc = f"# H\n{para1}\n\n{para2}\n"

    # 見出し込みの1段落目はちょうど12トークン(`# H\n`+40文字+`\n`=45文字 -> ceil(45/4)=12)。
    at_boundary = chunk_markdown(
        doc, ChunkOptions(max_tokens=12, hard_max_tokens=100000, min_tokens=0)
    )
    below_boundary = chunk_markdown(
        doc, ChunkOptions(max_tokens=11, hard_max_tokens=100000, min_tokens=0)
    )
    above_boundary = chunk_markdown(
        doc, ChunkOptions(max_tokens=13, hard_max_tokens=100000, min_tokens=0)
    )

    assert len(at_boundary) == 2  # 12 >= 12 -> 分割される(`>` 変異ならここが1になる)
    assert len(below_boundary) == 2  # 12 >= 11 -> 分割される(`==` 変異ならここが1になる)
    assert len(above_boundary) == 1  # 12 >= 13 は成り立たず分割されない


def test_enforce_hard_limit_ge_boundary() -> None:
    """`_enforce_hard_limit` のバッファ蓄積チェック
    `estimate_tokens(buffer) >= hard_max_tokens`。

    バッファのトークン数がちょうど `hard_max_tokens` と等しいときに区切られる
    (`>` に変異すると、その位置では区切られず次の行まで蓄積されてしまう)。
    """
    doc = "# H\naaaa\nbbbb\ncccc\ndddd\n"
    opts = ChunkOptions(max_tokens=100000, hard_max_tokens=4, min_tokens=0)

    chunks = chunk_markdown(doc, opts)

    assert [c.text for c in chunks] == ["# H\naaaa\nbbbb", "cccc\ndddd"]
    assert [c.token_estimate for c in chunks] == [4, 3]
    assert all(c.split_by_size for c in chunks)
