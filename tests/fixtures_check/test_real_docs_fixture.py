"""M1 Task 2 で採取した実データ層化サンプル (`tests/fixtures/real-docs/samples.json`) の健全性検証。

`capture-real-docs.mjs` は旧 Node システムの `data/sync-state.sqlite`（read-only）から
実文書を層化抽出し、Task 1 (`test_fixture_integrity.py`) と同じ枠組みでカーネル出力
(parseFrontmatter / hashBody / chunker / rangeHash / e5 embedding input) を記録した
ものである。ここでのテスト対象は Python 実装ではなく、採取された fixture 自体。

検証する性質:
  - 9層すべてが manifest に「実際の件数」付きで記録されていること（0件も含めて）
  - BOM 層・CRLF 層は空でないこと（空なら層化クエリが壊れている: brief の指摘）
  - 除外（64KB超過・秘密情報検知）が理由付きで記録され、黙って落とされていないこと
  - 層をまたいで同じ path が重複選択されていないこと
  - 採取済みサンプルの本文に、M0 の本物の `mask_secrets` を適用しても変化が無いこと
    （JS 側の簡易ミラーが取りこぼしていないかの、Python 側からの独立検証）
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from abist_kb.domain.redaction import mask_secrets

FIXTURES_REAL_DOCS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "real-docs"
SAMPLES_PATH = FIXTURES_REAL_DOCS_DIR / "samples.json"

# brief (task-2-brief.md) が列挙する9層。順序は brief の表と一致させている。
EXPECTED_LAYERS = [
    "esa",
    "web",
    "git",
    "reference",
    "japanese_path",
    "long_path",
    "bom_prefixed",
    "crlf_body",
    "no_frontmatter",
]

# Task1 の fixture (kernel/frontmatter.json 等) が BOM 付き・CRLF 混在の実例を
# 転記していることから、この実データ層化サンプルでも BOM 層・CRLF 層が
# 空になることは無いはずである。空なら層化クエリ自体が壊れている。
LAYERS_THAT_MUST_BE_NON_EMPTY = {"bom_prefixed", "crlf_body"}


def _load() -> dict[str, Any]:
    data = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
    assert data, f"real-docs フィクスチャが空: {SAMPLES_PATH}"
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


def test_samples_json_has_valid_schema() -> None:
    data = _load()
    assert data["schema"] == 1
    assert isinstance(data["source"], str) and data["source"]
    assert isinstance(data["layers"], list)
    assert isinstance(data["exclusions"], list)
    assert isinstance(data["cases"], list)
    assert len(data["cases"]) > 0, "採取されたサンプルが1件も無い"


def test_case_ids_are_unique() -> None:
    data = _load()
    ids = [case["id"] for case in data["cases"]]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"重複した case id がある: {duplicates}"


def test_all_b64_fields_decode_as_valid_base64() -> None:
    data = _load()
    for key, value in _iter_b64_fields(data["cases"]):
        try:
            base64.b64decode(value, validate=True)
        except binascii.Error as exc:  # pragma: no cover - 失敗時に原因を明示する
            raise AssertionError(f"フィールド {key!r} が base64 として不正: {exc}") from exc


def test_all_nine_layers_are_recorded_with_actual_counts() -> None:
    """brief の9層すべてが manifest に「実際の件数」付きで記録されていること。

    件数が0であること自体は許容する（例: このコーパスの sync-state.sqlite には
    source='web' の行も document_type='reference' の行も実在しない —
    reference corpus は reference-index.sqlite 側に source='manual' で
    別管理されているという実データの構造そのものであり、層化クエリの不具合
    ではない。0件も「実際の件数」として正直に記録されていることを確認する）。
    """
    data = _load()
    layers_by_name = {layer["name"]: layer for layer in data["layers"]}
    assert set(layers_by_name) == set(EXPECTED_LAYERS), (
        f"9層が揃っていない: 期待={sorted(EXPECTED_LAYERS)} 実際={sorted(layers_by_name)}"
    )
    for name, layer in layers_by_name.items():
        assert isinstance(layer["target_count"], int) and layer["target_count"] > 0
        assert isinstance(layer["matching_total"], int) and layer["matching_total"] >= 0
        assert isinstance(layer["selected_count"], int) and layer["selected_count"] >= 0
        assert layer["selected_count"] <= layer["target_count"], (
            f"{name}: selected_count が target_count を超えている"
        )
        assert layer["selected_count"] <= layer["matching_total"], (
            f"{name}: selected_count が matching_total を超えている"
        )
        assert len(layer["selected_paths"]) == layer["selected_count"]


def test_bom_and_crlf_layers_are_non_empty() -> None:
    """BOM 層・CRLF 層が空でないこと（空なら層化クエリが壊れている: brief の指摘）。"""
    data = _load()
    layers_by_name = {layer["name"]: layer for layer in data["layers"]}
    for name in LAYERS_THAT_MUST_BE_NON_EMPTY:
        layer = layers_by_name[name]
        assert layer["selected_count"] > 0, f"{name} 層が空 — 層化クエリが壊れている可能性"
        assert layer["matching_total"] > 0, (
            f"{name} 層の母集団自体が0件 — 層化クエリが壊れている可能性"
        )


def test_selected_paths_are_unique_across_layers() -> None:
    """brief: 「重複は除く」— 同じ path が複数層にまたがって選択されていないこと。"""
    data = _load()
    all_selected = [path for layer in data["layers"] for path in layer["selected_paths"]]
    duplicates = {p for p in all_selected if all_selected.count(p) > 1}
    assert not duplicates, f"複数層にまたがって重複選択された path がある: {duplicates}"

    case_paths = [case["path"] for case in data["cases"]]
    assert sorted(case_paths) == sorted(all_selected), (
        "layers[].selected_paths と cases[].path が一致しない"
    )


def test_exclusions_are_recorded_with_reasons() -> None:
    """brief: 除外件数と理由を manifest に記録する（黙って落とさない）。"""
    data = _load()
    exclusions = data["exclusions"]
    assert isinstance(exclusions, list)
    for excl in exclusions:
        assert excl.get("path"), "除外エントリに path が無い"
        assert excl.get("layer"), "除外エントリに layer が無い"
        reason = excl.get("reason")
        assert isinstance(reason, str) and reason, "除外エントリに reason が無い"
        assert reason == "size_exceeds_64kb" or reason.startswith("secret_pattern:"), (
            f"未知の除外理由: {reason}"
        )


def test_excluded_paths_are_not_also_selected() -> None:
    data = _load()
    excluded_paths = {excl["path"] for excl in data["exclusions"]}
    selected_paths = {case["path"] for case in data["cases"]}
    overlap = excluded_paths & selected_paths
    assert not overlap, f"除外されたはずの path がサンプルにも含まれている: {overlap}"


def test_no_secrets_leak_into_included_samples() -> None:
    """採取済みサンプルの本文コンテンツに、本物の mask_secrets を適用しても
    変化が無いこと。

    capture-real-docs.mjs は JS 側に mask_secrets のパターンを簡易ミラーして
    除外を判断している。このテストは Python 側の本物の実装を全 *_b64
    コンテンツに適用し直すことで、JS ミラーが取りこぼした秘密情報が無い
    ことを独立に検証する（brief: 「Python 側から呼び出して検査するか...
    同等の正規表現をJS側に持つ...どちらか実際に検証できる方法で」に対応）。
    """
    data = _load()
    checked = 0
    for key, value in _iter_b64_fields(data["cases"]):
        try:
            decoded = base64.b64decode(value).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue
        checked += 1
        masked = mask_secrets(decoded)
        assert masked == decoded, (
            f"フィールド {key!r} に mask_secrets が反応する内容が残っている"
            "（JS 側の秘密情報スキャンが取りこぼした可能性）"
        )
    assert checked > 0, "base64 コンテンツを1件も検証できなかった（テスト自体が空振り）"


def test_each_case_has_kernel_outputs() -> None:
    """Task 1 と同じ形式（parseFrontmatter / hashBody / chunker / rangeHash / e5入力）
    が各サンプルに記録されていること。"""
    data = _load()
    for case in data["cases"]:
        expected = case["expected"]
        fm = expected["frontmatter"]
        assert "hasFrontmatter" in fm
        assert "hashBody" in fm and isinstance(fm["hashBody"], str) and len(fm["hashBody"]) == 64

        chunker = expected["chunker"]
        assert chunker["chunkCount"] == len(chunker["chunks"])

        line_range = expected["line_range"]
        assert line_range["totalLines"] >= 1
        assert line_range["firstLine"]["ok"] is True
        assert line_range["lastLine"]["ok"] is True
        assert line_range["fullRange"]["ok"] is True

        # embedding は文書にチャンクが1つ以上ある場合にのみ記録される
        if chunker["chunkCount"] > 0:
            assert expected["embedding"] is not None
            assert expected["embedding"]["model"] == "Xenova/multilingual-e5-small"
            assert isinstance(expected["embedding"]["embeddingInputHash"], str)
        else:
            assert expected["embedding"] is None
