"""Markdown から視聴可能な社内動画までの通し実行。

Phase 1〜6 を1本に繋ぐ:

```
入力解決 → 台本 → シーン描画 → ナレーション尺 → タイムライン
       → 字幕 → 効果音 → 音声ミックス → 結合 → 成果物
```

**外部プロバイダが無くても完走する。** LLM 無しならルールベース台本、
TTS 無しなら `none`（無音）または手動音声で成立する。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.application.video.audio_sync import (
    plan_timeline,
    scene_offsets,
)
from abist_kb.application.video.input_resolver import ResolveResult
from abist_kb.application.video.project_store import (
    STATE_FAILED,
    STATE_SUCCEEDED,
    check_input_usage,
    create_project,
    load_project,
    save_project,
    write_state,
)
from abist_kb.application.video.script_planner import plan_video
from abist_kb.application.video.sound_events import (
    load_palette,
    resolve_sound_events,
    used_attributions,
)
from abist_kb.application.video.subtitles import build_track, write_subtitles
from abist_kb.application.video.video_renderer import render_project
from abist_kb.infrastructure.video.artifact_store import CITATIONS_FILE
from abist_kb.infrastructure.video.ffmpeg_runner import mux_audio, probe
from abist_kb.infrastructure.video.tts_provider import build_provider

#: 同梱パレットの場所（リポジトリルートからの相対）。
DEFAULT_PALETTE = Path("assets") / "sound-design" / "manifest.json"

ProgressFn = Callable[[str, int, int, str], None]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    ok: bool
    video_id: str | None = None
    project_dir: Path | None = None
    output: Path | None = None
    duration_sec: float | None = None
    scene_count: int = 0
    subtitle_paths: dict[str, Path] = field(default_factory=dict)
    sound_cue_count: int = 0
    qa_findings: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    code: str | None = None


def _synthesize_narration(
    scenes: list[dict[str, Any]], provider: Any, audio_dir: Path
) -> tuple[dict[str, float], dict[str, Path], list[str]]:
    """シーンごとにナレーションを合成し、**尺を返す**。"""
    durations: dict[str, float] = {}
    paths: dict[str, Path] = {}
    warnings: list[str] = []
    audio_dir.mkdir(parents=True, exist_ok=True)

    for scene in scenes:
        scene_id = str(scene["id"])
        text = (scene.get("narration") or {}).get("text")
        if not text:
            continue
        target = audio_dir / f"{scene_id}.wav"
        if hasattr(provider, "synthesize_scene"):
            result = provider.synthesize_scene(scene_id, text)
        else:
            result = provider.synthesize(text, target)
        if not result.ok:
            warnings.append(f"{scene_id}: {result.message or '音声を用意できませんでした'}")
            # 尺の見積もりは使う（字幕とタイムラインは成立させる）
            if result.duration_sec:
                durations[scene_id] = result.duration_sec
            continue
        durations[scene_id] = result.duration_sec
        if result.audio_path is not None:
            paths[scene_id] = result.audio_path
    return durations, paths, warnings


def run_pipeline(
    resolved: ResolveResult,
    *,
    docs_dir: Path,
    reports_dir: Path,
    repo_root: Path,
    title: str,
    purpose: str | None = None,
    tts: str = "none",
    manual_audio_dir: Path | None = None,
    sound_intensity: str = "subtle",
    sound_enabled: bool = True,
    chat_fn: Any | None = None,
    palette_path: Path | None = None,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PipelineResult:
    """入力解決の結果から動画一式を作る。"""
    warnings: list[str] = [w["message"] for w in resolved.warnings]

    def progress(phase: str, current: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(phase, current, total, message)

    # 1. 台本と SceneSpec
    progress("plan", 0, 6, "台本を作成しています")
    plan, _draft = plan_video(
        resolved.inputs, docs_dir=docs_dir, title=title, purpose=purpose, chat_fn=chat_fn
    )
    warnings.extend(plan.warnings)
    if not plan.ok:
        return PipelineResult(
            ok=False, code="INVALID_SCRIPT_DRAFT", errors=plan.errors, warnings=warnings
        )

    # 2. プロジェクト作成
    progress("project", 1, 6, "プロジェクトを作成しています")
    created = create_project(
        {
            "title": title,
            "purpose": purpose,
            "scenes": plan.scenes,
            "sound_events": plan.sound_events,
        },
        resolved,
        reports_dir=reports_dir,
    )
    if not created.ok or created.project is None:
        return PipelineResult(
            ok=False, code=created.code, errors=created.errors or [], warnings=warnings
        )
    project = created.project

    # 3. シーン描画
    progress("render", 2, 6, "シーンを描画しています")
    render = render_project(
        project.dir,
        docs_dir=docs_dir,
        repo_root=repo_root,
        should_cancel=should_cancel,
        on_progress=lambda p, c, t, m: progress("render", 2, 6, m),
    )
    warnings.extend(render.warnings)
    if not render.ok:
        return PipelineResult(
            ok=False,
            code=render.code,
            project_dir=project.dir,
            video_id=project.video_id,
            errors=render.errors,
            warnings=warnings,
        )
    scene_durations = {s.scene_id: (s.duration_sec or 0.0) for s in render.scenes}

    # 4. ナレーションとタイムライン
    progress("audio", 3, 6, "ナレーションを準備しています")
    provider = build_provider(tts, audio_dir=manual_audio_dir)
    narration_durations, narration_paths, tts_warnings = _synthesize_narration(
        plan.scenes, provider, project.dir / "audio"
    )
    warnings.extend(tts_warnings)

    timeline = plan_timeline(
        plan.scenes, scene_durations=scene_durations, narration_durations=narration_durations
    )
    warnings.extend(timeline.warnings)
    if not timeline.ok:
        write_state(project.dir, state=STATE_FAILED, code="NARRATION_TOO_LONG")
        return PipelineResult(
            ok=False,
            code="NARRATION_TOO_LONG",
            project_dir=project.dir,
            video_id=project.video_id,
            errors=[e.to_dict() for e in timeline.errors],
            warnings=warnings,
        )
    offsets = scene_offsets(timeline.timings)
    final_durations = {t.scene_id: t.final_sec for t in timeline.timings}

    # 5. 字幕
    progress("subtitles", 4, 6, "字幕を作成しています")
    track = build_track(plan.scenes, offsets=offsets, durations=final_durations)
    warnings.extend(track.warnings)
    subtitle_paths = write_subtitles(track, project.dir / "subtitles")

    # 6. 効果音
    progress("sound", 5, 6, "効果音を配置しています")
    assets, palette_warnings = load_palette(repo_root / (palette_path or DEFAULT_PALETTE))
    warnings.extend(palette_warnings)
    sound = resolve_sound_events(
        plan.sound_events,
        assets=assets,
        offsets=offsets,
        durations=final_durations,
        narration_starts={s: offsets[s] for s in narration_durations if s in offsets},
        intensity=sound_intensity,
        enabled=sound_enabled,
    )
    warnings.extend(sound.warnings)
    (project.dir / "audio").mkdir(parents=True, exist_ok=True)
    (project.dir / "audio" / "sound-cues.json").write_text(
        json.dumps(sound.to_manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 7. 音声を映像へ載せる（ナレーションがあれば）
    output = render.output
    if narration_paths and output is not None:
        merged = _merge_narration(narration_paths, plan.scenes, offsets, project.dir)
        if merged is not None:
            final = project.dir / "output-with-audio.mp4"
            muxed = mux_audio(output, merged, final, should_cancel=should_cancel)
            if muxed.ok:
                output = final
            else:
                warnings.append("音声の合成に失敗したため無音のまま出力しました")

    # 8. QA: 主入力が使われたか
    spec = load_project(project.dir) or {}
    qa = [e.to_dict() for e in check_input_usage(spec, plan.used_paths)]

    # 9. 出典に効果音の帰属を足す
    citations = {
        "sources": spec.get("sources") or [],
        "sound_attributions": used_attributions(sound.cues, assets),
    }
    (project.dir / CITATIONS_FILE).write_text(
        json.dumps(citations, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    spec["subtitles"] = {"srt": "subtitles/narration.srt", "vtt": "subtitles/narration.vtt"}
    save_project(project.dir, spec)
    info = probe(output) if output else None
    write_state(
        project.dir,
        state=STATE_SUCCEEDED,
        warnings=[*warnings],
        extra={
            "duration_sec": info.duration_sec if info else None,
            "scene_count": len(render.scenes),
            "qa_findings": qa,
        },
    )
    progress("done", 6, 6, "動画一式を生成しました")
    return PipelineResult(
        ok=True,
        video_id=project.video_id,
        project_dir=project.dir,
        output=output,
        duration_sec=info.duration_sec if info else None,
        scene_count=len(render.scenes),
        subtitle_paths=subtitle_paths,
        sound_cue_count=len(sound.cues),
        qa_findings=qa,
        warnings=warnings,
    )


def _merge_narration(
    paths: dict[str, Path],
    scenes: list[dict[str, Any]],
    offsets: dict[str, float],
    project_dir: Path,
) -> Path | None:
    """シーンごとの音声を1本のトラックへ並べる（ffmpeg の adelay + amix）。"""
    from abist_kb.infrastructure.video.ffmpeg_runner import run_ffmpeg

    ordered = [(s["id"], paths[s["id"]]) for s in scenes if s["id"] in paths]
    if not ordered:
        return None
    args: list[str] = []
    for _scene_id, path in ordered:
        args += ["-i", str(path)]
    filters = []
    for index, (scene_id, _path) in enumerate(ordered):
        delay_ms = int(offsets.get(scene_id, 0.0) * 1000)
        filters.append(f"[{index}:a]adelay={delay_ms}|{delay_ms}[a{index}]")
    mix_inputs = "".join(f"[a{i}]" for i in range(len(ordered)))
    filters.append(f"{mix_inputs}amix=inputs={len(ordered)}:dropout_transition=0:normalize=0[out]")
    target = project_dir / "audio" / "narration.m4a"
    result = run_ffmpeg(
        [*args, "-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "aac", str(target)]
    )
    return target if result.exit_code == 0 and target.exists() else None


__all__ = ["DEFAULT_PALETTE", "PipelineResult", "run_pipeline"]
