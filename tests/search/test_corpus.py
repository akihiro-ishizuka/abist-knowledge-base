"""`select_work_targets`/`select_reference_targets`(brief Step5)。"""

from __future__ import annotations

from pathlib import Path

from abist_kb.infrastructure.search.corpus import (
    select_reference_targets,
    select_work_targets,
)


def _write(docs_dir: Path, path: str, content: str) -> None:
    full = docs_dir / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _doc(path: str, **overrides) -> dict:
    base = {
        "path": path,
        "post_number": None,
        "title": "タイトル",
        "source": "esa",
        "document_type": "article",
        "status": "active",
        "url": None,
        "category": None,
    }
    base.update(overrides)
    return base


# -- select_work_targets -----------------------------------------------------


def test_selects_documents_with_work_sources_and_excludes_reference_type(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    documents = [
        _doc("a.md", source="esa"),
        _doc("b.md", source="web"),
        _doc("c.md", source="git"),
        _doc("d.md", source="manual"),
        _doc("e.md", source="esa", document_type="reference"),  # 除外
        _doc("f.md", source="unknown-source"),  # 除外
    ]
    result = select_work_targets(documents, docs_dir)
    paths = {t["path"] for t in result.targets}
    assert paths == {"a.md", "b.md", "c.md", "d.md"}


def test_target_dict_carries_metadata_columns_needed_for_indexing(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    documents = [
        _doc(
            "a.md",
            post_number=7,
            title="記事A",
            source="esa",
            document_type="article",
            status="active",
            url="https://example.com/a",
            category="tech/foo",
        )
    ]
    result = select_work_targets(documents, docs_dir)
    assert result.targets[0] == {
        "path": "a.md",
        "post_number": 7,
        "title": "記事A",
        "source": "esa",
        "document_type": "article",
        "status": "active",
        "url": "https://example.com/a",
        "category": "tech/foo",
    }


def test_backslash_paths_are_normalized_to_posix(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    documents = [_doc("a\\b\\c.md", source="esa")]
    result = select_work_targets(documents, docs_dir)
    assert result.targets[0]["path"] == "a/b/c.md"


def test_detects_files_on_disk_missing_from_documents_table(tmp_root: Path):
    """PROVENANCE.md §4: DB を完全な台帳と仮定せず、docs/ にあって DB に無いパスを検出する。"""
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "knowledge/catiadoc/orphan1.md", "内容")
    _write(docs_dir, "knowledge/catiadoc/orphan2.md", "内容")
    _write(docs_dir, "esa/known.md", "内容")

    documents = [_doc("esa/known.md", source="esa")]
    result = select_work_targets(documents, docs_dir)

    assert result.disk_only_paths == (
        "knowledge/catiadoc/orphan1.md",
        "knowledge/catiadoc/orphan2.md",
    )
    assert result.disk_only_count == 2


def test_disk_reconciliation_excludes_reference_corpus_subtree(tmp_root: Path):
    """B32doc は select_reference_targets の管轄であり、work 側の discrepancy には出さない。"""
    docs_dir = tmp_root / "docs"
    for i in range(5):
        _write(docs_dir, f"knowledge/B32doc/page{i}.md", "内容")

    result = select_work_targets([], docs_dir)
    assert result.disk_only_paths == ()
    assert result.disk_only_count == 0


def test_disk_reconciliation_only_considers_markdown_files(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "assets/image_meta.json", "{}")
    result = select_work_targets([], docs_dir)
    assert result.disk_only_paths == ()


def test_missing_docs_dir_yields_no_disk_only_paths(tmp_root: Path):
    docs_dir = tmp_root / "does-not-exist"
    result = select_work_targets([_doc("a.md", source="esa")], docs_dir)
    assert result.disk_only_paths == ()
    assert result.targets[0]["path"] == "a.md"


# -- select_reference_targets -------------------------------------------------

_B32DOC_FRONTMATTER = "---\r\nsource_name: page.htm\r\ncategory: html\r\nlanguage: mixed\r\n---\r\n"


def _b32doc_body(title: str, extracted_chars: int = 250) -> str:
    extracted = "本" * extracted_chars
    return (
        "## Summary Keys\r\n"
        "\r\n"
        f"- タイトル: {title}\r\n"
        "- 概要: 概要文\r\n"
        "\r\n"
        "## Extracted Content\r\n"
        "\r\n"
        f"{extracted}\r\n"
    )


def test_reference_targets_are_selected_via_b32doc_filter(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(
        docs_dir,
        "knowledge/B32doc/md_out/a.md",
        _B32DOC_FRONTMATTER + _b32doc_body("整備手順の説明"),
    )
    result = select_reference_targets(docs_dir)
    assert result.total_scanned == 1
    assert len(result.targets) == 1
    target = result.targets[0]
    assert target["path"] == "knowledge/B32doc/md_out/a.md"
    assert target["document_type"] == "reference"


def test_reference_target_title_comes_from_summary_keys_not_filename(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    _write(
        docs_dir,
        "knowledge/B32doc/md_out/some_weird_filename_hash123.md",
        _B32DOC_FRONTMATTER + _b32doc_body("本当のタイトル"),
    )
    result = select_reference_targets(docs_dir)
    assert result.targets[0]["title"] == "本当のタイトル"


def test_excluded_reference_documents_are_counted_by_reason(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    # path_excluded
    _write(
        docs_dir,
        "knowledge/B32doc/images/x.md",
        _B32DOC_FRONTMATTER + _b32doc_body("画像だけのページ"),
    )
    # too_short
    short_body = (
        "## Summary Keys\r\n\r\n- タイトル: 短い\r\n\r\n## Extracted Content\r\n\r\n短い\r\n"
    )
    _write(docs_dir, "knowledge/B32doc/md_out/short.md", _B32DOC_FRONTMATTER + short_body)
    # accepted
    _write(
        docs_dir,
        "knowledge/B32doc/md_out/ok.md",
        _B32DOC_FRONTMATTER + _b32doc_body("OK"),
    )
    result = select_reference_targets(docs_dir)
    assert result.total_scanned == 3
    assert len(result.targets) == 1
    assert result.excluded_reasons.get("path_excluded") == 1
    assert result.excluded_reasons.get("too_short") == 1


def test_reference_targets_empty_when_b32doc_dir_missing(tmp_root: Path):
    docs_dir = tmp_root / "docs"
    result = select_reference_targets(docs_dir)
    assert result.targets == ()
    assert result.total_scanned == 0
    assert result.excluded_reasons == {}
