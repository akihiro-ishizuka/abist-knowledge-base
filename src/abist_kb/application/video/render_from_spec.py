"""作成済みプロジェクトの描画（`render_video` ジョブの実体）。

`create_video_project` で作った `project-spec.json` を読み、`pipeline.finish_project`
（`run_pipeline` の後半と同じ関数）へ渡すだけの薄い層。**後半を共有する**ことで、
MCP/CLI/API のどの経路で作っても QA・メタデータ・配布判定が同じように揃う。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from abist_kb.application.video.pipeline import PipelineResult, ProgressFn, finish_project
from abist_kb.application.video.project_store import load_project


def used_paths_from_spec(spec: dict[str, Any]) -> set[str]:
    """spec の scene_spec に載っている出典パス（QA の入力使用検査に使う）。"""
    used: set[str] = set()
    for scene in spec.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for source in (scene.get("scene_spec") or {}).get("sources") or []:
            path = source.get("path")
            if isinstance(path, str):
                used.add(path)
    return used


def render_existing_project(
    project_dir: Path,
    *,
    docs_dir: Path,
    repo_root: Path,
    reports_dir: Path | None = None,
    tts: str = "none",
    manual_audio_dir: Path | None = None,
    sound_intensity: str = "subtle",
    sound_enabled: bool = True,
    capture_profile: str | None = None,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PipelineResult:
    """`project-spec.json` を読んで描画〜成果物一式まで進める。"""
    spec = load_project(project_dir)
    if spec is None:
        return PipelineResult(
            ok=False,
            code="VIDEO_NOT_FOUND",
            errors=[
                {
                    "path": "project_dir",
                    "code": "not_found",
                    "message": f"project-spec.json がありません: {project_dir}",
                }
            ],
        )
    scenes = [s for s in (spec.get("scenes") or []) if isinstance(s, dict)]
    return finish_project(
        project_dir,
        video_id=spec.get("video_id"),
        scenes=scenes,
        sound_events=[s for s in (spec.get("sound_events") or []) if isinstance(s, dict)],
        used_paths=used_paths_from_spec(spec),
        docs_dir=docs_dir,
        repo_root=repo_root,
        tts=tts,
        manual_audio_dir=manual_audio_dir,
        sound_intensity=sound_intensity,
        sound_enabled=sound_enabled,
        capture_profile=capture_profile,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )


__all__ = ["render_existing_project", "used_paths_from_spec"]
