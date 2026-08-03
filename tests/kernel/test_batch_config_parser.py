"""`batch_config_parser.py` の M1 ゴールデン(`tests/fixtures/kernel/batch-config.json`)照合テスト。

fixture は旧 `tools/lib/batch-config-store.js` の `formatConfig`/`loadBatchConfigsFresh`
を実行して得たゴールデン値であり、テストが落ちたら疑うべきは実装で fixture ではない。

`format_config_synthetic_roundtrip` が最重要ケースである: 空配列・`0`(falsy値の
`maxDepth`/`delay`)・単引用符を含むキーと値、を含む合成設定でのラウンドトリップを
検証する。M1 のレビューで、falsy値やクォートのエスケープを誤って特別扱いする
パーサは実ファイルのケース(`real_batch_config_roundtrip`)だけなら通ってしまう
ことが実証されたため、このケースが追加された。

`parse_batch_config` は JavaScript を一切実行しない(`eval` 相当・`json5` などの
緩いパーサ・正規表現での場当たり的抽出のいずれも使わない)。式・関数呼び出し・
テンプレートリテラル・変数参照は `AppError(ErrorCode.UNSUPPORTED_BATCH_CONFIG)` で
拒否されることを別途テストする(この4パターンは fixture には無く、設計書が
明示的に要求する拒否シナリオとして直接テストする)。
"""

from __future__ import annotations

import base64

import pytest

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.migration.batch_config_parser import (
    format_batch_config,
    parse_batch_config,
    serialize_batch_config_file,
)
from conftest import load_kernel_fixture

FIXTURE = load_kernel_fixture("batch-config")
CASES = {case["id"]: case for case in FIXTURE["cases"]}
_case_count = len(FIXTURE["cases"])
assert _case_count == 6, f"想定6ケースに対し {_case_count} 件しか読み込めていない"


def _b64d(s: str) -> str:
    return base64.b64decode(s).decode("utf-8")


# fixture の `input.config` はキャプチャ時にキー順を再ソートされてしまっている
# (metadata-schema.json の `resolve_batch_output_dirs` と同じ既知の制約)。
# `format_batch_config`/`serialize_batch_config_file` はJSの `Object.entries` と
# 同じ挿入順で出力するため、バイト単位比較には元の挿入順が要る。真の順序は
# 旧 `test/batch-config-store.test.js` の `SAMPLE_CONFIG` 定義(サンプル系)と、
# 期待される `formatted_b64`/`serialized_b64` を実際にデコードした内容(合成系)
# から確認済みの値をここへ固定する。dict の等価比較(`==`)自体はキー順を見ないため、
# `loaded == config` のような構造比較には影響しない。
_TOP_ORDER_SAMPLE = ("蛇腹形状の自動設計", "o'reilly-notes", "catiadoc", "catia-flotherm-prep")
_NESTED_ORDER_SAMPLE = {
    "catiadoc": ("type", "url", "outputDir", "maxDepth", "delay"),
    "catia-flotherm-prep": ("type", "repository", "branch", "outputDir"),
}
_TOP_ORDER_SYNTHETIC = (
    "空配列バッチ",
    "単純esaバッチ",
    "quote'in'name",
    "webBatch",
    "webNoOutputDir",
    "gitBatch",
    "numericAndBoolLikeStrings",
)
_NESTED_ORDER_SYNTHETIC = {
    "webBatch": ("type", "url", "outputDir", "maxDepth", "delay"),
    "webNoOutputDir": ("type", "url", "maxDepth", "delay"),
    "gitBatch": ("type", "repository", "branch", "outputDir"),
    "numericAndBoolLikeStrings": ("type", "url", "outputDir", "maxDepth", "delay"),
}
_CASE_ORDER = {
    "format_config_sample": (_TOP_ORDER_SAMPLE, _NESTED_ORDER_SAMPLE),
    "format_config_sample_roundtrip": (_TOP_ORDER_SAMPLE, _NESTED_ORDER_SAMPLE),
    "format_config_synthetic": (_TOP_ORDER_SYNTHETIC, _NESTED_ORDER_SYNTHETIC),
    "format_config_synthetic_roundtrip": (_TOP_ORDER_SYNTHETIC, _NESTED_ORDER_SYNTHETIC),
}


def _reordered_config(case_id: str, config: dict) -> dict:
    top_order, nested_orders = _CASE_ORDER[case_id]
    result = {}
    for key in top_order:
        value = config[key]
        if isinstance(value, dict) and key in nested_orders:
            value = {nested_key: value[nested_key] for nested_key in nested_orders[key]}
        result[key] = value
    return result


