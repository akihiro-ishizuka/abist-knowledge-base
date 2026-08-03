"""M1 追加採取(b32doc-filter ギャップ解消)の `tests/fixtures/kernel/b32doc-filter.json`
の健全性検証。

`capture-b32doc-filter.mjs` は旧 Node システムの `tools/lib/b32doc-filter.js` を実行し、
6つのルール(path_excluded / toc_or_default / not_html_category / language_excluded /
empty_content / too_short)それぞれの accept/reject ケース・CRLF セクション分割ケース・
実 B32doc 文書8件・JS/Python(filters.py)の優先順位食い違いの実測結果を記録した。
ここでのテスト対象は Python 実装ではなく、旧 Node システムを実行して採取した
fixture 自体。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from abist_kb.domain.redaction import mask_secrets

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "kernel" / "b32doc-filter.json"

# capture-b32doc-filter.mjs が accept/reject の両方を用意した6ルール。
# ケースid の接頭辞で accept/reject を判別する（decide_accept_* / decide_reject_*）。
EXPECTED_RULES = {
    "path_excluded",
    "toc_or_default",
    "not_html_category",
    "language_excluded",
    "empty_content",
    "too_short",
}


def _load() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _iter_b64_fields(obj: Any) -> Iterator[tuple[str, str]]:
    """オブジェクトを再帰的に辿り、キーが "_b64" で終わる文字列値をすべて返す。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.endswith("_b64") and isinstance(value, str):
                yield key, value
            else:
                yield from _iter_b64_fields(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_b64_fields(item)


def test_fixture_has_valid_standard_envelope() -> None:
    data = _load()
    assert data["schema"] == 1
    assert isinstance(data["source"], str) and data["source"] == "tools/lib/b32doc-filter.js"
    assert isinstance(data["cases"], list)
    assert len(data["cases"]) > 0


def test_case_ids_are_unique() -> None:
    data = _load()
    ids = [case["id"] for case in data["cases"]]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"重複した case id がある: {duplicates}"


def test_all_b64_fields_decode_as_valid_base64() -> None:
    data = _load()
    for key, value in _iter_b64_fields(data):
        try:
            base64.b64decode(value, validate=True)
        except binascii.Error as exc:  # pragma: no cover - 失敗時に原因を明示する
            raise AssertionError(f"フィールド {key!r} が base64 として不正: {exc}") from exc


def _decide_cases(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in data["cases"] if c["id"].startswith("decide_")}


def test_every_rule_has_both_an_accept_and_a_reject_case() -> None:
    """6ルールそれぞれについて、少なくとも1つの accept ケースと1つの reject ケースが
    fixture に存在すること(brief 要求: 「各ルールについて少なくとも1つのaccept
    ケースと1つのrejectケース」)。"""
    data = _load()
    decide_cases = _decide_cases(data)

    accepted_rules: set[str] = set()
    rejected_reasons: set[str] = set()
    for case in decide_cases.values():
        expected = case["expected"]
        if expected["indexable"] is True:
            accepted_rules.add("__any_accept__")
        else:
            assert expected["reason"], f"{case['id']}: reject ケースなのに reason が空"
            rejected_reasons.add(expected["reason"])

    assert rejected_reasons == EXPECTED_RULES, (
        f"reject 側で網羅されていないルールがある: 不足={EXPECTED_RULES - rejected_reasons}, "
        f"想定外={rejected_reasons - EXPECTED_RULES}"
    )
    assert "__any_accept__" in accepted_rules, "accept ケースが1件も無い"

    # baseline(全条件を満たす)ケースが存在し、6ルール全部の accept 経路を
    # 少なくとも一度は通っていることを、baseline とルール別 accept ケースの
    # 両方の存在で確認する。
    accept_case_ids = {
        cid for cid, case in decide_cases.items() if case["expected"]["indexable"] is True
    }
    expected_accept_ids = {
        "decide_accept_baseline",
        "decide_accept_path_not_excluded",
        "decide_accept_normal_filename",
        "decide_accept_html_category",
        "decide_accept_language_mixed",
        "decide_accept_language_ja",
        "decide_accept_too_short_boundary_exact",
    }
    missing = expected_accept_ids - accept_case_ids
    assert not missing, f"想定していた accept ケースが無い: {missing}"


def test_every_reject_case_has_a_non_empty_reason() -> None:
    data = _load()
    for case in _decide_cases(data).values():
        expected = case["expected"]
        if expected["indexable"] is False:
            assert isinstance(expected["reason"], str) and expected["reason"], (
                f"{case['id']}: reject ケースの reason が空または非文字列"
            )
        else:
            assert expected["reason"] is None, f"{case['id']}: accept ケースなのに reason が非null"


def test_crlf_case_decodes_with_crlf_intact() -> None:
    """B32doc の実ファイルは全て CRLF。転記した SAMPLE ベースのケースが
    改行変換で壊れず `\\r\\n` を保持していること。"""
    data = _load()
    case = next(c for c in data["cases"] if c["id"] == "extract_summary_keys_crlf_sample")
    raw = base64.b64decode(case["input_b64"]).decode("utf-8")
    assert "\r\n" in raw, "CRLF が採取時に失われている"
    # SAMPLE は全行 CRLF で構成されているため、裸の LF (\r を伴わない \n) が
    # 紛れ込んでいないことも確認する(改行の一部だけが正規化された場合を検出する)。
    bare_lf_count = raw.count("\n") - raw.count("\r\n")
    assert bare_lf_count == 0, (
        f"裸の \\n が {bare_lf_count} 個混入している(部分的な改行正規化の疑い)"
    )


def test_real_doc_samples_present_and_within_size_limit() -> None:
    data = _load()
    manifest = data["real_docs_sample"]
    assert manifest["selected_count"] >= 5, "real_docs_sample が5件未満"
    assert manifest["selected_count"] <= 10, "real_docs_sample が10件を超えている"
    assert manifest["skipped_over_64kb_count"] >= 0

    real_cases = [c for c in data["cases"] if c["id"].startswith("real_doc_")]
    assert len(real_cases) == manifest["selected_count"]
    for case in real_cases:
        assert case["byte_size"] <= manifest["max_bytes"], (
            f"{case['id']}: 64KB超過ファイルが real_docs サンプルに紛れ込んでいる"
        )
        assert "decision" in case["expected"]
        assert "reason" in case["expected"]["decision"]


def test_priority_order_divergence_case_is_recorded() -> None:
    """JS(decideIndexable)と旧Python filters.py の優先順位食い違いが、
    推測でなく実測値として記録されていること。"""
    data = _load()
    case = next(c for c in data["cases"] if c["id"] == "js_vs_python_priority_order_divergence")
    expected = case["expected"]
    assert expected["js_decideIndexable_reason"] == "path_excluded"
    assert expected["python_build_index_actual_order_reason"] == "path_excluded"
    assert expected["python_filters_decide_reason"] == "not_html_category"
    assert expected["divergence"]["js_vs_build_index_actual"] is False
    assert expected["divergence"]["js_vs_filters_decide"] is True


def test_noise_filename_source_divergence_case_is_recorded() -> None:
    data = _load()
    case = next(
        c for c in data["cases"] if c["id"] == "js_vs_python_noise_filename_source_divergence"
    )
    expected = case["expected"]
    assert expected["python_noise_via_frontmatter_source_name"]["noisy"] is True
    assert expected["python_noise_via_disk_filename"]["noisy"] is False


def test_mask_secrets_is_a_no_op_on_fixture_content() -> None:
    """`abist_kb.domain.redaction.mask_secrets` を fixture の全 *_b64 内容に
    適用しても変化が無いこと(brief: mask-invariance の検証)。"""
    data = _load()
    checked = 0
    for key, value in _iter_b64_fields(data):
        try:
            decoded = base64.b64decode(value).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue
        checked += 1
        masked = mask_secrets(decoded)
        assert masked == decoded, f"フィールド {key!r} に mask_secrets が反応する内容が残っている"
    assert checked > 0, "base64 コンテンツを1件も検証できなかった(テスト自体が空振り)"
