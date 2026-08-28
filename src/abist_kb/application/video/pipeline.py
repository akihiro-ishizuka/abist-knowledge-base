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
from abist_kb.application.video.bgm import BgmPlan, load_bgm, pick_asset, plan_bgm, select_mood
from abist_kb.application.video.caption_timing import estimate_caption_duration
from abist_kb.application.video.capture_planner import resolve_profile, run_capture
from abist_kb.application.video.contact_sheet import build_contact_sheet
from abist_kb.application.video.content_quality import (
    write_storyboard_review,
)
from abist_kb.application.video.distribution import evaluate, write_manual_publish_note
from abist_kb.application.video.distribution import write_report as write_distribution_report
from abist_kb.application.video.image_assets import (
    attach_image_assets,
    ingest_image_assets,
    validate_image_asset_refs,
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
from abist_kb.application.video.qa import run_qa, write_report
from abist_kb.application.video.script_planner import author_script
from abist_kb.application.video.sound_events import (
    SoundAsset,
    SoundCue,
    load_palette,
    resolve_sound_events,
    used_attributions,
)
from abist_kb.application.video.subtitles import build_track, burn_in_filter, write_subtitles
from abist_kb.application.video.thumbnail import build_thumbnail
from abist_kb.application.video.video_metadata import METADATA_FILE, build_metadata, write_metadata
from abist_kb.application.video.video_renderer import render_project
from abist_kb.domain.scene_spec import MAX_MIN_DURATION_SEC
from abist_kb.domain.video_project_spec import (
    DEFAULT_VIDEO_QUALITY,
    QUALITY_FRAME_RATES,
    VIDEO_QUALITIES,
    frame_pixels,
)
from abist_kb.infrastructure.video.artifact_store import CITATIONS_FILE, scene_dir
from abist_kb.infrastructure.video.ffmpeg_runner import probe, run_ffmpeg

#: 同梱パレットの場所（リポジトリルートからの相対）。効果音と BGM を同じ manifest で管理する。
DEFAULT_PALETTE = Path("assets") / "sound-design" / "manifest.json"

#: 完成音声のラウドネス目標。配信プラットフォームの一般的な基準に合わせる。
#: 尺やシーン構成が変わっても体感音量が揃うので、社内で連続再生しても耳が疲れない。
TARGET_LUFS = -16.0
TARGET_TRUE_PEAK_DB = -1.5
LOUDNORM_FILTER = f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TRUE_PEAK_DB:g}:LRA=11"

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


