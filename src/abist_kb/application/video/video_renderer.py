"""動画レンダリング（シーンごとに Manim → ffmpeg で結合）。

**同期 MCP `render_scene` は使わない。** `design/notes/visualize-recovery-known-issues.md`
の #1 のとおり、同期パスが `render` リースを長時間保持すると worker のキュー消費全体が
止まる。動画は多シーンを連続描画するため影響が拡大するので、既存の
`application.visualization.renderer.render_scene`（純粋な関数。リースを取らない）を
シーン単位で直接呼び、リースはジョブ側で1本だけ取る。

無音でも完走する（TTS が無くても MVP は成立する）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.application.video.project_store import (
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_RENDERING,
    STATE_SUCCEEDED,
    load_project,
    write_state,
)
from abist_kb.application.visualization.renderer import render_scene
from abist_kb.infrastructure.video.artifact_store import (
    MANIFEST_FILE,
    scene_dir,
    sha256_file,
)
from abist_kb.infrastructure.video.ffmpeg_runner import (
    FfmpegUnavailableError,
    concat_videos,
    mux_audio,
    probe,
)

#: 進捗コールバック。`(phase, current, total, message)`。
ProgressFn = Callable[[str, int, int, str], None]


@dataclass(frozen=True, slots=True)
class SceneRender:
    scene_id: str
    ok: bool
    video: Path | None = None
    duration_sec: float | None = None
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class VideoRenderResult:
    ok: bool
    code: str | None = None
    output: Path | None = None
    manifest_path: Path | None = None
    duration_sec: float | None = None
    scenes: list[SceneRender] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


def _scene_spec_for(scene: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    """シーンから SceneSpec を取り出す（無ければ None＝描画しない）。"""
    scene_spec = scene.get("scene_spec")
    if not isinstance(scene_spec, dict):
        return None
    # 動画の解像度・品質をシーンへ伝える（SceneSpec 1.0 の任意フィールド）
    fmt = spec.get("format") or {}
    enriched = {**scene_spec}
    enriched.setdefault("output_format", "mp4")
    if fmt.get("width") == 2560:
        enriched.setdefault("quality", "high")
    else:
        enriched.setdefault("quality", "standard")
    return enriched


def render_project(
    project_dir: Path,
    *,
    docs_dir: Path,
    repo_root: Path,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: ProgressFn | None = None,
    audio_path: Path | None = None,
) -> VideoRenderResult:
    """プロジェクトを1本の MP4 にする。

    1. シーンごとに Manim を回す（`scene_spec` を持つシーンのみ）
    2. ffmpeg で結合し、解像度・fps を統一する
    3. 音声があれば合成する（無ければ無音のまま）
    """
    spec = load_project(project_dir)
    if spec is None:
        return VideoRenderResult(
            ok=False,
            code="INVALID_VIDEO_SPEC",
            errors=[{"path": "", "code": "not_found", "message": "project-spec.json がありません"}],
        )

    fmt = spec.get("format") or {}
    width = int(fmt.get("width") or 1920)
    height = int(fmt.get("height") or 1080)
    fps = int(fmt.get("fps") or 30)
    scenes = [s for s in spec.get("scenes") or [] if isinstance(s, dict)]
    warnings: list[str] = []

    renderable = [s for s in scenes if _scene_spec_for(s, spec) is not None]
    if not renderable:
        return VideoRenderResult(
            ok=False,
            code="NO_RENDERABLE_SCENE",
            errors=[
                {
                    "path": "scenes",
                    "code": "empty",
                    "message": "scene_spec を持つシーンが1つもありません",
                }
            ],
        )

    write_state(project_dir, state=STATE_RENDERING)
    total = len(renderable) + 1  # +1 は結合
    results: list[SceneRender] = []
    videos: list[Path] = []

    for index, scene in enumerate(renderable):
        if should_cancel is not None and should_cancel():
            write_state(project_dir, state=STATE_CANCELLED, code="RENDER_CANCELLED")
            return VideoRenderResult(
                ok=False, code="RENDER_CANCELLED", scenes=results, warnings=warnings
            )

        scene_id = str(scene.get("id") or f"s{index + 1:02d}")
        if on_progress:
            on_progress("scene", index, total, f"シーン {scene_id} を描画しています")

        target = scene_dir(project_dir, scene_id)
        target.mkdir(parents=True, exist_ok=True)
        outcome = render_scene(
            _scene_spec_for(scene, spec),
            docs_dir=docs_dir,
            reports_dir=target,
            repo_root=repo_root,
            should_cancel=should_cancel,
        )
        if not outcome.ok:
            if outcome.code == "RENDER_CANCELLED":
                write_state(project_dir, state=STATE_CANCELLED, code="RENDER_CANCELLED")
                return VideoRenderResult(
                    ok=False, code="RENDER_CANCELLED", scenes=results, warnings=warnings
                )
            results.append(
                SceneRender(
                    scene_id=scene_id,
                    ok=False,
                    code=outcome.code,
                    message="; ".join(e.get("message", "") for e in outcome.errors) or None,
                )
            )
            write_state(project_dir, state=STATE_FAILED, code=outcome.code)
            return VideoRenderResult(
                ok=False,
                code=outcome.code or "RENDER_FAILED",
                scenes=results,
                warnings=[*warnings, *outcome.warnings],
                errors=outcome.errors,
            )

        produced = Path(outcome.outputs[0]["path"]) if outcome.outputs else None
        if produced is None or not produced.exists():
            write_state(project_dir, state=STATE_FAILED, code="OUTPUT_NOT_FOUND")
            return VideoRenderResult(ok=False, code="OUTPUT_NOT_FOUND", scenes=results)

        info = probe(produced)
        results.append(
            SceneRender(scene_id=scene_id, ok=True, video=produced, duration_sec=info.duration_sec)
        )
        videos.append(produced)
        warnings.extend(outcome.warnings)

    if on_progress:
        on_progress("concat", len(renderable), total, "シーンを結合しています")

    output = project_dir / "output.mp4"
    try:
        concat = concat_videos(
            videos,
            project_dir / "concat.mp4",
            width=width,
            height=height,
            fps=fps,
            work_dir=project_dir / "work",
            should_cancel=should_cancel,
        )
    except FfmpegUnavailableError as exc:
        write_state(project_dir, state=STATE_FAILED, code="FFMPEG_NOT_FOUND")
        return VideoRenderResult(
            ok=False,
            code="FFMPEG_NOT_FOUND",
            scenes=results,
            errors=[{"path": "", "code": "ffmpeg", "message": str(exc)}],
        )

    if not concat.ok:
        state = STATE_CANCELLED if concat.code == "RENDER_CANCELLED" else STATE_FAILED
        write_state(project_dir, state=state, code=concat.code)
        return VideoRenderResult(
            ok=False,
            code=concat.code,
            scenes=results,
            warnings=warnings,
            errors=[{"path": "", "code": concat.code or "", "message": concat.message or ""}],
        )

    muxed = mux_audio(concat.output, audio_path, output, should_cancel=should_cancel)
    if not muxed.ok:
        write_state(project_dir, state=STATE_FAILED, code=muxed.code)
        return VideoRenderResult(
            ok=False,
            code=muxed.code,
            scenes=results,
            warnings=warnings,
            errors=[{"path": "", "code": muxed.code or "", "message": muxed.message or ""}],
        )

    manifest_path = _write_manifest(
        project_dir, spec, output, results, warnings, muxed.duration_sec
    )
    write_state(
        project_dir,
        state=STATE_SUCCEEDED,
        warnings=warnings,
        extra={"duration_sec": muxed.duration_sec, "scene_count": len(results)},
    )
    if on_progress:
        on_progress("done", total, total, "動画を生成しました")
    return VideoRenderResult(
        ok=True,
        output=output,
        manifest_path=manifest_path,
        duration_sec=muxed.duration_sec,
        scenes=results,
        warnings=warnings,
    )


def _write_manifest(
    project_dir: Path,
    spec: dict[str, Any],
    output: Path,
    scenes: list[SceneRender],
    warnings: list[str],
    duration_sec: float | None,
) -> Path:
    info = probe(output)
    manifest = {
        "schema_version": "1.0",
        "video_id": spec.get("video_id"),
        "title": spec.get("title"),
        "created_at": datetime.now(UTC).isoformat(),
        "duration_sec": duration_sec,
        "format": {
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "has_audio": info.has_audio,
        },
        "outputs": [
            {
                "path": output.name,
                "sha256": sha256_file(output),
                "size_bytes": output.stat().st_size,
            }
        ],
        "scenes": [
            {"scene_id": s.scene_id, "duration_sec": s.duration_sec} for s in scenes if s.ok
        ],
        "warnings": warnings,
    }
    import json

    path = project_dir / MANIFEST_FILE
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = ["SceneRender", "VideoRenderResult", "render_project"]
