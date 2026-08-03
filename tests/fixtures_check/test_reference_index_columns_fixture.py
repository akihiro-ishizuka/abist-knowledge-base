"""M4 Task1b で採取した `tests/fixtures/reference-index/columns.json` の健全性検証。

`capture-reference-index-columns.mjs` は旧 `data/reference-index.sqlite` の
`documents`/`embeddings`/`meta` テーブルをサンドボックスコピー経由で実測し、
reference コーパス(B32doc)の列値が M4 Task1 実装時点の推測
(`source="b32doc"`)とは異なり `source="manual"` であることを記録した。
ここでのテスト対象は Python 実装ではなく、旧 Node システムを実行して採取した
fixture 自体(その値を Python 側が正しく再現しているかは
`tests/search/test_corpus.py` 側で別途検証する)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "reference-index" / "columns.json"


def _load() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_fixture_has_valid_standard_envelope() -> None:
    data = _load()
    assert data["schema"] == 1
    assert isinstance(data["source"], str) and "reference-index.sqlite" in data["source"]


def test_total_documents_matches_known_row_count() -> None:
    """PROVENANCE.md §4 が記録する 7,563 行と一致すること。"""
    data = _load()
    assert data["total_documents"] == 7563


def test_all_columns_take_exactly_one_distinct_value() -> None:
    """reference コーパスは B32doc フィルタ経由の単一起源のため、
    source/document_type/status/post_number/url/category はいずれも1値に決まる。"""
    data = _load()
    distinct_values = data["distinct_values"]
    for column in ("source", "document_type", "status", "post_number", "url", "category"):
        values = distinct_values[column]
        assert len(values) == 1, (
            f"{column} が複数値を持つ(推測の余地がある想定と食い違う): {values}"
        )
        assert values[0]["count"] == 7563


def test_measured_column_values_match_provenance_record() -> None:
    """b32doc という値がどの列にも存在しないこと、実測値が manual/reference/active であること。"""
    data = _load()
    distinct_values = data["distinct_values"]
    assert distinct_values["source"][0]["value"] == "manual"
    assert distinct_values["document_type"][0]["value"] == "reference"
    assert distinct_values["status"][0]["value"] == "active"
    assert distinct_values["post_number"][0]["value"] is None
    assert distinct_values["url"][0]["value"] is None
    assert distinct_values["category"][0]["value"] == "html"

    all_values = {v["value"] for col in distinct_values.values() for v in col}
    assert "b32doc" not in all_values, "b32doc という値が旧DBのどの列にも存在しないはずが見つかった"


def test_embeddings_row_count_is_zero() -> None:
    data = _load()
    assert data["embeddings_row_count"] == 0


def test_meta_table_records_expected_tokenizers() -> None:
    data = _load()
    meta = {row["key"]: row["value"] for row in data["meta"]}
    assert meta["schema_version"] == "1"
    assert json.loads(meta["tokenizers"]) == ["unicode61", "trigram"]


def test_title_source_verification_confirms_summary_keys_not_filename() -> None:
    """title は `## Summary Keys` の `- タイトル:` 行由来であり、ファイル名由来ではない。"""
    data = _load()
    samples = data["title_source_verification"]
    assert len(samples) >= 5
    for sample in samples:
        assert sample["matches_extract_summary_keys"] is True
        assert sample["matches_filename"] is False
        assert sample["db_title"] == sample["extract_summary_keys_title"]
