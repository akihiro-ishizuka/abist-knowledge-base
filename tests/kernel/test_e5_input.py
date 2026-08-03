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
    _truncate_utf16_units,
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


def test_empty_text_is_still_included_unlike_falsy_title_and_heading_path() -> None:
    """レビュー指摘(Minor): `embeddings.js:75-77` は `title`/`heading_path` を
    偽値でない場合だけ含めるが、`text` は無条件で含める。チャンカー生成の
    チャンクでは `text` が空文字列になることは無いため表面化しないが、DB行から
    直接組み立てる場合(M4)は `{title:'T', text:''}` のようなケースが起きうる。
    `text` を偽値フィルタから漏らすと、結合結果が末尾の改行1つ分だけ短くなり
    `input_hash` がズレる。
    """
    model = "Xenova/multilingual-e5-small"
    chunk = EmbeddingInputChunk(text="", title="T", heading_path=None)

    result = embedding_input(chunk, model)

    assert result == "passage: T\n"


# ---------------------------------------------------------------------------
# サロゲート境界のバイト一致回帰テスト。
#
# 過去の修正波で `_truncate_utf16_units` は UTF-16 コード単位での切り詰めまで
# 正しく実装したが、最終エンコードに `str.encode("utf-8", errors="replace")`
# を使っていた。これは Node の挙動を再現していると誤解されていたが誤りで
# あり、Python の decode 側 "replace" と encode 側 "replace" は意味が異なる:
# **encode** 側の "replace" は不正な入力を `?` (`0x3F`, 1バイト) に置換する
# だけで、Node の `Buffer.from(str, 'utf8')` が対になっていないサロゲートを
# 変換する U+FFFD (`EF BF BD`, 3バイト) にはならない。
#
# `input_hash` は UTF-8 バイト列に対する SHA-256 なので、この1バイト対3バイト
# の違いは「サロゲートペアの真ん中で切り詰められたチャンク」だけで
# `input_hash` を Node と食い違わせる——見出しに絵文字を使う本コーパスの
# 慣行では珍しくない条件である。以下は Node (`Buffer.from(slice, 'utf8')`)
# から実測した16進バイト列と一致することを固定する。
# ---------------------------------------------------------------------------


def test_truncate_cut_mid_pair_leaves_high_surrogate_as_u_fffd() -> None:
    """カットがサロゲートペアの真ん中(上位サロゲートが末尾に孤立)に来るケース。

    Node: `("abcd" + "\\u{1F600}" + "ef").slice(0, 5)` → "abcd" + 孤立上位
    サロゲート `\\ud83d`。`Buffer.from(result, 'utf8').hex()` の実測値は
    `61626364efbfbd` (末尾3バイトが U+FFFD)。
    """
    text = "abcd" + "\U0001f600" + "ef"
    result = _truncate_utf16_units(text, 5)
    assert result.encode("utf-8") == bytes.fromhex("61626364efbfbd")


def test_truncate_cut_after_complete_pair_keeps_pair_intact() -> None:
    """カットが完全なサロゲートペアの直後に来るケース(何も孤立しない)。

    Node: `("ab" + "\\u{1F600}" + "cdef").slice(0, 4)` → "ab" + 完全なペア。
    実測値は `6162f09f9880` (絵文字が4バイトのまま残る)。
    """
    text = "ab" + "\U0001f600" + "cdef"
    result = _truncate_utf16_units(text, 4)
    assert result.encode("utf-8") == bytes.fromhex("6162f09f9880")


def test_truncate_astral_char_exactly_at_boundary_keeps_pair_intact() -> None:
    """astral 文字がちょうど境界(切り詰め上限=文字列長)に位置するケース。

    Node: `("abc" + "\\u{1F600}").slice(0, 5)` → 全体(5 UTF-16 単位)がそのまま
    残り、ペアは割れない。実測値は `616263f09f9880`。
    """
    text = "abc" + "\U0001f600"
    result = _truncate_utf16_units(text, 5)
    assert result.encode("utf-8") == bytes.fromhex("616263f09f9880")