def _format_for(
    aspect_ratio: str,
    target_duration_sec: dict[str, float] | None,
    *,
    quality: str = DEFAULT_VIDEO_QUALITY,
) -> dict[str, Any]:
    """アスペクト比と品質段から `format` を組む。

    画素寸法と fps の正本は `domain.video_project_spec`（layout / render_scene の写し）。
    以前はここが 1920x1080 を決め打ちしていたため、`high`（2560x1440 / 60fps）へは
    到達できなかった。
    """
    resolved_quality = quality if quality in VIDEO_QUALITIES else DEFAULT_VIDEO_QUALITY
    width, height = frame_pixels(aspect_ratio, resolved_quality)
    fmt: dict[str, Any] = {
        "aspect_ratio": aspect_ratio,
        "width": width,
        "height": height,
        "fps": QUALITY_FRAME_RATES[resolved_quality],
        "quality": resolved_quality,
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
        needed = estimate_caption_duration(text) + NARRATION_MARGIN_SEC if text else 0.0
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


def _caption_durations(scenes: list[dict[str, Any]]) -> dict[str, float]:
    """シーンごとのテロップを読み切るのに要る秒数。

    ナレーション音声は作らないので、映像の尺を決めるのは読速だけ。
    """
    durations: dict[str, float] = {}
    for scene in scenes:
        text = (scene.get("narration") or {}).get("text")
        if not text:
            continue
        durations[str(scene["id"])] = estimate_caption_duration(text)
    return durations


def run_pipeline(
    resolved: ResolveResult,
    *,
    docs_dir: Path,
    reports_dir: Path,
    repo_root: Path,
    title: str,
    script: dict[str, Any],
    purpose: str | None = None,
    target_duration_sec: dict[str, float] | None = None,
    aspect_ratio: str = "16:9",
    quality: str = DEFAULT_VIDEO_QUALITY,
    sound_intensity: str = "subtle",
    sound_enabled: bool = True,
    capture_profile: str | None = None,
    palette_path: Path | None = None,
    story_requirements: dict[str, Any] | None = None,
    image_assets: list[dict[str, Any]] | None = None,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PipelineResult:
    """**作者が書いた台本**と解決済みの題材から、動画一式を作る。

    台本を機械生成する経路は持たない。何を語りどう見せるかは書き手の判断で、
    見出しや箇条書きを拾って組み立てられるものではなかった。

    ナレーション音声は作らない。台本の `narration.text` はテロップとして映像へ
    焼き込まれ、音は効果音と BGM だけが乗る。
    """
    warnings: list[str] = [w["message"] for w in resolved.warnings]

    def progress(phase: str, current: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(phase, current, total, message)

    # 1. 台本の受け入れ（検証を通らない台本は描かない）
    progress("plan", 0, 6, "台本を検証しています")
    authored = author_script(
        script,
        resolved.inputs,
        docs_dir=docs_dir,
        target_duration_sec=target_duration_sec,
        story_requirements=story_requirements,
        strict=True,
    )
    warnings.extend(authored.warnings)
    if not authored.ok:
        code = (authored.errors[0].get("code") if authored.errors else None) or (
            "INVALID_SCRIPT_DRAFT"
        )
        return PipelineResult(
            ok=False,
            code=code,
            errors=authored.errors,
            warnings=warnings,
            duration_plan=authored.duration_plan,
        )

    # 2. プロジェクト作成
    progress("project", 1, 6, "プロジェクトを作成しています")
    _apply_scene_minimums(authored.scenes, authored.duration_plan)
    project_format = _format_for(aspect_ratio, target_duration_sec, quality=quality)
    created = create_project(
        {
            "title": title,
            "purpose": purpose,
            "format": project_format,
            "scenes": authored.scenes,
            "sound_events": authored.sound_events,
            "story_requirements": story_requirements,
        },
        resolved,
        reports_dir=reports_dir,
    )
    if not created.ok or created.project is None:
        return PipelineResult(
            ok=False, code=created.code, errors=created.errors or [], warnings=warnings
        )
    project = created.project

    # 2-b. 持ち込み画像を取り込み、image beat へ結び付ける
    if image_assets:
        ingested = ingest_image_assets(image_assets, project.dir)
        if not ingested.ok:
            write_state(project.dir, state=STATE_FAILED, code="INVALID_IMAGE_ASSET")
            return PipelineResult(
                ok=False,
                code="INVALID_IMAGE_ASSET",
                video_id=project.video_id,
                project_dir=project.dir,
                errors=ingested.errors,
                warnings=warnings,
            )
        reference_errors = validate_image_asset_refs(
            authored.scenes, registered_ids={a["id"] for a in ingested.assets}
        )
        if reference_errors:
            write_state(project.dir, state=STATE_FAILED, code="UNKNOWN_IMAGE_ASSET")
            return PipelineResult(
                ok=False,
                code="UNKNOWN_IMAGE_ASSET",
                video_id=project.video_id,
                project_dir=project.dir,
                errors=reference_errors,
                warnings=warnings,
            )
        attach_image_assets(authored.scenes, ingested.assets)
        spec = load_project(project.dir) or {}
        spec["image_assets"] = ingested.assets
        spec["scenes"] = authored.scenes
        save_project(project.dir, spec)

    # レンダリング前に全表示文を残す（人が観る前の自己点検材料）。
    write_storyboard_review(project.dir, authored.scenes, authored.sound_events)

    return finish_project(
        project.dir,
        video_id=project.video_id,
        scenes=authored.scenes,
        sound_events=authored.sound_events,
        used_paths=authored.used_paths,
        docs_dir=docs_dir,
        repo_root=repo_root,
        sound_intensity=sound_intensity,
        sound_enabled=sound_enabled,
        capture_profile=capture_profile,
        palette_path=palette_path,
        duration_plan=authored.duration_plan,
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

    # 4. テロップの尺とタイムライン
    # ナレーション音声は作らない。読速から算出した「読み切るのに要る時間」で
    # 映像側のタイムラインを組む（`audio_sync` は尺の出所を問わない）。
    _progress("audio", 3, 6, "テロップの尺を計算しています")
    caption_durations = _caption_durations(scenes)

    timeline = plan_timeline(
        scenes, scene_durations=scene_durations, narration_durations=caption_durations
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

    # 5. テロップ（焼き込み用の SRT と、手動アップロード用のサイドカー）
    _progress("subtitles", 4, 6, "テロップを作成しています")
    spec_format = (load_project(project_dir) or {}).get("format") or {}
    aspect_ratio = str(spec_format.get("aspect_ratio") or "16:9")
    burn_in_captions = (spec_format.get("captions") or "burn_in") != "sidecar_only"
    track = build_track(
        scenes, offsets=offsets, durations=final_durations, aspect_ratio=aspect_ratio
    )
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
        # ダッキングの対象になる実音声は無い（テロップは音を出さない）。
        narration_starts={},
        # beat アンカーは描画側の実測時刻に載せる（尺を beat 数で等分した推定ではない）。
        beat_times={s.scene_id: s.beat_times for s in render.scenes if s.beat_times},
        intensity=sound_intensity,
        enabled=sound_enabled,
    )
    warnings.extend(sound.warnings)
    (project_dir / "audio").mkdir(parents=True, exist_ok=True)
    (project_dir / "audio" / "sound-cues.json").write_text(
        json.dumps(sound.to_manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 6-b. BGM（用途からムードを決め、動画全体へ敷く）
    bgm_plan: BgmPlan | None = None
    if sound_enabled:
        spec_for_bgm = load_project(project_dir) or {}
        mood = select_mood(
            spec_for_bgm.get("purpose") or spec_for_bgm.get("title"),
            override=(spec_for_bgm.get("format") or {}).get("bgm_mood"),
        )
        if mood is not None:
            bgm_assets, bgm_warnings = load_bgm(repo_root / (palette_path or DEFAULT_PALETTE))
            warnings.extend(bgm_warnings)
            asset = pick_asset(bgm_assets, mood)
            total = sum(final_durations.values())
            if asset is not None and total > 0:
                bgm_plan = plan_bgm(total, asset)
                (project_dir / "audio" / "bgm.json").write_text(
                    json.dumps(bgm_plan.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    # 7. BGM と効果音を載せ、テロップを焼き込んで最終形にする
    output = render.output
    subtitle_source = subtitle_paths["srt"] if burn_in_captions and track.cues else None
    if output is not None and (sound.cues or bgm_plan is not None or subtitle_source is not None):
        final = project_dir / "output-with-audio.mp4"
        if _finalize_video(
            output,
            sound.cues,
            assets,
            final,
            subtitle_path=subtitle_source,
            aspect_ratio=aspect_ratio,
            bgm=bgm_plan,
        ):
            output = final
        else:
            warnings.append("BGM・効果音・テロップの合成に失敗したため元映像を出力しました")

    # 8. 出典（効果音の帰属を含む）
    spec = load_project(project_dir) or {}
    attributions = used_attributions(sound.cues, assets)
    if bgm_plan is not None:
        # BGM も帰属を書く（説明文へ転記される。使った音の出所は必ず残す）。
        attributions.append(
            {
                "sound_id": bgm_plan.asset.id,
                "license": bgm_plan.asset.license,
                "attribution": bgm_plan.asset.attribution,
                "sha256": bgm_plan.asset.sha256,
            }
        )
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

    spec["subtitles"] = {
        "srt": "subtitles/narration.srt",
        "vtt": "subtitles/narration.vtt",
        # 焼き込んだかどうかは QA が見る（無音で観る動画で字幕が画面に無いのは欠陥）。
        "burned_in": bool(
            subtitle_source is not None and output == project_dir / "output-with-audio.mp4"
        ),
        "cue_count": len(track.cues),
    }
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
    contact_sheet = build_contact_sheet(
        output,
        project_dir,
        scene_ids=[str(s["id"]) for s in scenes],
        offsets=offsets,
        durations=final_durations,
        aspect_ratio=aspect_ratio,
    )
    warnings.extend(contact_sheet.warnings)

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


def _finalize_video(
    video: Path,
    cues: list[SoundCue],
    assets: list[SoundAsset],
    target: Path,
    *,
    subtitle_path: Path | None = None,
    aspect_ratio: str = "16:9",
    bgm: BgmPlan | None = None,
) -> bool:
    """BGM と効果音を載せ、テロップを焼き込んで最終形にする（ffmpeg 1パス）。

    音と字幕を別々に通すと映像のエンコードが2回増える。concat で1回、ここで1回の
    **計2回**に抑えるため、音の合成と焼き込みを同じコマンドでやる。

    字幕を焼くときだけ映像を再エンコードする（`-c:v copy` では焼けない）。
    音だけなら映像はコピーで済ませる。

    音が乗るときは最後に `loudnorm` を通す。完成尺ごとに体感音量がばらつくと、
    社内で連続再生したときに音量つまみを触ることになる。
    """
    by_id = {asset.id: Path(asset.path) for asset in assets}
    args: list[str] = ["-i", str(video)]
    filters: list[str] = []
    labels: list[str] = []
    input_index = 1

    if bgm is not None and Path(bgm.asset.path).is_file():
        # ループさせてから動画尺ちょうどで切る。`-stream_loop` は入力側の指定なので
        # 対象の `-i` の直前に置く。
        args += ["-stream_loop", str(max(0, bgm.loops - 1)), "-i", str(bgm.asset.path)]
        filters.append(
            f"[{input_index}:a]atrim=duration={bgm.total_sec:g},asetpts=PTS-STARTPTS,"
            f"volume={bgm.gain_db:.1f}dB,"
            f"afade=t=in:st=0:d={bgm.fade_in_sec:g},"
            f"afade=t=out:st={bgm.fade_out_start_sec:g}:d={bgm.fade_out_sec:g}[bgm]"
        )
        labels.append("[bgm]")
        input_index += 1

    for cue_index, cue in enumerate(cues):
        path = by_id.get(cue.sound_id)
        if path is None or not path.is_file():
            continue
        args += ["-i", str(path)]
        delay_ms = max(0, int(cue.t_sec * 1000))
        label = f"fx{cue_index}"
        filters.append(
            f"[{input_index}:a]volume={cue.gain_db:.1f}dB,adelay={delay_ms}|{delay_ms}[{label}]"
        )
        labels.append(f"[{label}]")
        input_index += 1

    burn_in = subtitle_path is not None and subtitle_path.is_file()
    if not labels and not burn_in:
        return False

    command = [*args]
    if labels:
        # BGM を尺の基準にする（効果音だけだと最後の音で切れてしまう）。
        duration_mode = "first" if bgm is not None else "longest"
        filters.append(
            f"{''.join(labels)}amix=inputs={len(labels)}"
            f":duration={duration_mode}:dropout_transition=0:normalize=0[mix]"
        )
        filters.append(f"[mix]{LOUDNORM_FILTER}[aout]")
        command += ["-filter_complex", ";".join(filters)]
    if burn_in:
        command += ["-vf", burn_in_filter(subtitle_path, aspect_ratio=aspect_ratio)]
    command += ["-map", "0:v:0"]
    if labels:
        command += ["-map", "[aout]"]
    command += (
        ["-c:v", "libx264", "-crf", "20", "-preset", "medium"]
        if burn_in
        else [
            "-c:v",
            "copy",
        ]
    )
    if labels:
        command += ["-c:a", "aac", "-ar", "44100"]
    command += ["-y", str(target)]

    result = run_ffmpeg(command, timeout_seconds=900.0)
    return result.exit_code == 0 and target.is_file()


__all__ = ["DEFAULT_PALETTE", "PipelineResult", "finish_project", "run_pipeline"]
