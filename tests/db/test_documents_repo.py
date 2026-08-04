"""`DocumentRepository`: 旧 `tools/lib/sync-state.js` の `documents` テーブル互換層。

旧実装が保証していた2つの意味論を明示的に検証する:

- 部分upsert: レコードに含まれない列は既存値を保持し、`WRITABLE_COLUMNS` に
  無いキーは無視する(`sync-state.js` の `upsertDocument` と同じ契約)。
- パス正規化: Windows区切り(`\\`)は POSIX(`/`)へ正規化してから主キーとして使う。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import ALL_COLUMNS, DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema


@pytest.fixture
def repo(tmp_root: Path) -> DocumentRepository:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    return DocumentRepository(conn)


def test_all_columns_matches_old_sync_state_27_plus_uuid_and_source_id() -> None:
    """旧 sync-state.js の27列 + uuid + source_id = 29列。"""
    assert len(ALL_COLUMNS) == 29
    assert ALL_COLUMNS[0] == "path"
    assert "uuid" in ALL_COLUMNS
    assert "source_id" in ALL_COLUMNS
    # 旧27列は1つも欠落・改名していないこと
    old_columns = {
        "path",
        "source",
        "managed_by",
        "document_type",
        "status",
        "title",
        "url",
        "post_number",
        "category",
        "source_key",
        "source_updated_at",
        "sync_status",
        "source_content_hash",
        "local_content_hash",
        "downloaded_at",
        "last_checked_at",
        "etag",
        "last_modified",
        "missing_count",
        "missing_since",
        "sync_error",
        "indexed_at",
        "embedding_model",
        "embedding_dimensions",
        "embedding_hash",
        "created_at",
        "updated_at",
    }
    assert len(old_columns) == 27
    assert old_columns <= set(ALL_COLUMNS)


def test_upsert_creates_new_document_and_generates_uuid(repo: DocumentRepository) -> None:
    repo.upsert({"path": "knowledge/a.md", "source": "esa", "title": "A"})
    doc = repo.get("knowledge/a.md")
    assert doc is not None
    assert doc["title"] == "A"
    assert doc["source"] == "esa"
    assert doc["uuid"]  # 自動生成される
    assert doc["created_at"] == doc["updated_at"]


def test_upsert_preserves_uuid_across_partial_updates(repo: DocumentRepository) -> None:
    repo.upsert({"path": "knowledge/a.md", "title": "A"})
    first_uuid = repo.get("knowledge/a.md")["uuid"]
    repo.upsert({"path": "knowledge/a.md", "title": "A2"})
    assert repo.get("knowledge/a.md")["uuid"] == first_uuid


def test_partial_upsert_keeps_absent_columns_unchanged(repo: DocumentRepository) -> None:
    """欠落列は既存値を保持する(last_checked_at だけ更新してもハッシュは消えない)。"""
    repo.upsert(
        {
            "path": "knowledge/a.md",
            "source_content_hash": "abc123",
            "local_content_hash": "def456",
            "last_checked_at": "2026-01-01T00:00:00+00:00",
        }
    )
    repo.upsert({"path": "knowledge/a.md", "last_checked_at": "2026-01-02T00:00:00+00:00"})
    doc = repo.get("knowledge/a.md")
    assert doc["source_content_hash"] == "abc123"
    assert doc["local_content_hash"] == "def456"
    assert doc["last_checked_at"] == "2026-01-02T00:00:00+00:00"


def test_partial_upsert_ignores_unknown_keys(repo: DocumentRepository) -> None:
    """`WRITABLE_COLUMNS` に無いキーは無視され、例外にもならない。"""
    repo.upsert({"path": "knowledge/a.md", "title": "A", "totally_unknown_key": "boom"})
    doc = repo.get("knowledge/a.md")
    assert doc["title"] == "A"
    assert "totally_unknown_key" not in doc


def test_upsert_with_no_writable_fields_only_touches_updated_at(repo: DocumentRepository) -> None:
    repo.upsert({"path": "knowledge/a.md", "title": "A"})
    before = repo.get("knowledge/a.md")
    repo.upsert({"path": "knowledge/a.md"})
    after = repo.get("knowledge/a.md")
    assert after["title"] == before["title"]
    assert after["updated_at"] >= before["updated_at"]


def test_upsert_requires_path(repo: DocumentRepository) -> None:
    with pytest.raises(AppError):
        repo.upsert({"title": "no path"})


def test_upsert_normalizes_windows_separators_to_posix(repo: DocumentRepository) -> None:
    repo.upsert({"path": "knowledge\\sub\\a.md", "title": "A"})
    assert repo.get("knowledge/sub/a.md") is not None
    assert repo.get("knowledge\\sub\\a.md") is not None  # 呼び出し側も正規化して探す


def test_upsert_windows_and_posix_paths_are_the_same_document(repo: DocumentRepository) -> None:
    """同じ文書を Windows 区切りと POSIX 区切りで別々に upsert しても1行にまとまる。"""
    repo.upsert({"path": "knowledge\\sub\\a.md", "title": "first"})
    repo.upsert({"path": "knowledge/sub/a.md", "title": "second"})
    doc = repo.get("knowledge/sub/a.md")
    assert doc["title"] == "second"
    all_docs = repo.list()
    assert len(all_docs) == 1


def test_list_filters_by_source(repo: DocumentRepository) -> None:
    repo.upsert({"path": "a.md", "source": "esa"})
    repo.upsert({"path": "b.md", "source": "web"})
    result = repo.list(source="esa")
    assert [d["path"] for d in result] == ["a.md"]


def test_list_filters_by_sync_status(repo: DocumentRepository) -> None:
    repo.upsert({"path": "a.md", "sync_status": "synced"})
    repo.upsert({"path": "b.md", "sync_status": "conflict"})
    result = repo.list(sync_status="conflict")
    assert [d["path"] for d in result] == ["b.md"]


def test_list_filters_by_status(repo: DocumentRepository) -> None:
    repo.upsert({"path": "a.md", "status": "active"})
    repo.upsert({"path": "b.md", "status": "archived"})
    result = repo.list(status="archived")
    assert [d["path"] for d in result] == ["b.md"]


def test_list_filters_by_path_prefix(repo: DocumentRepository) -> None:
    repo.upsert({"path": "knowledge/a.md"})
    repo.upsert({"path": "knowledge/sub/b.md"})
    repo.upsert({"path": "other/c.md"})
    result = repo.list(path_prefix="knowledge")
    assert {d["path"] for d in result} == {"knowledge/a.md", "knowledge/sub/b.md"}


def test_list_filters_by_category_prefix_includes_subcategories(
    repo: DocumentRepository,
) -> None:
    repo.upsert({"path": "a.md", "category": "設計効率化"})
    repo.upsert({"path": "b.md", "category": "設計効率化/三桜工業様"})
    repo.upsert({"path": "c.md", "category": "他カテゴリ"})
    result = repo.list(category_prefix="設計効率化")
    assert {d["path"] for d in result} == {"a.md", "b.md"}


def test_list_managed_only_excludes_human_managed(repo: DocumentRepository) -> None:
    repo.upsert({"path": "a.md", "managed_by": "esa-sync"})
    repo.upsert({"path": "b.md", "managed_by": "human"})
    repo.upsert({"path": "c.md", "managed_by": None})
    result = repo.list(managed_only=True)
    assert [d["path"] for d in result] == ["a.md"]


def test_count_by_source(repo: DocumentRepository) -> None:
    repo.upsert({"path": "a.md", "source": "esa"})
    repo.upsert({"path": "b.md", "source": "esa"})
    repo.upsert({"path": "c.md", "source": "web"})
    assert repo.count_by_source() == {"esa": 2, "web": 1}


def test_mark_missing_increments_count_and_sets_since_once(repo: DocumentRepository) -> None:
    repo.upsert({"path": "a.md"})
    count1 = repo.mark_missing("a.md", at="2026-01-01T00:00:00+00:00")
    count2 = repo.mark_missing("a.md", at="2026-01-02T00:00:00+00:00")
    assert count1 == 1
    assert count2 == 2
    doc = repo.get("a.md")
    assert doc["missing_since"] == "2026-01-01T00:00:00+00:00"  # 最初の値を保持


def test_clear_missing_resets_count_and_since(repo: DocumentRepository) -> None:
    repo.upsert({"path": "a.md"})
    repo.mark_missing("a.md")
    repo.clear_missing("a.md")
    doc = repo.get("a.md")
    assert doc["missing_count"] == 0
    assert doc["missing_since"] is None


def test_delete_removes_document_and_reports_deletion(repo: DocumentRepository) -> None:
    repo.upsert({"path": "a.md"})
    assert repo.delete("a.md") is True
    assert repo.get("a.md") is None
    assert repo.delete("a.md") is False


def test_get_returns_none_for_absent_document(repo: DocumentRepository) -> None:
    assert repo.get("does/not/exist.md") is None
