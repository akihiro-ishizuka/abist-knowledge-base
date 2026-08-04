"""`migrate plan`: ファイル単位の action 確定と from/to 安全性チェック。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.domain.errors import AppError
from abist_kb.migration.inventory import inspect_source
from abist_kb.migration.plan import assert_safe_roots, build_plan


def test_assert_safe_roots_rejects_identical_paths(tmp_path: Path) -> None:
    with pytest.raises(AppError):
        assert_safe_roots(tmp_path, tmp_path)


def test_assert_safe_roots_rejects_parent_child(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(AppError):
        assert_safe_roots(tmp_path, child)
    with pytest.raises(AppError):
        assert_safe_roots(child, tmp_path)


def test_assert_safe_roots_accepts_independent_dirs(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert_safe_roots(a, b)  # should not raise


def test_build_plan_excludes_damaged_files_with_reason(old_repo: Path, tmp_path: Path) -> None:
    report = inspect_source(old_repo, tmp_path / "sandbox")
    plan = build_plan(report, old_repo, tmp_path / "new-repo")

    by_path = {item.relative_path: item for item in plan.items}
    assert by_path["docs/mojibake.md"].action == "exclude"
    assert by_path["docs/mojibake.md"].reason
    assert by_path["docs/broken.md"].action == "exclude"
    assert by_path["docs/broken.md"].reason
    assert by_path["docs/a.md"].action == "copy"
    assert by_path["docs/knowledge/catiadoc/b.md"].action == "copy"
    assert by_path["C#ATIA/outside.md"].action == "copy"


def test_build_plan_marks_embeddings_for_regeneration(old_repo: Path, tmp_path: Path) -> None:
    report = inspect_source(old_repo, tmp_path / "sandbox")
    plan = build_plan(report, old_repo, tmp_path / "new-repo")
    by_path = {item.relative_path: item for item in plan.items}
    assert by_path["embeddings"].action == "regenerate"


def test_build_plan_every_excluded_item_has_reason(old_repo: Path, tmp_path: Path) -> None:
    report = inspect_source(old_repo, tmp_path / "sandbox")
    plan = build_plan(report, old_repo, tmp_path / "new-repo")
    for item in plan.excluded():
        assert item.reason
