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
from abist_kb.application.video.capture_planner import resolve_profile, run_capture
from abist_kb.application.video.distribution import evaluate, write_manual_publish_note
from abist_kb.application.video.distribution import write_report as write_distribution_report
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
from abist_kb.application.video.qa import run_qa, write_report
from abist_kb.application.video.script_planner import plan_video
from abist_kb.application.video.sound_events import (
    load_palette,
    resolve_sound_events,
    used_attributions,
)
from abist_kb.application.video.subtitles import build_track, write_subtitles
from abist_kb.application.video.thumbnail import build_thumbnail
from abist_kb.application.video.video_metadata import METADATA_FILE, build_metadata, write_metadata
from abist_kb.application.video.video_renderer import render_project
from abist_kb.domain.scene_spec import MAX_MIN_DURATION_SEC
from abist_kb.infrastructure.video.artifact_store import CITATIONS_FILE, scene_dir
from abist_kb.infrastructure.video.ffmpeg_runner import mux_audio, probe
from abist_kb.infrastructure.video.tts_provider import build_provider, estimate_duration

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
    #: `qa-report.json` の内容（Phase 9 の正式レポート）。
    qa_report: dict[str, Any] | None = None
    thumbnail: Path | None = None
    metadata_path: Path | None = None
    #: `distribution-report.json` の内容（Phase 11）。
    distribution: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    code: str | None = None
    #: 目標尺から逆算した構成（`target_duration_sec` を渡したときだけ入る）。
    duration_plan: dict[str, Any] | None = None


def _format_for(aspect_ratio: str, target_duration_sec: dict[str, float] | None) -> dict[str, Any]:
    """アスペクト比から `format` を組む（画素寸法の正本は layout 側と揃える）。"""
    width, height = (1080, 1920) if aspect_ratio == "9:16" else (1920, 1080)
    fmt: dict[str, Any] = {
        "aspect_ratio": aspect_ratio,
        "width": width,
        "height": height,
        "fps": 30,
    }
    if target_duration_sec:
        fmt["target_duration_sec"] = dict(target_duration_sec)
    return fmt


#: ナレーションの前後に置く間（秒）。読み終わりと同時に切り替わると忙しない。
NARRATION_MARGIN_SEC = 1.0
#: 固定尺で扱うカード面の scene_kind（`duration_planner.CARD_ROLES` と対応）。
CARD_KINDS = frozenset({"title", "chapter", "ending", "cta"})


def _apply_scene_minimums(
    scenes: list[dict[str, Any]], duration_plan: dict[str, Any] | None
) -> None:
    """各シーンに尺の下限を与える（**描画前**に決める必要がある）。

    ナレーションが映像より長いと、音声を載せる段で末尾が切れる。`audio_sync` は
    タイムラインを計算できるが、**既に描き終わった映像を伸ばすことはできない**。
    そこで描画前に「読み上げに必要な長さ」を見積もり、映像側の下限にする。

    `duration_plan` があれば1シーンの目標尺も下限に含める（章扉のように
    ナレーションが短い面でも、動画としての間を確保するため）。
    """
    card_sec = float((duration_plan or {}).get("card_target_sec") or 0.0)
    body_sec = float((duration_plan or {}).get("body_target_sec") or 0.0)
    for scene in scenes:
        # カード面（表紙・章扉・エンドカード）は固定尺、本編は配分された尺。
        # 一律にすると、実際は短く終わるカードのぶんだけ完成尺が目標を割る。
        floor = card_sec if scene.get("kind") in CARD_KINDS else body_sec
        text = (scene.get("narration") or {}).get("text") or ""
        needed = estimate_duration(text) + NARRATION_MARGIN_SEC if text else 0.0
        minimum = max(floor, needed)
        if minimum > 0:
            scene["min_duration_sec"] = round(min(minimum, MAX_MIN_DURATION_SEC), 2)


