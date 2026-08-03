"""`DocumentService`: 一覧・取得・メタデータ更新・安全削除(ブリーフ Step 4)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.document_service import DocumentService
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema


@pytest.fixture
def service(tmp_root: Path) -> DocumentService:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    return DocumentService(conn)


def test_list_delegates_to_repository_filters(service: DocumentService) -> None:
    service.upsert({"path": "a.md", "source": "esa"})
    service.upsert({"path": "b.md", "source": "web"})
    result = service.list(source="esa")
    assert [d["path"] for d in result] == ["a.md"]


def test_get_raises_not_found_for_missing_document(service: DocumentService) -> None:
    with pytest.raises(AppError) as excinfo:
        service.get("does/not/exist.md")
    assert excinfo.value.code == ErrorCode.NOT_FOUND


def test_get_returns_existing_document(service: DocumentService) -> None:
    service.upsert({"path": "a.md", "title": "A"})
    doc = service.get("a.md")
    assert doc["title"] == "A"


def test_update_metadata_is_partial(service: DocumentService) -> None:
    service.upsert({"path": "a.md", "title": "A", "status": "active"})
    service.update_metadata("a.md", {"status": "archived"})
    doc = service.get("a.md")
    assert doc["status"] == "archived"
    assert doc["title"] == "A"


def test_update_metadata_raises_not_found_for_missing_document(service: DocumentService) -> None:
    with pytest.raises(AppError) as excinfo:
        service.update_metadata("missing.md", {"status": "archived"})
    assert excinfo.value.code == ErrorCode.NOT_FOUND


def test_delete_requires_confirmation_and_records_audit(service: DocumentService) -> None:
    service.upsert({"path": "a.md"})
    confirmations: list[str] = []

    def confirm(prompt: str) -> bool:
        confirmations.append(prompt)
        return True

    service.delete("a.md", confirm=confirm, actor="tester")
    assert service.get_or_none("a.md") is None
    assert len(confirmations) == 1

    audit_row = service._conn.execute(  # noqa: SLF001 - テストのみ内部確認
        "SELECT * FROM audit_events WHERE target_type = 'document'"
    ).fetchone()
    assert audit_row["target_id"] == "a.md"
    assert audit_row["actor"] == "tester"


def test_delete_aborts_when_not_confirmed(service: DocumentService) -> None:
    service.upsert({"path": "a.md"})
    service.delete("a.md", confirm=lambda _prompt: False, actor="tester")
    assert service.get_or_none("a.md") is not None


def test_delete_raises_not_found_for_missing_document(service: DocumentService) -> None:
    with pytest.raises(AppError) as excinfo:
        service.delete("missing.md", confirm=lambda _prompt: True)
    assert excinfo.value.code == ErrorCode.NOT_FOUND
