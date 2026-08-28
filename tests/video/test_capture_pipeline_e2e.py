"""キャプチャ有効時のパイプライン統合（Git commit 追跡とマスクの転記）。

`KB_RUN_CAPTURE_TESTS=1` のときだけ走る（実ブラウザ・実 git を使う）。

`test_capture_e2e.py` がキャプチャ層そのものを見るのに対し、ここは
**パイプラインへの取り込み**を見る:

- 撮影 PNG がシーンディレクトリへ複製され、`image.path` が相対パスに書き換わる
- `resolved_commit_sha` が `citations.json` と `video-metadata.json` へ転記される
- マスク件数が `captures/manifest.json` に残る
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from abist_kb.application.video import capture_planner
from abist_kb.application.video.pipeline import _attach_captures
from abist_kb.application.video.project_store import load_project
from abist_kb.application.video.video_metadata import build_metadata

pytestmark = pytest.mark.skipif(
    os.environ.get("KB_RUN_CAPTURE_TESTS") != "1",
    reason="実ブラウザ・実 git を伴うため既定はスキップ。KB_RUN_CAPTURE_TESTS=1 で実行する",
)

ENABLED = {"ABIST_KB_VIDEO_CAPTURE_ENABLED": "1"}

_PAGE = (
    '<!doctype html><html lang="ja"><head><meta charset="utf-8"></head>'
    '<body style="background:#fff"><h1 id="main">社内アプリ</h1>'
    '<p id="user-name">担当: 山田 太郎</p></body></html>'
)


def _git(args: list[str], cwd: Path) -> str:
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
    ).stdout.strip()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_capture_flows_into_the_project(tmp_path: Path) -> None:
    repo = tmp_path / "origin"
    repo.mkdir()
    (repo / "index.html").write_text(_PAGE, encoding="utf-8")
    _git(["init", "-b", "main"], repo)
    _git(["add", "."], repo)
    _git(["commit", "-m", "init", "--no-gpg-sign"], repo)
    head = _git(["rev-parse", "HEAD"], repo)

    port = _free_port()
    registry = tmp_path / "capture-profiles.json"
    registry.write_text(
        json.dumps(
            {
                "profiles": {
                    "app": {
                        "repo": {"url": str(repo), "ref": "main"},
                        "launch": {
                            "type": "web",
                            "command": [sys.executable, "-m", "http.server", str(port)],
                            "url": f"http://127.0.0.1:{port}/index.html",
                            "ready_timeout_sec": 30,
                        },
                        "shots": [{"id": "home", "scene_id": "s02", "mask": ["#user-name"]}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # image beat を持つ最小プロジェクトを組む
    project = tmp_path / "project"
    project.mkdir()
    spec = {
        "schema_version": "1.0",
        "video_id": "20260101T000000Z-video-cap0",
        "title": "キャプチャつき動画",
        "format": {"aspect_ratio": "16:9", "width": 1920, "height": 1080, "fps": 30},
        "distribution": {"classification": "internal", "public_candidate": False},
        "sources": [{"id": "src1", "path": "a/b.md"}],
        "scenes": [
            {
                "id": "s02",
                "kind": "image",
                "title": "アプリ画面",
                "scene_spec": {
                    "scene_kind": "image",
                    "template": "image_still",
                    "beats": [{"type": "image", "path": "placeholder.png", "decorative": True}],
                },
            }
        ],
    }
    (project / "project-spec.json").write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8"
    )

    plan = capture_planner.resolve_profile(
        "app", repo_root=tmp_path, env=ENABLED, profiles_file=registry
    )
    assert plan.ok, plan.message
    outcome = capture_planner.run_capture(plan, project)
    assert outcome.ok, outcome.warnings
    assert outcome.resolved_commit_sha == head

    _attach_captures(project, outcome.images_by_scene)

    updated = load_project(project)
    beat = updated["scenes"][0]["scene_spec"]["beats"][0]
    assert beat["path"] == "home.png", "image.path がキャプチャへ差し替わっていない"
    assert "/" not in beat["path"] and ".." not in beat["path"]
    assert (project / "scenes" / "s02" / "home.png").is_file()

    manifest = json.loads((project / "captures" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["resolved_commit_sha"] == head
    assert manifest["shots"][0]["masked_regions"] == 1
    assert len(manifest["shots"][0]["sha256"]) == 64

    metadata = build_metadata(
        updated, offsets={}, duration_sec=10.0, capture_commit_sha=outcome.resolved_commit_sha
    )
    assert head in metadata.metadata["description"], "説明文に commit SHA が転記されていない"
    assert metadata.metadata["capture"]["resolved_commit_sha"] == head