# --- formatConfig(直列化)の単体ケース ------------------------------------------

FORMAT_CASE_IDS = ["format_config_sample", "format_config_synthetic"]


@pytest.mark.parametrize("case_id", FORMAT_CASE_IDS)
def test_format_batch_config(case_id: str) -> None:
    case = CASES[case_id]
    config = _reordered_config(case_id, case["input"]["config"])
    formatted = format_batch_config(config)
    assert formatted == _b64d(case["expected"]["formatted_b64"])


# --- サンプル設定でのフルファイル・ラウンドトリップ ---------------------------------

ROUNDTRIP_CASE_IDS = ["format_config_sample_roundtrip", "format_config_synthetic_roundtrip"]


@pytest.mark.parametrize("case_id", ROUNDTRIP_CASE_IDS)
def test_serialize_and_parse_roundtrip(case_id: str) -> None:
    case = CASES[case_id]
    original_config = case["input"]["config"]
    ordered_config = _reordered_config(case_id, original_config)

    serialized = serialize_batch_config_file(ordered_config)
    assert serialized == _b64d(case["expected"]["serialized_b64"])

    loaded = parse_batch_config(serialized)
    assert loaded == case["expected"]["loaded"]
    assert loaded == original_config
    assert case["expected"]["loadedEqualsOriginal"] is True


def test_format_config_regex_fallback_parseable() -> None:
    """UIサーバの正規表現フォールバックが対象にする形の内容を、パーサでも読めることを確認する。

    このケースの `content_b64` は `format_config_sample_roundtrip` の
    `serialized_b64` と同一内容(SAMPLE_CONFIG から生成したファイル全体)である。
    """
    case = CASES["format_config_regex_fallback_parseable"]
    content = _b64d(case["expected"]["content_b64"])

    sample_case = CASES["format_config_sample_roundtrip"]
    assert content == _b64d(sample_case["expected"]["serialized_b64"])

    loaded = parse_batch_config(content)
    assert loaded == sample_case["input"]["config"]
    assert case["expected"]["parsedBackEqualsOriginal"] is True
    assert case["expected"]["regexMatched"] is True


def test_real_batch_config_roundtrip() -> None:
    """実 `batch-config.js` を解析し、直列化した結果がバイト単位で元に戻ることを確認する。"""
    case = CASES["real_batch_config_roundtrip"]
    original = _b64d(case["expected"]["original_b64"])

    loaded = parse_batch_config(original)
    assert loaded == case["expected"]["loadedConfig"]
    assert case["expected"]["firstDiffIndex"] is None

    regenerated = serialize_batch_config_file(loaded)
    assert regenerated == _b64d(case["expected"]["regenerated_b64"])
    assert regenerated == original
    assert case["expected"]["regeneratedMatchesOriginalByteForByte"] is True


def test_all_fixture_cases_covered() -> None:
    """fixture の全6ケースがこのテストファイルで参照されていることの網羅性チェック。"""
    covered = {
        *FORMAT_CASE_IDS,
        *ROUNDTRIP_CASE_IDS,
        "format_config_regex_fallback_parseable",
        "real_batch_config_roundtrip",
    }
    assert covered == set(CASES)


# --- リテラル以外の拒否シナリオ(fixtureには無い。設計書 §Task7 が明示的に要求) -------


def _wrap(expression: str) -> str:
    return f"export const batchConfigs = {{ a: {expression} }};\n"


@pytest.mark.parametrize(
    ("name", "snippet"),
    [
        ("expression", _wrap("1 + 1")),
        ("function_call", _wrap("someFunction()")),
        ("template_literal", _wrap("`hello ${1}`")),
        ("variable_reference", _wrap("someVariable")),
    ],
)
def test_rejects_non_literal_tokens(name: str, snippet: str) -> None:
    with pytest.raises(AppError) as exc_info:
        parse_batch_config(snippet)

    error = exc_info.value
    assert error.code == ErrorCode.UNSUPPORTED_BATCH_CONFIG, name
    assert error.hint
    assert "line" in error.details
    assert "column" in error.details


def test_rejects_missing_assignment() -> None:
    with pytest.raises(AppError) as exc_info:
        parse_batch_config("// no batchConfigs export here\n")
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_BATCH_CONFIG


def test_rejects_top_level_non_object() -> None:
    with pytest.raises(AppError) as exc_info:
        parse_batch_config("export const batchConfigs = 'not an object';\n")
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_BATCH_CONFIG
