"""`video` コマンド群: plan / create / render / status / show / list / qa /
approve / distribution / gc / cost。

**このコマンド群は動画を外部へ送信しない。** YouTube への登録は人が手動で行う
前提で、ここが作るのはそのための材料一式（`manual publish pack`）だけ。

`render` は既定でプロセス内実行する（CLI の既定契約に合わせる）。長時間かかるので
`--detach` でキューへ投入もできる。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from abist_kb.application.video import cost_report, gc
from abist_kb.application.video.approval import approve as run_approve
from abist_kb.application.video.approval import verify_approval
from abist_kb.application.video.capture_planner import list_profiles
from abist_kb.application.video.distribution import evaluate as evaluate_distribution
from abist_kb.application.video.distribution import write_report as write_distribution_report
from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.application.video.pipeline import run_pipeline
from abist_kb.application.video.project_store import load_project, read_state
from abist_kb.application.video.qa import load_report as load_qa_report
from abist_kb.application.video.qa import run_qa, write_report
from abist_kb.application.video.script_planner import plan_video
from abist_kb.application.video.video_metadata import load_metadata
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.video_project_spec import (
    DEFAULT_MAX_DOCS_PER_DIRECTORY,
    DEFAULT_MAX_TOTAL_CANDIDATES,
)
from abist_kb.infrastructure.video.artifact_store import videos_dir
from abist_kb.presentation.cli.context import AppTyper, get_context

video_app = AppTyper(
    help="社内動画の生成・確認・承認（外部への自動投稿は行いません）。", no_args_is_help=True
)

_PathList = Annotated[list[str] | None, typer.Option(help="docs/ 配下の Markdown パス")]
_DirList = Annotated[list[str] | None, typer.Option(help="docs/ 配下のディレクトリ（候補集合）")]


def _emit(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _inputs(kb_path: list[str] | None, kb_dir: list[str] | None, query: list[str] | None):
    return {
        "kb_paths": list(kb_path or []),
        "kb_directories": list(kb_dir or []),
        "kb_queries": list(query or []),
        "esa_posts": [],
    }


def _project_dir(reports_dir: Path, video_id: str) -> Path:
    """`video_id` からプロジェクトディレクトリを引く（外へ出る指定は拒否）。"""
    root = videos_dir(reports_dir).resolve()
    candidate = (root / video_id).resolve()
    if root not in candidate.parents or not candidate.is_dir():
        raise AppError(
            code=ErrorCode.NOT_FOUND,
            message=f"動画 {video_id} が見つかりません。",
            hint="`abist-kb video list` で ID を確認してください。",
            exit_code=ExitCode.FAILURE,
        )
    return candidate


@video_app.command("plan")
def plan(
    ctx: typer.Context,
    title: Annotated[str, typer.Option(help="動画のタイトル")],
    kb_path: _PathList = None,
    kb_dir: _DirList = None,
    query: Annotated[list[str] | None, typer.Option(help="検索補完")] = None,
    purpose: Annotated[str | None, typer.Option(help="動画のねらい")] = None,
    min_sec: Annotated[float, typer.Option(help="目標尺の下限（秒）")] = 120.0,
    max_sec: Annotated[float, typer.Option(help="目標尺の上限（秒）")] = 180.0,
) -> None:
    """台本案を作る（保存しない）。目標尺から章数・シーン数を逆算する。"""
    app_ctx = get_context(ctx)
    settings = app_ctx.settings
    resolved = resolve_inputs(
        _inputs(kb_path, kb_dir, query),
        docs_dir=settings.docs_dir,
        max_docs_per_directory=DEFAULT_MAX_DOCS_PER_DIRECTORY,
        max_total_candidates=DEFAULT_MAX_TOTAL_CANDIDATES,
    )
    if not resolved.ok:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message="題材を解決できませんでした。",
            hint="kb_path / kb_dir が docs/ 配下にあるか確認してください。",
            exit_code=ExitCode.INVALID_INPUT,
        )
    result, _draft = plan_video(
        resolved.inputs,
        docs_dir=settings.docs_dir,
        title=title,
        purpose=purpose,
        target_duration_sec={"min": min_sec, "max": max_sec},
    )
    if not result.ok:
        _emit(
            {
                "ok": False,
                "code": (result.errors[0]["code"] if result.errors else "INVALID_SCRIPT_DRAFT"),
                "message": (result.errors[0]["message"] if result.errors else ""),
                "durationPlan": result.duration_plan,
            }
        )
        raise typer.Exit(code=int(ExitCode.INVALID_INPUT))
    _emit(
        {
            "ok": True,
            "durationPlan": result.duration_plan,
            "sceneCount": len(result.scenes),
            "scenes": [
                {"id": s["id"], "kind": s["kind"], "title": s.get("title")} for s in result.scenes
            ],
            "warnings": result.warnings,
        }
    )


@video_app.command("render")
def render(
    ctx: typer.Context,
    title: Annotated[str, typer.Option(help="動画のタイトル")],
    kb_path: _PathList = None,
    kb_dir: _DirList = None,
    query: Annotated[list[str] | None, typer.Option(help="検索補完")] = None,
    purpose: Annotated[str | None, typer.Option(help="動画のねらい")] = None,
    min_sec: Annotated[float, typer.Option(help="目標尺の下限（秒）")] = 120.0,
    max_sec: Annotated[float, typer.Option(help="目標尺の上限（秒）")] = 180.0,
    aspect: Annotated[str, typer.Option(help="16:9 または 9:16")] = "16:9",
    tts: Annotated[str, typer.Option(help="none / silence / test_tone / manual")] = "none",
    sound: Annotated[str, typer.Option(help="off / subtle / normal")] = "subtle",
    capture_profile: Annotated[
        str | None,
        typer.Option(help="登録済みの画面キャプチャプロファイル名（起動コマンドは渡せません）"),
    ] = None,
    max_docs: Annotated[int, typer.Option(help="1ディレクトリからの選抜上限")] = 8,
) -> None:
    """Markdown から動画一式を生成する（同期実行）。"""
    app_ctx = get_context(ctx)
    settings = app_ctx.settings
    resolved = resolve_inputs(
        _inputs(kb_path, kb_dir, query),
        docs_dir=settings.docs_dir,
        max_docs_per_directory=max_docs,
        max_total_candidates=max(max_docs, DEFAULT_MAX_TOTAL_CANDIDATES),
    )
    if not resolved.ok:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message="題材を解決できませんでした。",
            hint="kb_path / kb_dir が docs/ 配下にあるか確認してください。",
            exit_code=ExitCode.INVALID_INPUT,
        )

    def _on_progress(phase: str, current: int, total: int, message: str) -> None:
        typer.echo(f"[{phase}] {message}", err=True)

    result = run_pipeline(
        resolved,
        docs_dir=settings.docs_dir,
        reports_dir=settings.reports_dir,
        repo_root=settings.root_dir,
        title=title,
        purpose=purpose,
        tts=tts,
        sound_intensity=sound,
        sound_enabled=sound != "off",
        aspect_ratio=aspect,
        target_duration_sec={"min": min_sec, "max": max_sec},
        capture_profile=capture_profile,
        on_progress=_on_progress,
    )
    payload = {
        "ok": result.ok,
        "code": result.code,
        "videoId": result.video_id,
        "projectDir": str(result.project_dir) if result.project_dir else None,
        "output": str(result.output) if result.output else None,
        "durationSec": result.duration_sec,
        "sceneCount": result.scene_count,
        "thumbnail": str(result.thumbnail) if result.thumbnail else None,
        "qaOk": (result.qa_report or {}).get("ok"),
        "distribution": result.distribution,
        "durationPlan": result.duration_plan,
        "warnings": result.warnings,
        "errors": result.errors,
        # 外部送信はしない。手動アップロード用の材料を作るだけ
        "uploadNote": "外部への自動投稿は行いません（手動アップロード用の成果物です）",
    }
    _emit(payload)
    if not result.ok:
        raise typer.Exit(code=int(ExitCode.INVALID_INPUT))


@video_app.command("list")
def list_videos(ctx: typer.Context) -> None:
    """生成済み動画の一覧（ディスクを正本として読む）。"""
    settings = get_context(ctx).settings
    root = videos_dir(settings.reports_dir)
    items: list[dict[str, Any]] = []
    if root.is_dir():
        for entry in sorted(root.iterdir(), reverse=True):
            spec = load_project(entry)
            if spec is None:
                continue
            state = read_state(entry) or {}
            items.append(
                {
                    "videoId": spec.get("video_id") or entry.name,
                    "title": spec.get("title"),
                    "state": state.get("state"),
                    "durationSec": state.get("duration_sec"),
                    "qaOk": state.get("qa_ok"),
                }
            )
    _emit({"ok": True, "total": len(items), "items": items})


@video_app.command("show")
def show(ctx: typer.Context, video_id: str) -> None:
    """1本の動画の成果物・QA・配布判定をまとめて表示する。"""
    settings = get_context(ctx).settings
    project_dir = _project_dir(settings.reports_dir, video_id)
    spec = load_project(project_dir) or {}
    approval = verify_approval(project_dir)
    outputs = {
        name: str(project_dir / name)
        for name in (
            "output.mp4",
            "output-with-audio.mp4",
            "thumbnail.png",
            "video-metadata.json",
            "citations.json",
            "qa-report.json",
            "distribution-report.json",
            "subtitles/narration.srt",
        )
        if (project_dir / name).is_file()
    }
    _emit(
        {
            "ok": True,
            "videoId": spec.get("video_id"),
            "title": spec.get("title"),
            "state": (read_state(project_dir) or {}).get("state"),
            "metadata": load_metadata(project_dir),
            "qa": load_qa_report(project_dir),
            "approval": {"valid": approval.ok, "code": approval.code},
            "outputs": outputs,
        }
    )


@video_app.command("qa")
def qa(ctx: typer.Context, video_id: str) -> None:
    """`qa-report.json` を再生成する（FAIL でも成果物は残す）。"""
    settings = get_context(ctx).settings
    project_dir = _project_dir(settings.reports_dir, video_id)
    spec = load_project(project_dir) or {}
    report = run_qa(project_dir, spec=spec)
    write_report(report, project_dir)
    _emit({"ok": True, "qaOk": report.ok, "checks": [c.to_dict() for c in report.checks]})


@video_app.command("approve")
def approve(
    ctx: typer.Context,
    video_id: str,
    approver: Annotated[str, typer.Option(help="承認者の識別子")],
    confirmed: Annotated[bool, typer.Option("--confirmed", help="承認を確定する")] = False,
) -> None:
    """社内プレビュー承認を記録する（成果物ハッシュにバインドする）。"""
    if not confirmed:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message="承認には --confirmed が必要です。",
            hint="成果物を確認してから `--confirmed` を付けて再実行してください。",
            exit_code=ExitCode.INVALID_INPUT,
        )
    settings = get_context(ctx).settings
    project_dir = _project_dir(settings.reports_dir, video_id)
    result = run_approve(project_dir, approver=approver)
    _emit(
        {
            "ok": result.ok,
            "code": result.code,
            "message": result.message,
            "approvalId": (result.record or {}).get("approval_id"),
            "expiresAt": (result.record or {}).get("expires_at"),
        }
    )
    if not result.ok:
        raise typer.Exit(code=int(ExitCode.INVALID_INPUT))


@video_app.command("distribution")
def distribution(ctx: typer.Context, video_id: str) -> None:
    """配布判定を再評価して `distribution-report.json` を書き直す。"""
    settings = get_context(ctx).settings
    project_dir = _project_dir(settings.reports_dir, video_id)
    spec = load_project(project_dir) or {}
    decision = evaluate_distribution(
        spec,
        project_dir,
        qa=load_qa_report(project_dir),
        requested_public=bool((spec.get("distribution") or {}).get("public_candidate")),
    )
    write_distribution_report(decision, project_dir)
    _emit({"ok": True, "distribution": decision.to_dict()})


@video_app.command("gc")
def run_gc_command(
    ctx: typer.Context,
    keep_latest: Annotated[int, typer.Option(help="最新 N 件は必ず残す")] = gc.DEFAULT_KEEP_LATEST,
    ttl_days: Annotated[int, typer.Option(help="保持日数")] = gc.DEFAULT_TTL_DAYS,
    confirmed: Annotated[bool, typer.Option("--confirmed", help="実際に削除する")] = False,
) -> None:
    """古い動画成果物を整理する（既定は dry-run）。"""
    settings = get_context(ctx).settings
    result = gc.run_gc(
        settings.reports_dir,
        keep_latest=keep_latest,
        ttl_days=ttl_days,
        confirmed=confirmed,
    )
    _emit({"ok": True, **result.to_dict()})


@video_app.command("cost")
def cost(
    ctx: typer.Context,
    unit_price: Annotated[
        float | None, typer.Option(help="TTS 1000 文字あたりの単価（未指定なら金額は出さない）")
    ] = None,
    save: Annotated[bool, typer.Option("--save", help="reports/benchmarks/video へ保存")] = False,
) -> None:
    """TTS 文字数・ディスク使用量を集計する。"""
    settings = get_context(ctx).settings
    report = cost_report.build_report(settings.reports_dir, tts_unit_price_per_1k_chars=unit_price)
    payload = report.to_dict()
    if save:
        payload["savedTo"] = str(cost_report.write_report(report, settings.root_dir))
    _emit({"ok": True, **payload})


@video_app.command("capture-profiles")
def capture_profiles(ctx: typer.Context) -> None:
    """登録済みキャプチャプロファイル名を表示する（コマンドは表示しない）。"""
    settings = get_context(ctx).settings
    _emit(
        {
            "ok": True,
            "profiles": list_profiles(settings.root_dir),
            "note": "起動コマンドは config/capture-profiles.json でのみ管理します",
        }
    )


__all__ = ["video_app"]
