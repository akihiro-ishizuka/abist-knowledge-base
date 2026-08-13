"""動画レンダリングの永続ジョブ化（`render_video`）。

`render_scene` ジョブ（purring）と**同じリース規約**に従う: `run_job` が
`ResourceKind.RENDER` のリースをハンドラ呼び出しの前に取得・更新・解放するので、
ハンドラ自身はリースを取らない。

動画は多シーンを直列で描くため実行時間が長い。シーン境界と ffmpeg 呼び出しの
境界で `run.cancel_requested` を見て、途中で止められるようにする。

**キャプチャは `capture_profile`（登録済みプロファイル名）だけを受け取る。**
ジョブ params に起動コマンドが入ることはない。
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any

from abist_kb.application.video import catalog
from abist_kb.application.video.pipeline import PipelineResult, run_pipeline
from abist_kb.application.video.project_store import load_project
from abist_kb.domain.job import JobState, ResourceKind, Severity
from abist_kb.infrastructure.jobs.supervisor import JobRunContext

VIDEO_JOB_KIND = "render_video"


def _result_payload(outcome: PipelineResult) -> dict[str, Any]:
    return {
        "video_id": outcome.video_id,
        "project_dir": str(outcome.project_dir) if outcome.project_dir else None,
        "output": str(outcome.output) if outcome.output else None,
        "duration_sec": outcome.duration_sec,
        "scene_count": outcome.scene_count,
        "subtitles": {k: str(v) for k, v in outcome.subtitle_paths.items()},
        "sound_cue_count": outcome.sound_cue_count,
        "thumbnail": str(outcome.thumbnail) if outcome.thumbnail else None,
        "qa_ok": (outcome.qa_report or {}).get("ok"),
        "distribution": outcome.distribution,
        "duration_plan": outcome.duration_plan,
        "warnings": list(outcome.warnings),
    }


def render_video_job_handler(run: JobRunContext) -> None:
    """`render_video` ジョブ種別のハンドラ。

    `run.job.params` のキー:
    `project_dir` / `docs_dir` / `repo_root` / `reports_dir`（str）、
    任意で `sound_intensity` / `capture_profile`。

    既存プロジェクト（`project-spec.json` がある）を描画する経路と、
    入力から作り直す経路の両方を1本のハンドラで扱う。
    """
    params = run.job.params
    project_dir = Path(params["project_dir"])
    docs_dir = Path(params["docs_dir"])
    repo_root = Path(params["repo_root"])
    reports_dir = Path(params.get("reports_dir") or project_dir.parent.parent)

    spec = load_project(project_dir)
    if spec is None:
        run.finish_as(
            JobState.FAILED,
            error={
                "code": "VIDEO_NOT_FOUND",
                "message": f"project-spec.json がありません: {project_dir}",
            },
        )
        return

    total = max(1, len(spec.get("scenes") or []))
    run.emit(phase="render", current=0, total=total, message="動画の生成を開始しました")

    from abist_kb.application.video.render_from_spec import render_existing_project

    outcome = render_existing_project(
        project_dir,
        docs_dir=docs_dir,
        repo_root=repo_root,
        reports_dir=reports_dir,
        sound_intensity=str(params.get("sound_intensity") or "subtle"),
        capture_profile=params.get("capture_profile"),
        should_cancel=run.cancel_requested,
        on_progress=lambda phase, current, _total, message: run.emit(
            phase=phase, current=current, total=total, message=message
        ),
    )

    # カタログは索引であって正本ではない。書けなくてもジョブの成否は変えない
    # （取りこぼしは list_videos の自己修復で回復する）。
    if run.conn is not None:
        with contextlib.suppress(sqlite3.Error):
            catalog.record_project(run.conn, project_dir, root_dir=repo_root, job_id=run.job.id)

    if outcome.code == "RENDER_CANCELLED":
        run.finish_as(
            JobState.CANCELLED,
            error={"code": "RENDER_CANCELLED", "message": "利用者の要求により中止しました"},
        )
        run.emit(
            phase="render",
            current=total,
            total=total,
            message="動画の生成を中止しました",
            severity=Severity.WARNING,
        )
        return

    if not outcome.ok:
        message = outcome.errors[0]["message"] if outcome.errors else "動画を生成できませんでした"
        run.finish_as(
            JobState.FAILED,
            error={
                "code": outcome.code or "RENDER_FAILED",
                "message": message,
                "errors": list(outcome.errors),
                "warnings": list(outcome.warnings),
            },
        )
        run.emit(
            phase="render",
            current=total,
            total=total,
            message=f"動画の生成に失敗しました({outcome.code})",
            severity=Severity.ERROR,
        )
        return

    run.finish_as(JobState.SUCCEEDED, result=_result_payload(outcome))
    run.emit(
        phase="done",
        current=total,
        total=total,
        message=f"動画を生成しました: {outcome.video_id}",
    )


BUILTIN_VIDEO_HANDLERS: dict[str, Any] = {VIDEO_JOB_KIND: render_video_job_handler}
#: `render_video` も `render` 区画を使う（Manim は全プロセス横断で単一実行）。
BUILTIN_VIDEO_RESOURCES: dict[str, tuple[ResourceKind, str | None]] = {
    VIDEO_JOB_KIND: (ResourceKind.RENDER, None)
}


__all__ = [
    "BUILTIN_VIDEO_HANDLERS",
    "BUILTIN_VIDEO_RESOURCES",
    "VIDEO_JOB_KIND",
    "render_video_job_handler",
    "run_pipeline",
]
