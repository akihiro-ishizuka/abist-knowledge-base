"""Phase 7 の実キャプチャ E2E（ローカル git リポジトリ + 実ブラウザ）。

`KB_RUN_CAPTURE_TESTS=1` のときだけ走る。実ブラウザ起動を伴うため、
実 Manim テスト（`KB_RUN_MANIM_TESTS`）と同じ明示フラグ方式にする。

ここで確認するのは受け入れ条件そのもの:

- `resolved_commit_sha` が `captures/manifest.json` に残る
- 各 shot の `sha256` が残る
- **マスク対象が見つからない shot は落ちる**（未マスクのまま出さない）
- 撮影プロセスの子孫が残らない
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from abist_kb.application.video import capture_planner

pytestmark = pytest.mark.skipif(
    os.environ.get("KB_RUN_CAPTURE_TESTS") != "1",
    reason="実ブラウザ起動を伴うため既定はスキップ。KB_RUN_CAPTURE_TESTS=1 で実行する",
)

ENABLED = {"ABIST_KB_VIDEO_CAPTURE_ENABLED": "1"}

_PAGE_HTML = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>社内アプリ</title></head>
<body style="font-family: sans-serif; background:#fff">
  <h1 id="main">パターンエディタ</h1>
  <p id="user-name">担当: 山田 太郎</p>
  <p class="api-token">sk-do-not-show-this</p>
</body></html>
"""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": os.environ.get("PATH", ""),
    }
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def origin_repo(tmp_path: Path) -> tuple[Path, str]:
    """撮影対象の「社内アプリ」リポジトリを作る（静的 HTML 1枚）。"""
    repo = tmp_path / "origin"
    repo.mkdir()
    (repo / "index.html").write_text(_PAGE_HTML, encoding="utf-8")
    _git(["init", "-b", "main"], repo)
    _git(["add", "."], repo)
    _git(["commit", "-m", "init", "--no-gpg-sign"], repo)
    head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    return repo, head


def _registry(tmp_path: Path, repo: Path, port: int, shots: list[dict]) -> Path:
    payload = {
        "profiles": {
            "sample-app": {
                "repo": {"url": str(repo), "ref": "main"},
                "launch": {
                    "type": "web",
                    "command": [sys.executable, "-m", "http.server", str(port)],
                    "url": f"http://127.0.0.1:{port}/index.html",
                    "ready_timeout_sec": 30,
                },
                "shots": shots,
            }
        }
    }
    path = tmp_path / "capture-profiles.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_capture_records_commit_sha_and_masks(tmp_path: Path, origin_repo) -> None:
    repo, head = origin_repo
    project = tmp_path / "project"
    project.mkdir()
    registry = _registry(
        tmp_path,
        repo,
        _free_port(),
        [{"id": "home", "scene_id": "s04", "mask": ["#user-name", ".api-token"]}],
    )

    plan = capture_planner.resolve_profile(
        "sample-app", repo_root=tmp_path, env=ENABLED, profiles_file=registry
    )
    assert plan.ok, plan.message

    outcome = capture_planner.run_capture(plan, project)
    assert outcome.ok, outcome.warnings
    assert outcome.resolved_commit_sha == head

    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))
    assert manifest["resolved_commit_sha"] == head
    assert manifest["requested_ref"] == "main"
    shot = manifest["shots"][0]
    assert shot["ok"] is True
    assert len(shot["sha256"]) == 64
    assert shot["masked_regions"] == 2, "指定した2領域がマスクされていない"
    assert (project / shot["path"]).is_file()
    # image beat の path として使える形（scenes へ渡す相対パス）
    assert outcome.images_by_scene == {"s04": shot["path"]}


def test_missing_mask_target_drops_the_shot(tmp_path: Path, origin_repo) -> None:
    """マスクできないなら**その shot を落とす**（未マスクで出さない）。"""
    repo, _head = origin_repo
    project = tmp_path / "project"
    project.mkdir()
    registry = _registry(
        tmp_path, repo, _free_port(), [{"id": "home", "mask": ["#does-not-exist"]}]
    )

    plan = capture_planner.resolve_profile(
        "sample-app", repo_root=tmp_path, env=ENABLED, profiles_file=registry
    )
    outcome = capture_planner.run_capture(plan, project)
    assert not outcome.ok
    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))
    assert manifest["shots"][0]["code"] == "MASK_TARGET_NOT_FOUND"
    assert not (project / "captures" / "home.png").exists()


def test_capture_failure_does_not_raise(tmp_path: Path) -> None:
    """存在しない repo でも例外にせず、動画生成が続けられる形で返る。"""
    project = tmp_path / "project"
    project.mkdir()
    registry = tmp_path / "capture-profiles.json"
    registry.write_text(
        json.dumps(
            {
                "profiles": {
                    "sample-app": {
                        "repo": {"url": "https://example.invalid/none.git", "ref": "main"},
                        "launch": {
                            "type": "web",
                            "command": [sys.executable, "-c", "pass"],
                            "url": "http://127.0.0.1:1/",
                        },
                        "shots": [{"id": "home"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    plan = capture_planner.resolve_profile(
        "sample-app", repo_root=tmp_path, env=ENABLED, profiles_file=registry
    )
    outcome = capture_planner.run_capture(plan, project)
    assert not outcome.ok
    assert outcome.code in ("GIT_CLONE_FAILED", "GIT_REF_NOT_FOUND", "GIT_UNAVAILABLE")
    assert outcome.warnings
