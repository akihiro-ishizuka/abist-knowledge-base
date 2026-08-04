"""`migrate verify`: manifest と移行先を突合する§11.4条件。"""

from __future__ import annotations

from pathlib import Path

from abist_kb.migration.inventory import inspect_source
from abist_kb.migration.manifest import Manifest, load_manifest
from abist_kb.migration.plan import build_plan
from abist_kb.migration.runner import run_migration, swap_into_place
from abist_kb.migration.verify import verify_migration


def _migrate(old_repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    to_root = tmp_path / "new-repo"
    report = inspect_source(old_repo, tmp_path / "sandbox")
    plan = build_plan(report, old_repo, to_root)
    manifest_path = tmp_path / "migration-manifest.json"
    build_dir = tmp_path / "build"
    manifest = Manifest(from_root=str(old_repo), to_root=str(to_root))
    manifest = run_migration(
        plan, manifest, manifest_path, old_repo, build_dir, tmp_path / "sandbox2"
    )
    from abist_kb.migration.manifest import save_manifest

    save_manifest(manifest, manifest_path)
    swap_into_place(build_dir, to_root)
    return manifest_path, to_root


def test_verify_passes_structural_conditions_after_run(old_repo: Path, tmp_path: Path) -> None:
    manifest_path, to_root = _migrate(old_repo, tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest is not None

    result = verify_migration(manifest, old_repo, to_root, expected_sync_by_source={"git": 1})
    by_name = {c.name: c for c in result.conditions}
    assert by_name["markdown_bytes"].passed
    assert by_name["excluded_have_reason"].passed
    assert by_name["no_unfinished_steps"].passed
    assert by_name["sync_state_counts"].passed
    # search_quality requires injected baseline data that this unit test doesn't
    # provide; it must fail closed rather than silently pass.
    assert not by_name["search_quality"].passed
    assert not result.ok


def test_verify_search_quality_passes_within_tolerance(old_repo: Path, tmp_path: Path) -> None:
    manifest_path, to_root = _migrate(old_repo, tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest is not None

    result = verify_migration(
        manifest,
        old_repo,
        to_root,
        expected_sync_by_source={"git": 1},
        recall_before=0.90,
        recall_after=0.895,
        citation_agreement=0.97,
    )
    assert result.ok


def test_verify_detects_hash_mismatch(old_repo: Path, tmp_path: Path) -> None:
    manifest_path, to_root = _migrate(old_repo, tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest is not None

    (to_root / "docs" / "a.md").write_text("破壊された内容", encoding="utf-8")

    result = verify_migration(manifest, old_repo, to_root)
    by_name = {c.name: c for c in result.conditions}
    assert not by_name["markdown_bytes"].passed
    assert not result.ok


def test_verify_fails_on_unfinished_step(old_repo: Path, tmp_path: Path) -> None:
    manifest_path, to_root = _migrate(old_repo, tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest is not None
    from abist_kb.migration.manifest import StepRecord

    manifest.record_step(StepRecord(name="broken_step", status="failed", input_hash="x"))

    result = verify_migration(manifest, old_repo, to_root)
    by_name = {c.name: c for c in result.conditions}
    assert not by_name["no_unfinished_steps"].passed
