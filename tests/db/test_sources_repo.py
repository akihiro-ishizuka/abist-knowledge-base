"""`SourceRepository`: `sources` テーブルの CRUD。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.infrastructure.db.sources_repo import SourceRepository


@pytest.fixture
def repo(tmp_root: Path) -> SourceRepository:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    return SourceRepository(conn)


def test_create_assigns_id_and_defaults(repo: SourceRepository) -> None:
    source = repo.create(
        type="esa", display_name="esa本体", connection={"team": "abist"}, output_dir="docs/esa"
    )
    assert source["id"]
    assert source["type"] == "esa"
    assert source["connection"] == {"team": "abist"}
    assert source["enabled"] is True


def test_get_round_trips_connection_json(repo: SourceRepository) -> None:
    created = repo.create(
        type="git",
        display_name="repo",
        connection={"repository": "https://example.com/x.git", "branch": "main"},
        output_dir="docs/x",
    )
    fetched = repo.get(created["id"])
    assert fetched is not None
    assert fetched["connection"] == {"repository": "https://example.com/x.git", "branch": "main"}


def test_get_returns_none_for_missing_id(repo: SourceRepository) -> None:
    assert repo.get("does-not-exist") is None


def test_list_returns_all_sources_ordered_by_display_name(repo: SourceRepository) -> None:
    repo.create(type="web", display_name="Zeta", connection={}, output_dir="docs/z")
    repo.create(type="web", display_name="Alpha", connection={}, output_dir="docs/a")
    names = [s["display_name"] for s in repo.list()]
    assert names == ["Alpha", "Zeta"]


def test_update_partially_changes_fields(repo: SourceRepository) -> None:
    created = repo.create(
        type="web", display_name="Old", connection={"url": "https://old"}, output_dir="docs/old"
    )
    repo.update(created["id"], display_name="New")
    updated = repo.get(created["id"])
    assert updated["display_name"] == "New"
    assert updated["connection"] == {"url": "https://old"}  # 未指定の列は変わらない


def test_update_unknown_id_raises_not_found(repo: SourceRepository) -> None:
    with pytest.raises(AppError):
        repo.update("does-not-exist", display_name="X")


def test_delete_removes_source(repo: SourceRepository) -> None:
    created = repo.create(type="web", display_name="X", connection={}, output_dir="docs/x")
    assert repo.delete(created["id"]) is True
    assert repo.get(created["id"]) is None
    assert repo.delete(created["id"]) is False
