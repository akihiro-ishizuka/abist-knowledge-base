"""`migrate run`: 縮小 fixture からの構築・再実行の冪等性・swap。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from abist_kb.migration.inventory import inspect_source
from abist_kb.migration.manifest import Manifest, load_manifest
from abist_kb.migration.plan import build_plan
from abist_kb.migration.runner import run_migration, swap_into_place


def _run_once(old_repo: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    to_root = tmp_path / "new-repo"
    report = inspect_source(old_repo, tmp_path / "sandbox")
    plan = build_plan(report, old_repo, to_root)
    manifest_path = tmp_path / "migration-manifest.json"
    build_dir = tmp_path / "build"
    manifest = load_manifest(manifest_path) or Manifest(
        from_root=str(old_repo), to_root=str(to_root)
    )
    manifest = run_migration(
        plan, manifest, manifest_path, old_repo, build_dir, tmp_path / "sandbox2"
    )
    return build_dir, manifest_path, to_root


def test_run_copies_bytes_exactly_and_records_manifest(old_repo: Path, tmp_path: Path) -> None:
    build_dir, manifest_path, _to_root = _run_once(old_repo, tmp_path)

    original = (old_repo / "docs" / "a.md").read_bytes()
    copied = (build_dir / "docs" / "a.md").read_bytes()
    assert original == copied

    manifest = load_manifest(manifest_path)
    assert manifest is not None
    assert manifest.steps["copy_docs"].status == "completed"
    assert "docs/mojibake.md" in [e["path"] for e in manifest.steps["copy_docs"].excluded]
    for excluded in manifest.steps["copy_docs"].excluded:
        assert excluded["reason"]


def test_run_imports_batch_config(old_repo: Path, tmp_path: Path) -> None:
    build_dir, manifest_path, _to_root = _run_once(old_repo, tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest is not None
    assert manifest.steps["import_batch_config"].status == "completed"
    assert manifest.steps["import_batch_config"].counts["imported"] == 2

    conn = sqlite3.connect(build_dir / "app.sqlite")
    try:
        count = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    finally:
        conn.close()
    assert count == 2


def test_run_imports_sync_state_rows(old_repo: Path, tmp_path: Path) -> None:
    build_dir, manifest_path, _to_root = _run_once(old_repo, tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest is not None
    assert manifest.steps["import_sync_state"].counts["imported"] == 1

    conn = sqlite3.connect(build_dir / "app.sqlite")
    try:
        row = conn.execute("SELECT path, source FROM documents").fetchone()
    finally:
        conn.close()
    assert row == ("docs/a.md", "git")


def test_run_resolves_source_id_by_creating_matching_source(old_repo: Path, tmp_path: Path) -> None:
    """`source_id` は `sources` テーブルとの突合(無ければ最小限の新規作成)で解決され、
    NULL のまま放置されない(旧行の source='git' に一致する sources.type='git' が
    作られ、その id が documents.source_id へ入る)。
    """
    build_dir, manifest_path, _to_root = _run_once(old_repo, tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest is not None
    step = manifest.steps["import_sync_state"]
    assert step.counts["source_id_resolved"] == 1
    assert step.counts["source_id_unresolved"] == 0

    conn = sqlite3.connect(build_dir / "app.sqlite")
    try:
        doc_source_id = conn.execute(
            "SELECT source_id FROM documents WHERE path = 'docs/a.md'"
        ).fetchone()[0]
        assert doc_source_id is not None
        source_type = conn.execute(
            "SELECT type FROM sources WHERE id = ?", (doc_source_id,)
        ).fetchone()[0]
        assert source_type == "git"
    finally:
        conn.close()


def test_run_records_unresolvable_source_id_with_reason(old_repo: Path, tmp_path: Path) -> None:
    """旧行の source が空の場合、source_id は NULL のままだが manifest に理由付きで
    記録される(黙って NULL にしない)。
    """
    import sqlite3 as _sqlite3

    sync_db = old_repo / "data" / "sync-state.sqlite"
    conn = _sqlite3.connect(sync_db)
    conn.execute(
        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "docs/no-source.md",
            "22222222-2222-2222-2222-222222222222",
            None,
            "work",
            "active",
            "NoSource",
            "synced",
            "2026-07-29T07:09:56Z",
            "2026-07-29T07:09:56Z",
        ),
    )
    (old_repo / "docs" / "no-source.md").write_text("本文", encoding="utf-8")
    conn.commit()
    conn.close()

    build_dir, manifest_path, _to_root = _run_once(old_repo, tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest is not None
    step = manifest.steps["import_sync_state"]
    assert step.counts["source_id_unresolved"] == 1
    unresolved_paths = [e["path"] for e in step.excluded]
    assert "docs/no-source.md" in unresolved_paths
    for entry in step.excluded:
        assert entry["reason"]

    conn = sqlite3.connect(build_dir / "app.sqlite")
    try:
        source_id = conn.execute(
            "SELECT source_id FROM documents WHERE path = 'docs/no-source.md'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert source_id is None


def test_run_is_idempotent_on_second_invocation(old_repo: Path, tmp_path: Path) -> None:
    build_dir, manifest_path, to_root = _run_once(old_repo, tmp_path)
    manifest_after_first = load_manifest(manifest_path)
    assert manifest_after_first is not None

    # Re-run against the same build dir/manifest: steps should be skipped (same
    # input hash), not duplicated.
    report = inspect_source(old_repo, tmp_path / "sandbox")
    plan = build_plan(report, old_repo, to_root)
    manifest_second = run_migration(
        plan,
        load_manifest(manifest_path) or Manifest(from_root=str(old_repo), to_root=str(to_root)),
        manifest_path,
        old_repo,
        build_dir,
        tmp_path / "sandbox2",
    )

    conn = sqlite3.connect(build_dir / "app.sqlite")
    try:
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        batch_count = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    finally:
        conn.close()
    assert doc_count == 1  # not duplicated
    assert batch_count == 2  # not duplicated
    assert manifest_second.steps["copy_docs"].finished_at == (
        manifest_after_first.steps["copy_docs"].finished_at
    )


def test_run_never_modifies_source(old_repo: Path, tmp_path: Path) -> None:
    sync_db = old_repo / "data" / "sync-state.sqlite"
    before = (sync_db.stat().st_size, sync_db.stat().st_mtime_ns)
    _run_once(old_repo, tmp_path)
    after = (sync_db.stat().st_size, sync_db.stat().st_mtime_ns)
    assert before == after


def test_swap_into_place_moves_build_dir(old_repo: Path, tmp_path: Path) -> None:
    build_dir, _manifest_path, to_root = _run_once(old_repo, tmp_path)
    swap_into_place(build_dir, to_root)
    assert not build_dir.exists()
    assert (to_root / "docs" / "a.md").exists()
