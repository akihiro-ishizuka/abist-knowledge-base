"""`SourceService`: CRUD と接続テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.source_service import SourceService
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema


@pytest.fixture
def service(tmp_root: Path) -> SourceService:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    return SourceService(conn)


def test_add_and_get(service: SourceService) -> None:
    created = service.add(
        type="esa", display_name="esa本体", connection={"team": "abist"}, output_dir="docs/esa"
    )
    assert service.get(created["id"])["display_name"] == "esa本体"


def test_get_raises_not_found(service: SourceService) -> None:
    with pytest.raises(AppError) as excinfo:
        service.get("missing")
    assert excinfo.value.code == ErrorCode.NOT_FOUND


def test_edit_partial_update(service: SourceService) -> None:
    created = service.add(type="web", display_name="Old", connection={}, output_dir="docs/old")
    updated = service.edit(created["id"], display_name="New")
    assert updated["display_name"] == "New"


def test_remove_requires_confirmation(service: SourceService) -> None:
    created = service.add(type="web", display_name="X", connection={}, output_dir="docs/x")
    assert service.remove(created["id"], confirm=lambda _p: False) is False
    assert service.get(created["id"]) is not None
    assert service.remove(created["id"], confirm=lambda _p: True) is True
    with pytest.raises(AppError):
        service.get(created["id"])


def test_test_connection_unknown_type_reports_failure(service: SourceService) -> None:
    created = service.add(type="unknown-type", display_name="X", connection={}, output_dir="docs/x")
    result = service.test_connection(created["id"])
    assert result["ok"] is False


def test_test_connection_esa_missing_required_fields_reports_failure(
    service: SourceService,
) -> None:
    created = service.add(type="esa", display_name="esa", connection={}, output_dir="docs/esa")
    result = service.test_connection(created["id"])
    assert result["ok"] is False
    assert "team" in result["detail"] or "token" in result["detail"]


def test_test_connection_esa_with_required_fields_reports_ok(service: SourceService) -> None:
    created = service.add(
        type="esa",
        display_name="esa",
        connection={"team": "abist", "access_token": "secret"},
        output_dir="docs/esa",
    )
    result = service.test_connection(created["id"])
    assert result["ok"] is True