def _attach_captures(project_dir: Path, images_by_scene: dict[str, str]) -> None:
    """撮影した PNG を該当シーンの `image` beat へ結びつける。

    `image.path` は **scene-spec.json からの相対パス**なので、`captures/` の
    画像をシーンディレクトリへ複製してからパスを書き換える。こうすると
    成果物ツリーの外を指す余地が無くなる（検証も相対パスしか通さない）。
    """
    import shutil

    spec = load_project(project_dir)
    if spec is None:
        return
    changed = False
    for scene in spec.get("scenes") or []:
        relative = images_by_scene.get(str(scene.get("id")))
        if not relative:
            continue
        source = project_dir / relative
        if not source.is_file():
            continue
        target_dir = scene_dir(project_dir, str(scene.get("id")))
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target_dir / source.name)
        scene_spec = scene.get("scene_spec") or {}
        for beat in scene_spec.get("beats") or []:
            if isinstance(beat, dict) and beat.get("type") == "image":
                beat["path"] = source.name
                changed = True
    if changed:
        save_project(project_dir, spec)


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
    target_duration_sec: dict[str, float] | None = None,
    aspect_ratio: str = "16:9",
    sound_intensity: str = "subtle",
    sound_enabled: bool = True,
    chat_fn: Any | None = None,
    capture_profile: str | None = None,
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
        resolved.inputs,
        docs_dir=docs_dir,
        title=title,
        purpose=purpose,
        chat_fn=chat_fn,
        target_duration_sec=target_duration_sec,
    )
    warnings.extend(plan.warnings)
    if not plan.ok:
        # 目標尺に届かない場合は水増しせず、そのコードのまま返す
        code = (plan.errors[0].get("code") if plan.errors else None) or "INVALID_SCRIPT_DRAFT"
        return PipelineResult(
            ok=False,
            code=code,
            errors=plan.errors,
            warnings=warnings,
            duration_plan=plan.duration_plan,
        )

    # 2. プロジェクト作成
    progress("project", 1, 6, "プロジェクトを作成しています")
    _apply_scene_minimums(plan.scenes, plan.duration_plan)
    project_format = _format_for(aspect_ratio, target_duration_sec)
    created = create_project(
        {
            "title": title,
            "purpose": purpose,
            "format": project_format,
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

    return finish_project(
        project.dir,
        video_id=project.video_id,
        scenes=plan.scenes,
        sound_events=plan.sound_events,
        used_paths=plan.used_paths,
        docs_dir=docs_dir,
        repo_root=repo_root,
        tts=tts,
        manual_audio_dir=manual_audio_dir,
        sound_intensity=sound_intensity,
        sound_enabled=sound_enabled,
        capture_profile=capture_profile,
        palette_path=palette_path,
        duration_plan=plan.duration_plan,
        warnings=warnings,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )


def finish_project(
    project_dir: Path,
    *,
    video_id: str | None,
    scenes: list[dict[str, Any]],
    sound_events: list[dict[str, Any]],
    used_paths: set[str],
    docs_dir: Path,
    repo_root: Path,
    tts: str = "none",
    manual_audio_dir: Path | None = None,
    sound_intensity: str = "subtle",
    sound_enabled: bool = True,
    capture_profile: str | None = None,
    palette_path: Path | None = None,
    duration_plan: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PipelineResult:
    """作成済みプロジェクトを描画して成果物一式まで仕上げる。

    `run_pipeline`（入力解決から始める経路）と `render_video` ジョブ
    （既存プロジェクトを描き直す経路）の**共通の後半**。ここを1本にしておかないと、
    ジョブ経由で作った動画だけ QA やメタデータが欠ける、という食い違いが起きる。
    """
    warnings = list(warnings or [])

    def _progress(phase: str, current: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(phase, current, total, message)

    # 2-b. 画面キャプチャ（既定無効。失敗しても動画生成は止めない）
    capture_commit_sha: str | None = None
    if capture_profile:
        capture_plan = resolve_profile(capture_profile, repo_root=repo_root)
        outcome = run_capture(capture_plan, project_dir)
        warnings.extend(outcome.warnings)
        capture_commit_sha = outcome.resolved_commit_sha
        if outcome.images_by_scene:
            _attach_captures(project_dir, outcome.images_by_scene)

    # 3. シーン描画
    _progress("render", 2, 6, "シーンを描画しています")
    render = render_project(
        project_dir,
        docs_dir=docs_dir,
        repo_root=repo_root,
        should_cancel=should_cancel,
        on_progress=lambda p, c, t, m: _progress("render", 2, 6, m),
    )
    warnings.extend(render.warnings)
    if not render.ok:
        return PipelineResult(
            ok=False,
            code=render.code,
            project_dir=project_dir,
            video_id=video_id,
            errors=render.errors,
            warnings=warnings,
        )
    scene_durations = {s.scene_id: (s.duration_sec or 0.0) for s in render.scenes}

    # 4. ナレーションとタイムライン
    _progress("audio", 3, 6, "ナレーションを準備しています")
    provider = build_provider(tts, audio_dir=manual_audio_dir)
    narration_durations, narration_paths, tts_warnings = _synthesize_narration(
        scenes, provider, project_dir / "audio"
    )
    warnings.extend(tts_warnings)

    timeline = plan_timeline(
        scenes, scene_durations=scene_durations, narration_durations=narration_durations
    )
    warnings.extend(timeline.warnings)
    if not timeline.ok:
        write_state(project_dir, state=STATE_FAILED, code="NARRATION_TOO_LONG")
        return PipelineResult(
            ok=False,
            code="NARRATION_TOO_LONG",
            project_dir=project_dir,
            video_id=video_id,
            errors=[e.to_dict() for e in timeline.errors],
            warnings=warnings,
        )
    offsets = scene_offsets(timeline.timings)
    final_durations = {t.scene_id: t.final_sec for t in timeline.timings}

    # 5. 字幕
    _progress("subtitles", 4, 6, "字幕を作成しています")
    track = build_track(scenes, offsets=offsets, durations=final_durations)
    warnings.extend(track.warnings)
    subtitle_paths = write_subtitles(track, project_dir / "subtitles")

    # 6. 効果音
    _progress("sound", 5, 6, "効果音を配置しています")
    assets, palette_warnings = load_palette(repo_root / (palette_path or DEFAULT_PALETTE))
    warnings.extend(palette_warnings)
    sound = resolve_sound_events(
        sound_events,
        assets=assets,
        offsets=offsets,
        durations=final_durations,
        narration_starts={s: offsets[s] for s in narration_durations if s in offsets},
        intensity=sound_intensity,
        enabled=sound_enabled,
    )
    warnings.extend(sound.warnings)
    (project_dir / "audio").mkdir(parents=True, exist_ok=True)
    (project_dir / "audio" / "sound-cues.json").write_text(
        json.dumps(sound.to_manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 7. 音声を映像へ載せる（ナレーションがあれば）
    output = render.output
    if narration_paths and output is not None:
        merged = _merge_narration(narration_paths, scenes, offsets, project_dir)
        if merged is not None:
            final = project_dir / "output-with-audio.mp4"
            muxed = mux_audio(output, merged, final, should_cancel=should_cancel)
            if muxed.ok:
                output = final
            else:
                warnings.append("音声の合成に失敗したため無音のまま出力しました")

    # 8. 出典（効果音の帰属を含む）
    spec = load_project(project_dir) or {}
    attributions = used_attributions(sound.cues, assets)
    citations: dict[str, Any] = {
        "sources": spec.get("sources") or [],
        "sound_attributions": attributions,
    }
    if capture_commit_sha:
        # 「どの時点のアプリ画面か」は出典の一部（説明文にも転記する）
        citations["capture"] = {"resolved_commit_sha": capture_commit_sha}
    (project_dir / CITATIONS_FILE).write_text(
        json.dumps(citations, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    spec["subtitles"] = {"srt": "subtitles/narration.srt", "vtt": "subtitles/narration.vtt"}
    save_project(project_dir, spec)
    info = probe(output) if output else None

    # 9. サムネイルとメタデータ（手動投稿パック）
    _progress("package", 6, 8, "サムネイルとメタデータを作成しています")
    metadata_result = build_metadata(
        spec,
        offsets=offsets,
        duration_sec=info.duration_sec if info else None,
        sound_attributions=attributions,
        capture_commit_sha=capture_commit_sha,
    )
    warnings.extend(metadata_result.warnings)
    write_metadata(metadata_result, project_dir)
    thumb = build_thumbnail(
        output,
        project_dir,
        chapters=(metadata_result.metadata or {}).get("chapters"),
        duration_sec=info.duration_sec if info else None,
    )
    warnings.extend(thumb.warnings)

    # 10. QA（正式レポート）と配布判定
    _progress("qa", 7, 8, "QA を実行しています")
    qa_report = run_qa(project_dir, spec=spec, output=output, used_paths=used_paths)
    write_report(qa_report, project_dir)
    decision = evaluate(
        spec,
        project_dir,
        qa=qa_report.to_dict(),
        citations=citations,
        requested_public=bool((spec.get("distribution") or {}).get("public_candidate")),
    )
    write_distribution_report(decision, project_dir)
    write_manual_publish_note(decision, project_dir)

    # 入力使用の指摘は従来どおり別立てでも返す（呼び出し側の互換のため）
    qa_findings = [e.to_dict() for e in check_input_usage(spec, used_paths)]

    write_state(
        project_dir,
        state=STATE_SUCCEEDED,
        warnings=[*warnings],
        extra={
            "duration_sec": info.duration_sec if info else None,
            "scene_count": len(render.scenes),
            "qa_findings": qa_findings,
            "qa_ok": qa_report.ok,
            "phase": "done",
        },
    )
    _progress("done", 8, 8, "動画一式を生成しました")
    return PipelineResult(
        ok=True,
        duration_plan=duration_plan,
        video_id=video_id,
        project_dir=project_dir,
        output=output,
        duration_sec=info.duration_sec if info else None,
        scene_count=len(render.scenes),
        subtitle_paths=subtitle_paths,
        sound_cue_count=len(sound.cues),
        qa_findings=qa_findings,
        qa_report=qa_report.to_dict(),
        thumbnail=thumb.path,
        metadata_path=project_dir / METADATA_FILE,
        distribution=decision.to_dict(),
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


__all__ = ["DEFAULT_PALETTE", "PipelineResult", "finish_project", "run_pipeline"]
