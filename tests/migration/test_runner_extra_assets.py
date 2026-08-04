"""§11.2 の残り3区分(可視化アーティファクト・git-cache・.env診断)の移行(M8b)。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from abist_kb.migration.inventory import inspect_source
from abist_kb.migration.manifest import Manifest
from abist_kb.migration.plan import build_plan
from abist_kb.migration.runner import run_migration


def _run(old_repo: Path, tmp_path: Path) -> Path:
    to_root = tmp_path / "new-repo"
    report = inspect_source(old_repo, tmp_path / "sandbox")
    plan = build_plan(report, old_repo, to_root)
    manifest_path = tmp_path / "migration-manifest.json"
    build_dir = tmp_path / "build"
    manifest = Manifest(from_root=str(old_repo), to_root=str(to_root))
    run_migration(plan, manifest, manifest_path, old_repo, build_dir, tmp_path / "sandbox2")
    return build_dir


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_visualization(root: Path, *, name: str, output_bytes: bytes, sha_mismatch: bool) -> None:
    viz_dir = root / "reports" / "visualizations" / name
    viz_dir.mkdir(parents=True)
    (viz_dir / "output.png").write_bytes(output_bytes)
    manifest = {
        "visualization_id": name,
        "outputs": [
            {
                "path": "output.png",
                "sha256": "0" * 64 if sha_mismatch else _sha256(output_bytes),
                "size_bytes": len(output_bytes),
            }
        ],
    }
    (viz_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_visualizations_with_verified_sha256_are_copied(old_repo: Path, tmp_path: Path) -> None:
    _write_visualization(old_repo, name="scene-ok", output_bytes=b"png-bytes", sha_mismatch=False)
    build_dir = _run(old_repo, tmp_path)
    dest = build_dir / "reports" / "visualizations" / "scene-ok" / "output.png"
    assert dest.read_bytes() == b"png-bytes"


def test_visualizations_with_sha256_mismatch_are_not_copied_and_are_findings(
    old_repo: Path, tmp_path: Path
) -> None:
    _write_visualization(old_repo, name="scene-bad", output_bytes=b"bytes", sha_mismatch=True)
    build_dir = _run(old_repo, tmp_path)
    assert not (build_dir / "reports" / "visualizations" / "scene-bad").exists()

    from abist_kb.migration.manifest import load_manifest

    manifest = load_manifest(tmp_path / "migration-manifest.json")
    assert manifest is not None
    step = manifest.steps["copy_visualizations"]
    excluded_names = [e["path"] for e in step.excluded]
    assert "scene-bad" in excluded_names
    for entry in step.excluded:
        assert entry["reason"]


def _init_git_repo(path: Path, *, with_remote: bool) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=path, check=True)
    (path / "README.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "README.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    if with_remote:
        subprocess.run(
            ["git", "remote", "add", "origin", "https://example.invalid/repo.git"],
            cwd=path,
            check=True,
        )


def test_git_cache_with_verifiable_remote_and_head_is_copied(
    old_repo: Path, tmp_path: Path
) -> None:
    _init_git_repo(old_repo / "data" / "git-cache" / "repo-ok", with_remote=True)
    build_dir = _run(old_repo, tmp_path)
    assert (build_dir / "data" / "git-cache" / "repo-ok" / "README.txt").exists()

    from abist_kb.migration.manifest import load_manifest

    manifest = load_manifest(tmp_path / "migration-manifest.json")
    assert manifest is not None
    step = manifest.steps["copy_git_cache"]
    assert step.counts["copied"] == 1
    # remote URL は記録されるがサニタイズされ、資格情報等は含まない前提の
    # プレーンな URL なのでホスト名までは残る。
    assert "example.invalid" in step.details["verified_remotes"]["repo-ok"]


def test_git_cache_without_remote_is_discarded_with_reason(old_repo: Path, tmp_path: Path) -> None:
    _init_git_repo(old_repo / "data" / "git-cache" / "repo-no-remote", with_remote=False)
    build_dir = _run(old_repo, tmp_path)
    assert not (build_dir / "data" / "git-cache" / "repo-no-remote").exists()

    from abist_kb.migration.manifest import load_manifest

    manifest = load_manifest(tmp_path / "migration-manifest.json")
    assert manifest is not None
    step = manifest.steps["copy_git_cache"]
    excluded_names = [e["path"] for e in step.excluded]
    assert "repo-no-remote" in excluded_names
    for entry in step.excluded:
        assert entry["reason"]


def test_git_cache_remote_credentials_are_never_recorded(old_repo: Path, tmp_path: Path) -> None:
    path = old_repo / "data" / "git-cache" / "repo-secret"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=path, check=True)
    (path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://ghp_secrettoken@github.com/org/repo"],
        cwd=path,
        check=True,
    )

    _run(old_repo, tmp_path)

    from abist_kb.migration.manifest import load_manifest

    manifest = load_manifest(tmp_path / "migration-manifest.json")
    assert manifest is not None
    manifest_text = json.dumps(manifest.to_dict())
    assert "ghp_secrettoken" not in manifest_text


def test_env_keys_are_diagnosed_but_values_are_never_stored(old_repo: Path, tmp_path: Path) -> None:
    (old_repo / ".env").write_text(
        "ESA_ACCESS_TOKEN=super-secret-value\nOPENAI_API_KEY=sk-another-secret\n# comment\n",
        encoding="utf-8",
    )
    _run(old_repo, tmp_path)

    from abist_kb.migration.manifest import load_manifest

    manifest = load_manifest(tmp_path / "migration-manifest.json")
    assert manifest is not None
    step = manifest.steps["diagnose_env"]
    assert sorted(step.details["keys"]) == ["ESA_ACCESS_TOKEN", "OPENAI_API_KEY"]
    manifest_text = json.dumps(manifest.to_dict())
    assert "super-secret-value" not in manifest_text
    assert "sk-another-secret" not in manifest_text


def test_env_absent_is_recorded_as_zero_keys(old_repo: Path, tmp_path: Path) -> None:
    build_dir = _run(old_repo, tmp_path)
    del build_dir

    from abist_kb.migration.manifest import load_manifest

    manifest = load_manifest(tmp_path / "migration-manifest.json")
    assert manifest is not None
    assert manifest.steps["diagnose_env"].counts["keys_found"] == 0
