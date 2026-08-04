"""`application.curation.promote`(M7 task-5): `curate promote` の移植先ロジック。

旧実装 `tools/knowledge-curator/promote.py` の挙動(索引から1件見つけて
`status: draft` のドラフトを書き出す・原本は変更しない)を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.application.curation.promote import (
    RecordNotFoundError,
    SourceMissingError,
    find_record,
    parse_front_matter,
    parse_page_links,
    promote,
    split_sections,
)

_SOURCE_MD = """---
category: "html"
language: "ja"
source_name: "prtugbt0501.htm"
ext: ".htm"
tags:
  - "パート"
  - "スケッチ"
---

## Summary Keys

- タイトル: サンプル手順
- 概要: これはサンプルです
- 見出し: 概要

## Extracted Content

# サンプル手順

これは抽出された本文です。

## Structured Data

## Links

- (関連ページ) -> ../../online/Japanese/prtug_C2/prtugbt0502.htm
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    md_dir = root / "docs" / "knowledge" / "B32doc" / "md_out" / "online" / "Japanese" / "prtug_C2"
    md_dir.mkdir(parents=True)
    (md_dir / "prtugbt0501.htm.md").write_text(_SOURCE_MD, encoding="utf-8")

    catalog_dir = root / "docs" / "knowledge" / "generated" / "b32doc" / "catalog"
    catalog_dir.mkdir(parents=True)
    record = {
        "id": "prtug/prtugbt0501",
        "module": "prtug_C2",
        "module_short": "prtug",
        "workbench": None,
        "source_name": "prtugbt0501.htm",
        "title": "サンプル手順",
        "path": "docs/knowledge/B32doc/md_out/online/Japanese/prtug_C2/prtugbt0501.htm.md",
        "language": "ja",
        "tags": ["パート", "スケッチ"],
    }
    (catalog_dir / "procedures-index.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return root


def test_parse_front_matter_extracts_scalars_and_lists():
    meta, body = parse_front_matter(_SOURCE_MD)
    assert meta["category"] == "html"
    assert meta["tags"] == ["パート", "スケッチ"]
    assert body.startswith("\n## Summary Keys")


def test_split_sections_finds_all_four_headers():
    _meta, body = parse_front_matter(_SOURCE_MD)
    sections = split_sections(body)
    assert "サンプル手順" in sections["## Extracted Content"]
    assert "タイトル: サンプル手順" in sections["## Summary Keys"]


def test_parse_page_links_rewrites_htm_link_to_target_id():
    _meta, body = parse_front_matter(_SOURCE_MD)
    sections = split_sections(body)
    links = parse_page_links(sections["## Links"], "prtug/prtugbt0501")
    assert links == [{"text": "関連ページ", "target_id": "prtug/prtugbt0502", "anchor": None}]


_INDEX_RELATIVE = (
    Path("docs") / "knowledge" / "generated" / "b32doc" / "catalog" / "procedures-index.jsonl"
)


def test_find_record_returns_matching_id(repo: Path):
    index_path = repo / _INDEX_RELATIVE
    rec = find_record(index_path, "prtug/prtugbt0501")
    assert rec is not None
    assert rec["title"] == "サンプル手順"


def test_find_record_returns_none_for_unknown_id(repo: Path):
    index_path = repo / _INDEX_RELATIVE
    assert find_record(index_path, "unknown/id") is None


def test_promote_writes_draft_with_status_draft_and_leaves_original_untouched(repo: Path):
    original_md = repo / "docs/knowledge/B32doc/md_out/online/Japanese/prtug_C2/prtugbt0501.htm.md"
    original_bytes_before = original_md.read_bytes()

    result = promote(record_id="prtug/prtugbt0501", repo_root=repo)

    assert result.written is True
    assert result.output_path.exists()
    content = result.output_path.read_text(encoding="utf-8")
    assert "status: draft" in content
    assert "prtug/prtugbt0501" in content
    assert "これは抽出された本文です。" in content
    assert original_md.read_bytes() == original_bytes_before


def test_promote_dry_run_does_not_write(repo: Path):
    result = promote(record_id="prtug/prtugbt0501", repo_root=repo, dry_run=True)
    assert result.written is False
    assert not result.output_path.exists()


def test_promote_unknown_id_raises_record_not_found(repo: Path):
    with pytest.raises(RecordNotFoundError):
        promote(record_id="unknown/id", repo_root=repo)


def test_promote_missing_source_raises_source_missing(repo: Path):
    index_path = repo / _INDEX_RELATIVE
    record = {
        "id": "prtug/missing",
        "module": "prtug_C2",
        "source_name": "missing.htm",
        "title": "無い",
        "path": "docs/knowledge/B32doc/md_out/online/Japanese/prtug_C2/missing.htm.md",
        "language": "ja",
        "tags": [],
    }
    with index_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    with pytest.raises(SourceMissingError):
        promote(record_id="prtug/missing", repo_root=repo)


def test_promote_existing_output_without_force_raises_file_exists(repo: Path):
    promote(record_id="prtug/prtugbt0501", repo_root=repo)
    with pytest.raises(FileExistsError):
        promote(record_id="prtug/prtugbt0501", repo_root=repo)


def test_promote_existing_output_with_force_overwrites(repo: Path):
    promote(record_id="prtug/prtugbt0501", repo_root=repo)
    result = promote(record_id="prtug/prtugbt0501", repo_root=repo, force=True)
    assert result.written is True