def test_truncate_low_surrogate_orphaned_at_start_becomes_u_fffd() -> None:
    """孤立した下位サロゲートが保持領域の先頭にあるケース(不正な入力データ)。

    JS 文字列は元々 UTF-16 コード単位の任意列を許容するため、対になって
    いない下位サロゲートが(切り詰めとは無関係に)最初から入力に含まれて
    いることもありうる。Node: `Buffer.from("\\udE00abcdef", 'utf8').hex()`
    の実測値は `efbfbd616263646566` (先頭3バイトが U+FFFD)。
    """
    text = "\ude00" + "abcdef"
    result = _truncate_utf16_units(text, 100)
    assert result.encode("utf-8") == bytes.fromhex("efbfbd616263646566")


def test_embedding_input_mid_surrogate_pair_truncation_matches_node_u_fffd() -> None:
    """`embedding_input()` を通しても U+FFFD (3バイト) が Node と一致する。

    `max_input_chars` (Xenova/multilingual-e5-small は512) をちょうど跨ぐ
    位置に絵文字を置き、切り詰めが上位サロゲートを孤立させることを確認する。
    """
    model = "Xenova/multilingual-e5-small"
    config = model_config(model)
    # "passage: " プレフィックスは切り詰めに数えないため、本文だけで
    # ちょうど512 UTF-16単位になるよう組み立てる: 511文字 + 絵文字の上位
    # サロゲートが512番目の単位として孤立する。
    body_text = "a" * (config.max_input_chars - 1) + "\U0001f600"
    chunk = EmbeddingInputChunk(text=body_text, title=None, heading_path=None)

    result = embedding_input(chunk, model)

    assert result == "passage: " + "a" * (config.max_input_chars - 1) + "�"
    result_bytes = result.encode("utf-8")
    assert result_bytes.endswith(bytes.fromhex("efbfbd"))
    assert b"\x3f" not in result_bytes[-3:]  # 旧不具合の `?` (0x3F) ではない


def test_query_input_mid_surrogate_pair_truncation_matches_node_u_fffd() -> None:
    """`query_input()` でも同じ U+FFFD 置換が働くことを確認する。"""
    model = "Xenova/multilingual-e5-small"
    config = model_config(model)
    query = "a" * (config.max_input_chars - 1) + "\U0001f600"

    result = query_input(query, model)

    assert result == "query: " + "a" * (config.max_input_chars - 1) + "�"
    assert result.encode("utf-8").endswith(bytes.fromhex("efbfbd"))


def test_input_hash_of_mid_surrogate_truncation_uses_three_byte_u_fffd() -> None:
    """`input_hash` が(1バイトの `?` ではなく)3バイトの U+FFFD を刻んだ
    UTF-8 バイト列に対するハッシュであることを固定する。
    """
    model = "Xenova/multilingual-e5-small"
    text = "abcd" + "\U0001f600" + "ef"
    truncated = _truncate_utf16_units(text, 5)  # "abcd�"

    expected_bytes = f"{model}\n".encode() + bytes.fromhex("61626364efbfbd")
    assert input_hash(model, truncated) == hashlib.sha256(expected_bytes).hexdigest()

    # 回帰防止: もし `errors="replace"` (encode 側) が再導入されたら
    # ここが `?` (0x3F) 1バイトになり、上記アサーションが失敗する。
    wrong_bytes = f"{model}\n".encode() + b"abcd?ef"
    assert input_hash(model, truncated) != hashlib.sha256(wrong_bytes).hexdigest()


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
#
# レビュー指摘(C3): 旧版のテストは `input_hash(model, decode(embeddingInput_b64))
# == input_hash` を確認するだけで、これは記録された「答え」を再ハッシュして
# 再度突き合わせているだけの循環参照であり、`embedding_input()` を一度も
# 呼んでいなかった(実装の truncate/prefix 順序が壊れていても検出できない)。
#
# M1 Task 8 で `capture-embeddings.mjs` を拡張し、`gate-samples.json` の各ケースに
# `embeddingInput()` が実際に受け取った材料そのもの(`title_b64`/`heading_path_b64`/
# `text_b64`、`chunks` テーブル行から直接採取)を追加採取した。そのため、以前のように
# `tests/fixtures/real-docs/samples.json`(40件の層化サンプル)から同じ path を探して
# `chunk_markdown()` で再構成する必要が無くなり、100件全件で `embedding_input()` を
# 直接呼び出して検証できる(以前は path が real-docs 側に無い36件が `pytest.skip` に
# なっていた。この 64/100 という達成カバレッジは `tests/fixtures/PROVENANCE.md` の
# 旧記述であり、本ファイルの更新に合わせて 100/100 に置き換えている)。
#
# `title_b64`/`heading_path_b64` は NULL 許容の `chunks.title`/`chunks.heading_path`
# 列をそのまま反映するため、フィクスチャ側で `null` の場合は `None` として渡す
# (`embedding_input()` 側は falsy チェックなので `None` と `""` は挙動上同じだが、
# 元の値を保つ)。`text_b64` は `chunks.text`(NOT NULL)なので常に文字列。
#
# 全100件が `title_b64`/`heading_path_b64`/`text_b64` を持つため、スキップは発生
# しない(0件)。万一いずれかのケースでフィールドが欠けていれば、それはフィクスチャの
# 採取不備でありテストの不具合ではないため、`pytest.fail` で明示的に落とす
# (黙ってスキップしない)。
# ---------------------------------------------------------------------------

