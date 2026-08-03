"""実データ40件(`tests/fixtures/real-docs/samples.json`)による四項目の横断照合。

M2 のレビューで指摘された欠落: 各サンプルは `frontmatter` / `chunker` /
`line_range` / `embedding` の4つの期待値グループを持つが、`chunker` は
`test_chunker.py` で照合されていたものの、残り3グループ(特に `embedding`)は
どのテストからも比較されていなかった。`embedding` は M8 の再埋め込みゲートの
前提そのもの(`input_hash` 不一致は全件再埋め込みか、誤ったベクトル再利用に
つながる)であり、この欠落が C1(UTF-16 切り詰めの不一致)を検出できなかった
直接の原因である。このファイルは40件全件について4グループ全てを照合する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from abist_kb.domain.frontmatter import hash_body, parse_frontmatter
from abist_kb.domain.line_range import range_hash
from abist_kb.infrastructure.search.chunker import DEFAULT_CHUNK_OPTIONS, Chunk, chunk_markdown
from abist_kb.infrastructure.search.e5_input import EmbeddingInputChunk, embedding_input, input_hash
from conftest import b64d


def _utf16_length(text: str) -> int:
    """JS の `string.length`(UTF-16 コード単位数)を Python 側で再現する。

    Python の `len()` はコードポイント単位なので、絵文字等の astral 文字
    (サロゲートペア)を含む文字列では JS の `.length` より小さくなる。
    `inputLength` フィクスチャ値は JS の `.length` そのものなので、比較は
    このヘルパー経由で行う。
    """
    return len(text.encode("utf-16-le")) // 2


REAL_DOCS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "real-docs" / "samples.json"
REAL_DOCS = json.loads(REAL_DOCS_PATH.read_text(encoding="utf-8"))
REAL_DOCS_CASES = {case["id"]: case for case in REAL_DOCS["cases"]}


def test_real_docs_fixture_has_40_cases() -> None:
    assert len(REAL_DOCS["cases"]) == 40


@pytest.mark.parametrize("case_id", sorted(REAL_DOCS_CASES))
def test_real_docs_frontmatter(case_id: str) -> None:
    case = REAL_DOCS_CASES[case_id]
    expected = case["expected"]["frontmatter"]
    raw = b64d(expected["raw_b64"])

    result = parse_frontmatter(raw)

    assert result.has_frontmatter == expected["hasFrontmatter"], case["path"]
    assert (result.bom != "") == expected["bomPresent"], case["path"]
    assert result.bom == b64d(expected["bom_b64"]), case["path"]
    assert result.eol == expected["eol"], case["path"]
    assert result.data == expected["data"], case["path"]
    assert list(result.keys) == expected["keys"], case["path"]
    assert sorted(result.block_keys) == expected["blockKeys"], case["path"]
    assert result.body == b64d(expected["body_b64"]), case["path"]
    assert result.raw == raw, case["path"]
    assert hash_body(raw) == expected["hashBody"], case["path"]


def _assert_chunk_matches(chunk: Chunk, expected: dict[str, Any], path: str) -> None:
    assert chunk.index == expected["index"], path
    assert chunk.heading_path == expected["heading_path"], path
    assert chunk.text == b64d(expected["text_b64"]), path
    assert chunk.start_line == expected["start_line"], path
    assert chunk.end_line == expected["end_line"], path
    assert chunk.token_estimate == expected["token_estimate"], path
    assert chunk.content_hash == expected["content_hash"], path
    assert chunk.split_by_size == expected["split_by_size"], path


@pytest.mark.parametrize("case_id", sorted(REAL_DOCS_CASES))
def test_real_docs_chunker(case_id: str) -> None:
    case = REAL_DOCS_CASES[case_id]
    raw = b64d(case["expected"]["frontmatter"]["raw_b64"])
    expected = case["expected"]["chunker"]

    chunks = chunk_markdown(raw, DEFAULT_CHUNK_OPTIONS)

    assert len(chunks) == expected["chunkCount"], case["path"]
    for chunk, expected_chunk in zip(chunks, expected["chunks"], strict=True):
        _assert_chunk_matches(chunk, expected_chunk, case["path"])


@pytest.mark.parametrize("case_id", sorted(REAL_DOCS_CASES))
def test_real_docs_line_range(case_id: str) -> None:
    """`line_range` グループ(firstLine/lastLine/fullRange/firstChunkRange)の照合。

    これまでどのテストからも参照されていなかった(`test_real_docs_fixture.py` は
    `ok is True` を確認するのみで、`range_hash` の値そのものは比較していなかった)。
    """
    case = REAL_DOCS_CASES[case_id]
    raw = b64d(case["expected"]["frontmatter"]["raw_b64"])
    expected = case["expected"]["line_range"]
    path = case["path"]

    total_lines_expected = expected["totalLines"]

    first = range_hash(raw, 1, 1)
    assert first.ok == expected["firstLine"]["ok"], path
    assert first.total_lines == total_lines_expected, path
    assert first.hash == expected["firstLine"]["hash"], path

    last = range_hash(raw, total_lines_expected, total_lines_expected)
    assert last.ok == expected["lastLine"]["ok"], path
    assert last.hash == expected["lastLine"]["hash"], path

    full = range_hash(raw, 1, total_lines_expected)
    assert full.ok == expected["fullRange"]["ok"], path
    assert full.hash == expected["fullRange"]["hash"], path

    chunks = chunk_markdown(raw, DEFAULT_CHUNK_OPTIONS)
    if chunks:
        first_chunk = range_hash(raw, chunks[0].start_line, chunks[0].end_line)
        assert expected["firstChunkRange"] is not None, path
        assert first_chunk.ok == expected["firstChunkRange"]["ok"], path
        assert first_chunk.hash == expected["firstChunkRange"]["hash"], path
    else:
        assert expected["firstChunkRange"] is None, path


@pytest.mark.parametrize("case_id", sorted(REAL_DOCS_CASES))
def test_real_docs_embedding(case_id: str) -> None:
    """`embedding` グループの照合(C1 の UTF-16 切り詰め不一致を検出するテスト)。

    採取スクリプト(`capture-real-docs.mjs`)は `chunks[0]`(chunker がそのまま
    返すオブジェクト)を直接 `embeddingInput` へ渡している。チャンクオブジェクトは
    `title` フィールドを持たないため、ここでの再現も `title=None` で行う。
    """
    case = REAL_DOCS_CASES[case_id]
    raw = b64d(case["expected"]["frontmatter"]["raw_b64"])
    expected = case["expected"]["embedding"]
    path = case["path"]

    chunks = chunk_markdown(raw, DEFAULT_CHUNK_OPTIONS)

    if not chunks:
        assert expected is None, path
        return

    assert expected is not None, path
    model = expected["model"]
    chunk = EmbeddingInputChunk(
        text=chunks[0].text, title=None, heading_path=chunks[0].heading_path
    )

    text = embedding_input(chunk, model)

    assert text == b64d(expected["embeddingInput_b64"]), path
    assert _utf16_length(text) == expected["inputLength"], path
    assert input_hash(model, text) == expected["embeddingInputHash"], path
