"""`BatchRepository`: `batches`/`batch_items` テーブルの CRUD。

バッチは app.sqlite が正であり、旧 `batch-config.js` へは書き戻さない
(一方向 import は `migration.batch_config_parser` の役目、CRUD 自体はここ)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.infrastructure.db.sources_repo import SourceRepository


@pytest.fixture
def repo(tmp_root: Path) -> BatchRepository:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    return BatchRepository(conn)


@pytest.fixture
def sources(tmp_root: Path, repo: BatchRepository) -> SourceRepository:
    # 同じ接続を共有する(BatchRepository はテスト用に conn を公開していないため、
    # repo フィクスチャと同じ生成過程を再利用する)。
    conn = connect(tmp_root / "app.sqlite")
    return SourceRepository(conn)


def test_create_batch_with_esa_items(repo: BatchRepository) -> None:
    batch = repo.create(
        name="蛇腹形状の自動設計",
        type="esa",
        output_dir="docs/蛇腹形状の自動設計",
        items=[
            {"target": "設計効率化/三桜工業様/蛇腹形状の自動設計"},
            {"target": "議事録/設計効率化/三桜工業様定例"},
        ],
    )
    assert batch["name"] == "蛇腹形状の自動設計"
    fetched = repo.get(batch["id"])
    assert fetched is not None
    assert [item["target"] for item in fetched["items"]] == [
        "設計効率化/三桜工業様/蛇腹形状の自動設計",
        "議事録/設計効率化/三桜工業様定例",
    ]
    assert [item["position"] for item in fetched["items"]] == [0, 1]


def test_create_batch_name_must_be_unique(repo: BatchRepository) -> None:
    repo.create(name="dup", type="esa", output_dir="docs/dup", items=[])
    with pytest.raises(AppError):
        repo.create(name="dup", type="esa", output_dir="docs/dup2", items=[])


def test_list_returns_batches_ordered_by_name(repo: BatchRepository) -> None:
    repo.create(name="Zeta", type="esa", output_dir="docs/z", items=[])
    repo.create(name="Alpha", type="esa", output_dir="docs/a", items=[])
    names = [b["name"] for b in repo.list()]
    assert names == ["Alpha", "Zeta"]


def test_get_by_name(repo: BatchRepository) -> None:
    repo.create(name="catiadoc", type="web", output_dir="docs/catiadoc", items=[])
    found = repo.get_by_name("catiadoc")
    assert found is not None
    assert found["type"] == "web"


def test_update_replaces_items(repo: BatchRepository) -> None:
    batch = repo.create(name="b", type="esa", output_dir="docs/b", items=[{"target": "cat1"}])
    repo.update(batch["id"], items=[{"target": "cat1"}, {"target": "cat2"}])
    fetched = repo.get(batch["id"])
    assert [item["target"] for item in fetched["items"]] == ["cat1", "cat2"]


def test_update_output_dir_without_touching_items(repo: BatchRepository) -> None:
    batch = repo.create(name="b", type="esa", output_dir="docs/old", items=[{"target": "cat1"}])
    repo.update(batch["id"], output_dir="docs/new")
    fetched = repo.get(batch["id"])
    assert fetched["output_dir"] == "docs/new"
    assert [item["target"] for item in fetched["items"]] == ["cat1"]


def test_delete_removes_batch_and_items(repo: BatchRepository) -> None:
    batch = repo.create(name="b", type="esa", output_dir="docs/b", items=[{"target": "cat1"}])
    assert repo.delete(batch["id"]) is True
    assert repo.get(batch["id"]) is None


def test_delete_unknown_id_returns_false(repo: BatchRepository) -> None:
    assert repo.delete("does-not-exist") is False


def test_batch_item_can_reference_source(repo: BatchRepository, sources: SourceRepository) -> None:
    source = sources.create(
        type="git", display_name="repo", connection={"repository": "x"}, output_dir="docs/repo"
    )
    batch = repo.create(
        name="git-batch",
        type="git",
        output_dir="docs/repo",
        items=[{"source_id": source["id"], "options": {"branch": "main"}}],
    )
    fetched = repo.get(batch["id"])
    assert fetched["items"][0]["source_id"] == source["id"]
    assert fetched["items"][0]["options"] == {"branch": "main"}
