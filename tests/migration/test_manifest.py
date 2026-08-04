"""manifest の再開判定と保存/読み込みラウンドトリップ。"""

from __future__ import annotations

from pathlib import Path

from abist_kb.migration.manifest import (
    Manifest,
    StepRecord,
    hash_inputs,
    load_manifest,
    save_manifest,
)


def test_is_step_current_only_when_completed_and_hash_matches() -> None:
    manifest = Manifest(from_root="a", to_root="b")
    assert manifest.is_step_current("copy_docs", "h1") is False

    manifest.record_step(StepRecord(name="copy_docs", status="completed", input_hash="h1"))
    assert manifest.is_step_current("copy_docs", "h1") is True
    assert manifest.is_step_current("copy_docs", "h2") is False

    manifest.record_step(StepRecord(name="copy_docs", status="failed", input_hash="h1"))
    assert manifest.is_step_current("copy_docs", "h1") is False


def test_unexplained_gap_lists_non_completed_steps() -> None:
    manifest = Manifest(from_root="a", to_root="b")
    manifest.record_step(StepRecord(name="ok", status="completed", input_hash="h"))
    manifest.record_step(StepRecord(name="broken", status="failed", input_hash="h"))
    assert manifest.unexplained_gap() == ["broken"]


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    manifest = Manifest(from_root="a", to_root="b")
    step = StepRecord(name="copy_docs", status="completed", input_hash="h1")
    step.counts = {"copied": 3}
    step.sha256 = {"docs/a.md": "deadbeef"}
    step.excluded = [{"path": "docs/x.md", "reason": "encoding"}]
    manifest.record_step(step)

    path = tmp_path / "migration-manifest.json"
    save_manifest(manifest, path)
    loaded = load_manifest(path)

    assert loaded is not None
    assert loaded.from_root == "a"
    assert loaded.steps["copy_docs"].counts == {"copied": 3}
    assert loaded.steps["copy_docs"].sha256 == {"docs/a.md": "deadbeef"}
    assert loaded.steps["copy_docs"].excluded == [{"path": "docs/x.md", "reason": "encoding"}]


def test_load_manifest_missing_returns_none(tmp_path: Path) -> None:
    assert load_manifest(tmp_path / "nope.json") is None


def test_hash_inputs_is_order_sensitive_and_stable() -> None:
    assert hash_inputs("a", "b") == hash_inputs("a", "b")
    assert hash_inputs("a", "b") != hash_inputs("b", "a")
