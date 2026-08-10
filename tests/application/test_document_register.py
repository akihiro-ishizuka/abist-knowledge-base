"""`application.document_register`(`abist-kb document register-disk`)。

「置くだけ / `index build` だけでは索引されない」という穴を埋める操作なので、
最後のテストで実際に `IndexService.build` の対象に入るところまで通しで確認する。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from abist_kb.application.document_register import register_disk_documents
from abist_kb.application.document_service import DocumentService
from abist_kb.application.index_service import IndexService
from abist_kb.infrastructure.db.schema import open_app_db


@pytest.fixture
def docs_dir(tmp_root: Path) -> Path:
    path = tmp_root / "docs"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def conn(tmp_root: Path) -> Iterator[sqlite3.Connection]:
    connection = open_app_db(tmp_root / "data" / "app.sqlite")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def service(conn: sqlite3.Connection) -> DocumentService:
    return DocumentService(conn)


def _write(docs_dir: Path, relative: str, content: str) -> None:
    target = docs_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_registers_hand_placed_markdown_as_manual(service: DocumentService, docs_dir: Path) -> None:
    _write(docs_dir, "notes/hello.md", "# こんにちは\n\n手で置いた文書です。\n")

    result = register_disk_documents(service, docs_dir, apply=True)

    assert result["registered"] == 1
    assert result["paths"] == ["notes/hello.md"]
    document = service.get("notes/hello.md")
    assert document["source"] == "manual"
    assert document["managed_by"] == "human"
    assert document["status"] == "active"
    assert document["title"] == "こんにちは"
    assert document["local_content_hash"]


def test_uses_classification_result_not_a_fixed_source(
    service: DocumentService, docs_dir: Path
) -> None:
    """frontmatter に post_number があれば分類は esa になる(manual 決め打ちにしない)。"""
    _write(
        docs_dir,
        "議事録/2026-08-10.md",
        "---\npost_number: 4952\ntitle: 定例\ncategory: 議事録/定例\n---\n\n本文\n",
    )

    register_disk_documents(service, docs_dir, apply=True)

    document = service.get("議事録/2026-08-10.md")
    assert document["source"] == "esa"
    assert document["post_number"] == 4952
    assert document["title"] == "定例"
    assert document["category"] == "議事録/定例"
    assert document["document_type"] == "meeting"


def test_existing_rows_are_skipped_and_never_overwritten(
    service: DocumentService, docs_dir: Path
) -> None:
    service.upsert({"path": "notes/hello.md", "source": "esa", "title": "esa 管理"})
    _write(docs_dir, "notes/hello.md", "# 別のタイトル\n\n本文\n")

    result = register_disk_documents(service, docs_dir, apply=True)

    assert result["registered"] == 0
    assert result["skipped_existing"] == 1
    document = service.get("notes/hello.md")
    assert document["source"] == "esa"
    assert document["title"] == "esa 管理"


def test_reference_corpus_subtrees_are_out_of_scope(
    service: DocumentService, docs_dir: Path
) -> None:
    _write(docs_dir, "knowledge/B32doc/prtug/a.md", "# 参照\n\n本文\n")
    _write(docs_dir, "knowledge/catiadoc/b.md", "# 参照\n\n本文\n")
    _write(docs_dir, "knowledge/generated/c.md", "# 参照\n\n本文\n")
    _write(docs_dir, "notes/hello.md", "# 通常\n\n本文\n")

    result = register_disk_documents(service, docs_dir, apply=True)

    assert result["scanned"] == 1
    assert result["paths"] == ["notes/hello.md"]
    assert service.get_or_none("knowledge/B32doc/prtug/a.md") is None
    assert service.get_or_none("knowledge/catiadoc/b.md") is None
    assert service.get_or_none("knowledge/generated/c.md") is None


def test_frontmatter_declared_reference_is_skipped(
    service: DocumentService, docs_dir: Path
) -> None:
    _write(docs_dir, "notes/ref.md", "---\ndocument_type: reference\n---\n\n本文\n")

    result = register_disk_documents(service, docs_dir, apply=True)

    assert result["registered"] == 0
    assert result["skipped_reference_paths"] == ["notes/ref.md"]
    assert service.get_or_none("notes/ref.md") is None


def test_dry_run_is_the_default_and_writes_nothing(
    service: DocumentService, docs_dir: Path
) -> None:
    _write(docs_dir, "notes/hello.md", "# こんにちは\n\n本文\n")

    result = register_disk_documents(service, docs_dir)

    assert result["applied"] is False
    assert result["paths"] == ["notes/hello.md"]
    assert service.get_or_none("notes/hello.md") is None


def test_paths_are_sorted_and_json_friendly(service: DocumentService, docs_dir: Path) -> None:
    for relative in ("z.md", "a.md", "m/n.md"):
        _write(docs_dir, relative, "# タイトル\n\n本文\n")

    result = register_disk_documents(service, docs_dir)

    assert result["paths"] == ["a.md", "m/n.md", "z.md"]


def test_registered_documents_become_index_build_targets(
    service: DocumentService, docs_dir: Path, tmp_root: Path
) -> None:
    _write(docs_dir, "notes/hello.md", "# CATIA起動\n\n起動時間を短縮する手順。\n")
    index_service = IndexService(
        docs_dir=docs_dir,
        app_db_path=tmp_root / "data" / "app.sqlite",
        work_index_path=tmp_root / "data" / "work-index.sqlite",
        reference_index_path=tmp_root / "data" / "reference-index.sqlite",
    )

    before = index_service.build("work")
    assert before["summary"]["documents_added"] == 0
    assert "notes/hello.md" in before["disk_only_paths_sample"]

    register_disk_documents(service, docs_dir, apply=True)
    after = index_service.build("work")

    assert after["summary"]["documents_added"] >= 1
    assert after["disk_only_count"] == 0
