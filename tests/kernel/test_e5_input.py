"""`e5_input.py` の M1 ゴールデン(`tests/fixtures/kernel/embeddings.json`)照合テスト。

fixture は旧 `tools/lib/embeddings.js` を実行して得たゴールデン値であり、
テストが落ちたら疑うべきは実装であって fixture ではない。

fixture のキーは Node 由来の camelCase(`embeddingInputHash` 等)、Python 側 API は
snake_case のため、ここで明示的にマッピングする。モデル名も旧実装の
`Xenova/multilingual-e5-small` 等をそのまま fixture キーとして使う
(`input_hash` はモデル名の文字列そのものをハッシュに含めるため、Python 側で
別名に変えると M8 の埋め込みゲート照合が壊れる)。

順序が命: `[title, heading_path, text]` を結合してから `max_input_chars` で
切り詰め、その後に接頭辞を付ける。接頭辞は文字数制限に数えない。
`boundary_straddle_512` ケースで、この順序を逆にすると `input_hash` が変わる
ことを明示的に確認する。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from abist_kb.infrastructure.search.e5_input import (
    EMBEDDING_MODELS,
    EmbeddingInputChunk,
    embedding_input,
    input_hash,
    model_config,
    query_input,
)
from conftest import b64d, load_kernel_fixture

FIXTURE = load_kernel_fixture("embeddings")
CASES = {case["id"]: case for case in FIXTURE["cases"]}
assert len(FIXTURE["cases"]) == 15


def _chunk(data: dict[str, Any]) -> EmbeddingInputChunk:
    return EmbeddingInputChunk(
        text=b64d(data["text_b64"]),
        title=data.get("title"),
        heading_path=data.get("heading_path"),
    )


def test_embedding_models_table() -> None:
    case = CASES["embedding_models_table"]
    expected = case["expected"]["value"]
    assert set(EMBEDDING_MODELS) == set(expected)
    for model, spec in expected.items():
        config = model_config(model)
        assert config.provider == spec["provider"]
        assert config.dimensions == spec["dimensions"]
        assert config.passage_prefix == spec["passagePrefix"]
        assert config.query_prefix == spec["queryPrefix"]
        assert config.max_input_chars == spec["maxInputChars"]


@pytest.mark.parametrize(
    "case_id",
    [
        "model_config_Xenova_multilingual_e5_small",
        "model_config_Xenova_multilingual_e5_base",
        "model_config_text_embedding_3_small",
        "model_config_text_embedding_unknown_variant",
        "model_config_some_unknown_local_model",
    ],
)
def test_model_config_cases(case_id: str) -> None:
    case = CASES[case_id]
    expected = case["expected"]["value"]
    config = model_config(case["input"])
    assert config.provider == expected["provider"]
    assert config.dimensions == expected["dimensions"]
    assert config.passage_prefix == expected["passagePrefix"]
    assert config.query_prefix == expected["queryPrefix"]
    assert config.max_input_chars == expected["maxInputChars"]


@pytest.mark.parametrize(
    "case_id",
    [
        "short_no_truncation",
        "boundary_straddle_512",
        "boundary_exact_512",
        "boundary_513_one_over",
        "openai_model_no_prefix_no_truncation",
        "no_title_no_heading_path",
    ],
)
def test_embedding_input_cases(case_id: str) -> None:
    case = CASES[case_id]
    model = case["input"]["model"]
    chunk = _chunk(case["input"]["chunk"])
    expected = case["expected"]

    text = embedding_input(chunk, model)

    assert text == b64d(expected["embeddingInput_b64"])
    assert len(text) == expected["inputLength"]
    assert input_hash(model, text) == expected["embeddingInputHash"]


@pytest.mark.parametrize(
    "case_id",
    ["query_short_e5", "query_boundary_513_e5", "query_long_openai"],
)
def test_query_input_cases(case_id: str) -> None:
    case = CASES[case_id]
    model = case["input"]["model"]
    query = b64d(case["input"]["query_b64"])
    expected = case["expected"]

    text = query_input(query, model)

    assert text == b64d(expected["queryInput_b64"])
    assert len(text) == expected["length"]


def test_input_hash_formula() -> None:
    assert input_hash("model-x", "hello") == hashlib.sha256(b"model-x\nhello").hexdigest()


def test_boundary_straddle_512_breaks_if_prefix_applied_before_truncation() -> None:
    """順序を逆にする(先に接頭辞を付けてから切り詰める)と `input_hash` が変わることを
    明示的に固定する。境界(512文字)をまたぐケースでなければこの違いは表面化しない。
    """
    case = CASES["boundary_straddle_512"]
    model = case["input"]["model"]
    chunk = _chunk(case["input"]["chunk"])
    expected_hash = case["expected"]["embeddingInputHash"]

    correct = embedding_input(chunk, model)
    assert input_hash(model, correct) == expected_hash

    config = model_config(model)
    parts = [p for p in (chunk.title, chunk.heading_path, chunk.text) if p]
    body = "\n".join(parts)
    reversed_order = (config.passage_prefix + body)[: config.max_input_chars]

    assert reversed_order != correct
    assert input_hash(model, reversed_order) != expected_hash


def test_all_fixture_cases_covered() -> None:
    covered = {
        "embedding_models_table",
        "model_config_Xenova_multilingual_e5_small",
        "model_config_Xenova_multilingual_e5_base",
        "model_config_text_embedding_3_small",
        "model_config_text_embedding_unknown_variant",
        "model_config_some_unknown_local_model",
        "short_no_truncation",
        "boundary_straddle_512",
        "boundary_exact_512",
        "boundary_513_one_over",
        "openai_model_no_prefix_no_truncation",
        "no_title_no_heading_path",
        "query_short_e5",
        "query_boundary_513_e5",
        "query_long_openai",
    }
    assert set(CASES) == covered


# ---------------------------------------------------------------------------
# 埋め込みゲート素材(`tests/fixtures/embedding/gate-samples.json`)との突合。
#
# M8 の埋め込み再利用ゲート(design/system-design.md §11.2)の前提条件:
# 同一チャンクから Python 側でも同じ e5 入力文字列・input_hash が再現できる
# こと。ここが通らなければ M8 は cosine 判定に進めず全件再埋め込みになる。
# ---------------------------------------------------------------------------

GATE_SAMPLES_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "embedding" / "gate-samples.json"
)
GATE_SAMPLES = json.loads(GATE_SAMPLES_PATH.read_text(encoding="utf-8"))


def test_gate_samples_fixture_has_100_cases() -> None:
    assert len(GATE_SAMPLES["cases"]) == 100


@pytest.mark.parametrize("index", range(len(GATE_SAMPLES["cases"])))
def test_gate_samples_reproduce_e5_input(index: int) -> None:
    case = GATE_SAMPLES["cases"][index]
    model = case["model"]
    expected_input = b64d(case["embeddingInput_b64"])

    assert input_hash(model, expected_input) == case["input_hash"]
