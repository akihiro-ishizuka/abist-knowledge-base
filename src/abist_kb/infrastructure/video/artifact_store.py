"""動画成果物のディレクトリ命名・封じ込め。

purring の `infrastructure/visualization/artifact_store.py` と**同じ命名規則**
（`<UTC-ts>-<slug>-<4hex>`、`:` を含まない、Windows 予約名を避ける）を使う。
純関数はそちらを再利用し、ここでは動画ツリー固有の導出だけを持つ。

成果物ツリー（`design/video-pipeline.md`）:

```
reports/videos/<id>/
  project-spec.json      # VideoProjectSpec（正本）
  inputs-manifest.json   # 入力解決と選抜の記録
  state.json             # 進行状態（再開用）
  scenes/<scene_id>/     # シーンごとの中間成果物
  output.mp4
  manifest.json
  citations.json
```
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from abist_kb.infrastructure.visualization.artifact_store import (
    OutputPathViolation,
    assert_inside_path,
    make_visualization_id,
    normalize_slug,
    sha256_file,
    sha256_text,
)

#: 成果物ファイル名（動画ツリーで固定）。
PROJECT_SPEC_FILE = "project-spec.json"
INPUTS_MANIFEST_FILE = "inputs-manifest.json"
STATE_FILE = "state.json"
MANIFEST_FILE = "manifest.json"
CITATIONS_FILE = "citations.json"
SCENES_DIR = "scenes"


def videos_dir(reports_dir: Path) -> Path:
    """`reports_dir`（常にベースの `reports/`）から動画成果物ディレクトリを導出する。

    purring の `visualizations_dir()` と対称。語義は「`reports_dir` は常にベース」。
    """
    return reports_dir / "videos"


def make_video_id(slug: str, *, now: datetime | None = None, random: Any = None) -> str:
    """`<UTC-ts>-<slug>-<4hex>`。purring と同じ形式・同じ制約。"""
    return make_visualization_id(slug, now=now, random=random)


@dataclass(frozen=True, slots=True)
class CreatedVideoDir:
    video_id: str
    dir: Path


def create_video_dir(
    videos_root: Path,
    title: str,
    *,
    slug: str | None = None,
    now: datetime | None = None,
    random: Any = None,
    attempts: int = 5,
) -> CreatedVideoDir:
    """一意な動画ディレクトリを作る（id 衝突は 4hex を引き直して再試行）。"""
    base_slug = normalize_slug(slug or title, fallback="video")
    videos_root.mkdir(parents=True, exist_ok=True)
    last_error: OSError | None = None
    for _ in range(attempts):
        video_id = make_video_id(base_slug, now=now, random=random)
        target = videos_root / video_id
        try:
            target.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            last_error = exc
            continue
        (target / SCENES_DIR).mkdir(exist_ok=True)
        return CreatedVideoDir(video_id=video_id, dir=target)
    raise RuntimeError(f"動画ディレクトリを作成できませんでした: {videos_root}") from last_error


def scene_dir(video_dir: Path, scene_id: str) -> Path:
    """シーン用ディレクトリ。`scene_id` による脱出を許さない。"""
    safe = normalize_slug(scene_id, fallback="scene")
    target = video_dir / SCENES_DIR / safe
    assert_inside_path(video_dir, target)
    return target


__all__ = [
    "CITATIONS_FILE",
    "INPUTS_MANIFEST_FILE",
    "MANIFEST_FILE",
    "PROJECT_SPEC_FILE",
    "SCENES_DIR",
    "STATE_FILE",
    "CreatedVideoDir",
    "OutputPathViolation",
    "assert_inside_path",
    "create_video_dir",
    "make_video_id",
    "scene_dir",
    "sha256_file",
    "sha256_text",
    "videos_dir",
]
