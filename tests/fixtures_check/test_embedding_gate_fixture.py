"""M1 Task 5 (Part 1) で採取した埋め込みゲート素材 (`tests/fixtures/embedding/gate-samples.json`)
の健全性検証。

design/system-design.md §11.2 の埋め込み再利用ゲート判定(コサイン類似度>=0.999 かつ
Recall@5低下<=0.01)の「入力」を採取したフィクスチャそのものを検証する。ここでは
ゲート判定(合格/不合格)は行わない — それは M8 の役目であり、この fixture は
その測定に使う入力データが正しく・過不足なく採取されていることだけを保証する。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np

from abist_kb.domain.redaction import mask_secrets

FIXTURES_EMBEDDING_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "embedding"
GATE_SAMPLES_PATH = FIXTURES_EMBEDDING_DIR / "gate-samples.json"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_STRATA = [
    f"{length}_{script}"
    for length in ("short", "medium", "long")
    for script in ("japanese", "ascii", "mixed")
]


def _load() -> dict[str, Any]:
    data = json.loads(GATE_SAMPLES_PATH.read_text(encoding="utf-8"))
    assert data, f"embedding gate フィクスチャが空: {GATE_SAMPLES_PATH}"
    return data


def _iter_b64_fields(obj: Any) -> Iterator[tuple[str, str]]:
    """オブジェクトを再帰的に辿り、キーが \"_b64\" で終わる文字列値をすべて返す。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.endswith("_b64") and isinstance(value, str):
                yield key, value
            else:
                yield from _iter_b64_fields(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_b64_fields(item)


def test_schema_has_expected_top_level_fields() -> None:
    data = _load()
    assert data["schema"] == 1
    assert isinstance(data["source"], str) and data["source"]
    assert isinstance(data["design_reference"], str) and "11.2" in data["design_reference"]
    assert isinstance(data["gate_note"], str) and data["gate_note"]
    assert data["embedding_model"] == "Xenova/multilingual-e5-small"
    assert isinstance(data["db_counts"], dict)
    assert isinstance(data["strata"], list)
    assert isinstance(data["cases"], list)


def test_exactly_100_samples() -> None:
    data = _load()
    assert len(data["cases"]) == 100, f"サンプル件数が100件ではない: {len(data['cases'])}"


def test_case_ids_are_unique() -> None:
    data = _load()
    ids = [case["id"] for case in data["cases"]]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"重複した case id がある: {duplicates}"


def test_chunk_ids_are_unique() -> None:
    """層化抽出は9層が互いに排他的な条件(長さ×文字種)であるべきで、同一chunk_idが
    複数層・複数ケースにまたがって重複選択されていないこと。"""
    data = _load()
    chunk_ids = [case["chunk_id"] for case in data["cases"]]
    duplicates = {c for c in chunk_ids if chunk_ids.count(c) > 1}
    assert not duplicates, f"重複選択された chunk_id がある: {duplicates}"


def test_all_nine_strata_are_present_with_recorded_counts() -> None:
    """9層(短/中/長 × 日本語主体/英数主体/混在)すべてが manifest に「実際の件数」
    付きで記録され、いずれも空でないこと(母集団は各層とも数十件以上ある実データの
    はずなので、空になれば層化ロジックの破損を疑う)。"""
    data = _load()
    strata_by_name = {s["name"]: s for s in data["strata"]}
    assert set(strata_by_name) == set(EXPECTED_STRATA), (
        f"9層が揃っていない: 期待={sorted(EXPECTED_STRATA)} 実際={sorted(strata_by_name)}"
    )
    total_selected = 0
    for name, stratum in strata_by_name.items():
        assert isinstance(stratum["target_count"], int) and stratum["target_count"] > 0
        assert isinstance(stratum["matching_total"], int) and stratum["matching_total"] > 0, (
            f"{name}: 母集団が0件 — 層化クエリが壊れている可能性"
        )
        assert stratum["selected_count"] > 0, (
            f"{name}: 選択件数が0件 — 層化クエリが壊れている可能性"
        )
        assert stratum["selected_count"] == stratum["target_count"], (
            f"{name}: selected_count({stratum['selected_count']}) が"
            f"target_count({stratum['target_count']})と一致しない"
        )
        assert stratum["selected_count"] <= stratum["matching_total"]
        assert len(stratum["selected_chunk_ids"]) == stratum["selected_count"]
        total_selected += stratum["selected_count"]
    assert total_selected == 100, f"全層の合計が100件ではない: {total_selected}"


def test_strata_target_counts_sum_to_100_as_12_plus_11_times_8() -> None:
    """100 = 12 + 11*8 という配分(9では割り切れない100件の決定的な配分方法)を固定する。"""
    data = _load()
    targets = sorted((s["target_count"] for s in data["strata"]), reverse=True)
    assert targets == [12] + [11] * 8


def test_every_case_has_required_fields() -> None:
    data = _load()
    for case in data["cases"]:
        for key in (
            "chunk_id",
            "path",
            "chunk_index",
            "content_hash",
            "model",
            "dimensions",
            "embeddingInput_b64",
            "input_hash",
            "vector_b64",
            "title_b64",
            "heading_path_b64",
            "text_b64",
            "contains_astral",
        ):
            assert key in case, f"{case.get('id')}: 必須フィールド {key!r} が無い"
        assert isinstance(case["content_hash"], str) and len(case["content_hash"]) == 64
        assert case["model"] == "Xenova/multilingual-e5-small"
        assert case["dimensions"] == 384


# ---------------------------------------------------------------------------
# M1 Task 8: embeddingInput() の材料そのもの(title_b64/heading_path_b64/text_b64)
# の健全性検証。以前は gate-samples.json が出力のみを記録していたため、Python 側の
# `tests/kernel/test_e5_input.py` は 64/100 件しか `embedding_input()` を実際に
# 呼び出して再現検証できなかった(残り36件は real-docs フィクスチャの限界により
# 再構成不能で skip)。ここでは追加採取した材料フィールドが全100件に存在し、
# base64として正しくデコードでき、`contains_astral` フラグがデコード後の実内容と
# 一致することを固定する。
# ---------------------------------------------------------------------------


def test_text_b64_present_on_all_cases_and_decodes() -> None:
    """`text_b64` は `chunks.text`(NOT NULL)由来なので全100件に必須。

    `text_length` は採取スクリプト(Node)側で `text.length`(UTF-16 コード単位数)
    として定義されている。Python の `len(str)` はコードポイント単位で数えるため、
    astral 文字(サロゲートペア=UTF-16では2単位、Pythonでは1コードポイント)を
    含むテキストでは1文字あたり1ずつ差が出る。そのため単純な長さ一致ではなく、
    astral 文字の個数分を補正して比較する。
    """
    data = _load()
    decoded_count = 0
    for case in data["cases"]:
        assert case["text_b64"] is not None, f"{case['id']}: text_b64 が無い"
        try:
            decoded = base64.b64decode(case["text_b64"], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise AssertionError(
                f"{case['id']}: text_b64 が有効なUTF-8のbase64ではない: {exc}"
            ) from exc
        astral_chars = sum(1 for ch in decoded if ord(ch) > 0xFFFF)
        utf16_length = len(decoded) + astral_chars
        assert utf16_length == case["text_length"], (
            f"{case['id']}: text_b64 をデコードした文字列のUTF-16コード単位数が "
            f"text_length と一致しない(実際={utf16_length}, 期待={case['text_length']})"
        )
        decoded_count += 1
    assert decoded_count == 100


def test_title_and_heading_path_b64_are_string_or_null() -> None:
    """`chunks.title`/`chunks.heading_path` は NULL 許容の列なので、フィクスチャ側
    でも `null`(Python では `None`)か文字列のいずれかであるべき(数値や配列を
    誤って書いてしまう回帰を防ぐ)。null でなければ有効な base64 としてデコードできる
    こと。
    """
    data = _load()
    for case in data["cases"]:
        for key in ("title_b64", "heading_path_b64"):
            value = case[key]
            assert value is None or isinstance(value, str), (
                f"{case['id']}: {key} が null でも文字列でもない: {value!r}"
            )
            if value is not None:
                try:
                    base64.b64decode(value, validate=True)
                except binascii.Error as exc:
                    raise AssertionError(
                        f"{case['id']}: {key} が有効なbase64ではない: {exc}"
                    ) from exc


def test_contains_astral_is_boolean_on_every_case() -> None:
    data = _load()
    for case in data["cases"]:
        assert isinstance(case["contains_astral"], bool), (
            f"{case['id']}: contains_astral が真偽値ではない: {case['contains_astral']!r}"
        )


def test_contains_astral_flag_agrees_with_decoded_material() -> None:
    """`contains_astral` が title/heading_path/text をデコードした実内容の
    astral文字(コードポイント > 0xFFFF)有無と一致することを固定する
    (採取スクリプト側の判定ロジックの回帰防止。brief必須要件: astral母集団が
    問い合わせ可能であることの裏付け)。

    採取スクリプト(Node)はUTF-16コード単位の文字列上でサロゲートペアの有無を
    見て判定するが、Python の `str` はデコード後すでにコードポイント単位
    (astral文字は単一の文字)なので、ここでは同値な `ord(ch) > 0xFFFF` で判定する。
    """
    data = _load()
    astral_count = 0
    for case in data["cases"]:
        parts = []
        if case["title_b64"] is not None:
            parts.append(base64.b64decode(case["title_b64"]).decode("utf-8"))
        if case["heading_path_b64"] is not None:
            parts.append(base64.b64decode(case["heading_path_b64"]).decode("utf-8"))
        parts.append(base64.b64decode(case["text_b64"]).decode("utf-8"))
        has_astral = any(ord(ch) > 0xFFFF for ch in "".join(parts))
        assert has_astral == case["contains_astral"], (
            f"{case['id']}: contains_astral={case['contains_astral']} だが、"
            f"デコードした実内容の astral 文字有無は {has_astral}"
        )
        if has_astral:
            astral_count += 1
    assert astral_count > 0, "astral 文字を含むケースが1件も無い(判定ロジックが壊れている可能性)"


def test_input_hash_is_64_char_lowercase_hex() -> None:
    data = _load()
    for case in data["cases"]:
        assert _HEX64_RE.match(case["input_hash"]), (
            f"{case['id']}: input_hash が64桁小文字16進数ではない: {case['input_hash']!r}"
        )


def test_all_b64_fields_decode_as_valid_base64() -> None:
    data = _load()
    for key, value in _iter_b64_fields(data["cases"]):
        try:
            base64.b64decode(value, validate=True)
        except binascii.Error as exc:  # pragma: no cover - 失敗時に原因を明示する
            raise AssertionError(f"フィールド {key!r} が base64 として不正: {exc}") from exc


def test_every_vector_decodes_to_dimensions_times_4_bytes() -> None:
    """vector_b64 は Float32 LE の生バイト列であるべきで、その長さは常に
    dimensions * 4 バイトに一致する(brief必須要件)。"""
    data = _load()
    for case in data["cases"]:
        raw = base64.b64decode(case["vector_b64"])
        expected_len = case["dimensions"] * 4
        assert len(raw) == expected_len, (
            f"{case['id']}: vector_b64 のデコード長が dimensions*4 と一致しない"
            f"(実際={len(raw)}, 期待={expected_len})"
        )


def test_decoded_vectors_are_l2_normalized_within_tolerance() -> None:
    """保存済みベクトルは L2 正規化済みのはず(tools/lib/embeddings.js の
    saveEmbeddings が normalize() してから保存する)。M8 がここへ突き合わせる
    Python 側の再計算ベクトルも正規化済みである前提のため、採取物自体が
    本当に正規化されていることを NumPy で直接検証する(brief必須要件)。"""
    data = _load()
    checked = 0
    for case in data["cases"]:
        raw = base64.b64decode(case["vector_b64"])
        vector = np.frombuffer(raw, dtype="<f4")
        assert vector.shape[0] == case["dimensions"]
        norm = float(np.linalg.norm(vector))
        assert abs(norm - 1.0) < 1e-3, f"{case['id']}: L2ノルムが1.0から乖離している(norm={norm})"
        checked += 1
    assert checked == 100


def test_db_counts_recorded_for_both_index_databases() -> None:
    data = _load()
    db_counts = data["db_counts"]
    assert set(db_counts) == {"kb-index.sqlite", "reference-index.sqlite"}
    for name, entry in db_counts.items():
        counts = entry["counts"]
        for table in ("documents", "chunks", "embeddings"):
            assert isinstance(counts[table], int) and counts[table] >= 0, (
                f"{name}.{table} の件数が無い"
            )
        assert isinstance(entry["meta"], dict), f"{name}.meta が記録されていない"


def test_kb_index_meta_reports_e5_small_model() -> None:
    data = _load()
    meta = data["db_counts"]["kb-index.sqlite"]["meta"]
    assert meta.get("embedding_model") == "Xenova/multilingual-e5-small"


def test_no_secrets_leak_into_gate_samples() -> None:
    """本物の mask_secrets を全 *_b64 フィールドに適用しても変化が無いこと。"""
    data = _load()
    checked = 0
    for key, value in _iter_b64_fields(data["cases"]):
        try:
            decoded = base64.b64decode(value).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue  # vector_b64 は生バイナリのためUTF-8として解釈できないことが多い(想定通り)
        checked += 1
        masked = mask_secrets(decoded)
        assert masked == decoded, f"フィールド {key!r} に mask_secrets が反応する内容が残っている"
    assert checked > 0, "base64 コンテンツを1件も検証できなかった(テスト自体が空振り)"