GATE_SAMPLES_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "embedding" / "gate-samples.json"
)
GATE_SAMPLES = json.loads(GATE_SAMPLES_PATH.read_text(encoding="utf-8"))

# 100/100 という達成カバレッジそのものを固定する(この数が下がったら、fixture の
# 採取ロジックが壊れて必須フィールドを書けなくなったことを意味するので、無言で
# 変動させない)。
GATE_SAMPLES_VERIFIABLE_COUNT = 100
GATE_SAMPLES_UNVERIFIABLE_COUNT = 0


def _gate_sample_chunk(case: dict) -> EmbeddingInputChunk:
    """`gate-samples.json` の1ケースから `embedding_input()` への入力を組み立てる。

    `title_b64`/`heading_path_b64` は `chunks.title`/`chunks.heading_path` が NULL
    だった場合に `null` として記録されているため、その場合は `None` を渡す。
    """
    title = b64d(case["title_b64"]) if case["title_b64"] is not None else None
    heading_path = b64d(case["heading_path_b64"]) if case["heading_path_b64"] is not None else None
    text = b64d(case["text_b64"])
    return EmbeddingInputChunk(text=text, title=title, heading_path=heading_path)


def test_gate_samples_fixture_has_100_cases() -> None:
    assert len(GATE_SAMPLES["cases"]) == 100


def test_gate_samples_all_cases_carry_verifiable_input_material() -> None:
    """達成カバレッジそのものの回帰テスト。

    以前は `real-docs/samples.json` に同じ文書が無い36件が再構成不能だったが、
    `gate-samples.json` 自体が材料(title/heading_path/text)を持つようになったため、
    100件全件が検証可能になった。この数が下がったら fixture の採取が壊れている。
    """
    verifiable = sum(1 for case in GATE_SAMPLES["cases"] if case.get("text_b64") is not None)
    unverifiable = len(GATE_SAMPLES["cases"]) - verifiable
    assert verifiable == GATE_SAMPLES_VERIFIABLE_COUNT
    assert unverifiable == GATE_SAMPLES_UNVERIFIABLE_COUNT


@pytest.mark.parametrize("index", range(len(GATE_SAMPLES["cases"])))
def test_gate_samples_reproduce_e5_input(index: int) -> None:
    """`embedding_input()` を実際に呼び出して `gate-samples.json` を再現する。

    フィクスチャに材料(title_b64/heading_path_b64/text_b64)が無いケースは
    スキップではなく明示的な失敗にする(黙って通過させない。現状は0件のはず)。
    """
    case = GATE_SAMPLES["cases"][index]
    if case.get("text_b64") is None:
        pytest.fail(
            f"id={case['id']!r}: gate-samples.json に text_b64 が記録されていない"
            "(fixture の採取不備。capture-embeddings.mjs を確認すること)"
        )

    chunk = _gate_sample_chunk(case)
    model = case["model"]
    reconstructed = embedding_input(chunk, model)

    expected_input = b64d(case["embeddingInput_b64"])
    assert reconstructed == expected_input
    assert input_hash(model, reconstructed) == case["input_hash"]
