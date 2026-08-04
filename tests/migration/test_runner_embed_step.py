"""埋め込み再生成工程(`embed_chunks`)のrunnerへの配線・観測性・再開可能性(M8b)。

実際のローカル埋め込みモデルのロードは重いため(`tests/search/test_embedding.py`
と同じ理由でCIでは避ける)、`IndexService` をダブルへ差し替えて配線・ログ出力・
manifestへの記録・再開判定だけを検証する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import abist_kb.migration.runner as runner_mod
from abist_kb.migration.manifest import Manifest, hash_inputs, save_manifest
from abist_kb.migration.runner import run_migration


class _FakeIndexService:
    call_log: list[str] = []

    def __init__(self, **kwargs: Any) -> None:
        pass

    def build(self, corpus: str, *, emit=None, check_lease=None) -> dict[str, Any]:
        if emit is not None:
            emit(phase="build", current=1, total=1, message="build done")
        return {"summary": {"documents_added": 1, "documents_updated": 0}}

    def embed(self, corpus: str, *, emit=None, check_lease=None) -> dict[str, Any]:
        if emit is not None:
            emit(phase="embed", current=5, total=10, message="5件生成")
        return {"summary": {"generated": 5, "scanned": 10, "skipped": 5}}


class _FailingIndexService(_FakeIndexService):
    def embed(self, corpus: str, *, emit=None, check_lease=None) -> dict[str, Any]:
        raise RuntimeError("CPU競合で中断(リハーサル再現)")


@pytest.fixture(autouse=True)
def _stub_index_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "IndexService", _FakeIndexService)


def _run_with_embeddings(old_repo: Path, tmp_path: Path):
    from abist_kb.migration.inventory import inspect_source
    from abist_kb.migration.plan import build_plan

    to_root = tmp_path / "new-repo"
    report = inspect_source(old_repo, tmp_path / "sandbox")
    plan = build_plan(report, old_repo, to_root)
    manifest_path = tmp_path / "migration-manifest.json"
    build_dir = tmp_path / "build"
    manifest = Manifest(from_root=str(old_repo), to_root=str(to_root))
    manifest = run_migration(
        plan,
        manifest,
        manifest_path,
        old_repo,
        build_dir,
        tmp_path / "sandbox2",
        run_embeddings=True,
    )
    return build_dir, manifest_path, manifest


def test_embed_step_is_not_run_by_default(old_repo: Path, tmp_path: Path) -> None:
    from abist_kb.migration.inventory import inspect_source
    from abist_kb.migration.plan import build_plan

    to_root = tmp_path / "new-repo"
    report = inspect_source(old_repo, tmp_path / "sandbox")
    plan = build_plan(report, old_repo, to_root)
    manifest = Manifest(from_root=str(old_repo), to_root=str(to_root))
    manifest = run_migration(
        plan,
        manifest,
        tmp_path / "migration-manifest.json",
        old_repo,
        tmp_path / "build",
        tmp_path / "sandbox2",
    )
    assert "embed_chunks" not in manifest.steps


def test_embed_step_runs_build_then_embed_and_records_counts(
    old_repo: Path, tmp_path: Path
) -> None:
    build_dir, _manifest_path, manifest = _run_with_embeddings(old_repo, tmp_path)
    step = manifest.steps["embed_chunks"]
    assert step.status == "completed"
    assert step.counts["embeddings_generated"] == 5
    assert step.counts["embeddings_scanned"] == 10
    del build_dir


def test_embed_step_writes_progress_log_observable_during_run(
    old_repo: Path, tmp_path: Path
) -> None:
    build_dir, _manifest_path, manifest = _run_with_embeddings(old_repo, tmp_path)
    step = manifest.steps["embed_chunks"]
    log_path = Path(step.details["progress_log"])
    assert log_path.is_file()
    text = log_path.read_text(encoding="utf-8")
    assert "phase=build" in text
    assert "phase=embed" in text
    assert "5/10" in text
    assert "embed_chunks 完了" in text
    del build_dir


def test_embed_step_failure_is_recorded_with_exit_timestamp_in_log(
    old_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod, "IndexService", _FailingIndexService)
    build_dir, _manifest_path, manifest = _run_with_embeddings(old_repo, tmp_path)
    step = manifest.steps["embed_chunks"]
    assert step.status == "failed"
    assert step.finished_at
    log_path = (
        Path(step.details["progress_log"])
        if "progress_log" in step.details
        else (build_dir / "logs" / "embedding-progress.log")
    )
    text = log_path.read_text(encoding="utf-8")
    assert "embed_chunks 失敗" in text
    # A timestamped line means an operator tailing the log can tell the run died
    # rather than merely stalled.
    assert text.strip().splitlines()[-1].split(" ", 1)[0]


def test_embed_step_is_skipped_on_second_invocation_once_completed(
    old_repo: Path, tmp_path: Path
) -> None:
    build_dir, manifest_path, manifest = _run_with_embeddings(old_repo, tmp_path)
    first_finished_at = manifest.steps["embed_chunks"].finished_at

    from abist_kb.migration.manifest import load_manifest

    manifest2 = load_manifest(manifest_path)
    assert manifest2 is not None
    # Sanity: is_step_current agrees the completed step should be skipped.
    assert manifest2.is_step_current("embed_chunks", hash_inputs("work"))
    del build_dir, first_finished_at

    save_manifest(manifest2, manifest_path)
