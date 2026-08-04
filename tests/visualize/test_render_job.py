"""`application.visualization.render_job.render_scene_job_handler` の単体テスト
(Task 6 fix round 1)。

`render_scene`(レンダラー本体)を直接モックし、`RenderOutcome` → `finish_as()`
の変換だけを検証する。実 Manim/ffmpeg は一切呼ばず、`JobRunContext`/`Job` も
DBに触れず直接構築する — このハンドラは `run.check_lease()` を呼ばない設計
(単発の不可分操作のため)であり、リースの取得・解放はテスト対象外
(`infrastructure/jobs/execution.py` が別途担う)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from abist_kb.application.visualization import render_job
from abist_kb.application.visualization.renderer import RenderOutcome
from abist_kb.domain.job import Job, JobState, Severity
from abist_kb.infrastructure.jobs.supervisor import JobRunContext


def _make_job(**param_overrides: Any) -> Job:
    from datetime import UTC, datetime

    params = {
        "scene_spec": {"title": "テストシーン"},
        "docs_dir": "docs",
        "reports_dir": "reports/visualizations",
        "repo_root": ".",
    }
    params.update(param_overrides)
    return Job(
        id="job-1",
        kind=render_job.RENDER_JOB_KIND,
        state=JobState.RUNNING,
        params=params,
        created_at=datetime.now(UTC),
    )


class _EmitRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def emit() -> _EmitRecorder:
    return _EmitRecorder()


def test_success_outcome_finishes_succeeded_and_persists_result(
    monkeypatch: pytest.MonkeyPatch, emit: _EmitRecorder
) -> None:
    outcome = RenderOutcome(
        ok=True,
        visualization_id="viz-123",
        output_dir=Path("/tmp/out"),
        outputs=[{"path": "scene.mp4", "kind": "video"}],
        manifest_path=Path("/tmp/out/manifest.json"),
        warnings=["ffmpeg のバージョンが古い可能性があります"],
        duration_ms=1234.5,
    )
    captured_kwargs: dict[str, Any] = {}

    def _fake_render_scene(spec: dict[str, Any], **kwargs: Any) -> RenderOutcome:
        captured_kwargs["spec"] = spec
        captured_kwargs.update(kwargs)
        return outcome

    monkeypatch.setattr(render_job, "render_scene", _fake_render_scene)

    job = _make_job(slug="my-slug")
    run = JobRunContext(job=job, emit=emit)

    render_job.render_scene_job_handler(run)

    # render_scene には投入側が解決済みのパス・spec がそのまま渡る。
    assert captured_kwargs["spec"] == job.params["scene_spec"]
    assert captured_kwargs["docs_dir"] == Path("docs")
    assert captured_kwargs["reports_dir"] == Path("reports/visualizations")
    assert captured_kwargs["repo_root"] == Path()
    assert captured_kwargs["slug"] == "my-slug"

    # finish_as(SUCCEEDED, result=...) が呼ばれ、結果ペイロードが永続化対象になる。
    assert run._finish_state == JobState.SUCCEEDED
    assert run._finish_error is None
    assert run._finish_result == {
        "visualization_id": "viz-123",
        "output_dir": str(Path("/tmp/out")),
        "outputs": [{"path": "scene.mp4", "kind": "video"}],
        "manifest_path": str(Path("/tmp/out/manifest.json")),
        "warnings": ["ffmpeg のバージョンが古い可能性があります"],
        "duration_ms": 1234.5,
    }

    # 進捗イベントも開始・完了の2回発火し、完了メッセージに visualization_id を含む。
    assert len(emit.calls) == 2
    assert emit.calls[0]["phase"] == "render"
    assert emit.calls[0]["current"] == 0
    assert emit.calls[1]["current"] == 1
    assert "viz-123" in emit.calls[1]["message"]


def test_failure_outcome_finishes_failed_with_error_code_and_message(
    monkeypatch: pytest.MonkeyPatch, emit: _EmitRecorder
) -> None:
    outcome = RenderOutcome(
        ok=False,
        code="SOURCE_HASH_MISMATCH",
        errors=[{"path": "sources[0]", "message": "content_hash が一致しません"}],
        warnings=[],
    )
    monkeypatch.setattr(render_job, "render_scene", lambda spec, **kwargs: outcome)

    job = _make_job()
    run = JobRunContext(job=job, emit=emit)

    render_job.render_scene_job_handler(run)

    assert run._finish_state == JobState.FAILED
    assert run._finish_result is None
    assert run._finish_error == {
        "code": "SOURCE_HASH_MISMATCH",
        "message": "content_hash が一致しません",
        "errors": [{"path": "sources[0]", "message": "content_hash が一致しません"}],
        "warnings": [],
    }

    assert len(emit.calls) == 2
    assert emit.calls[1]["severity"] == Severity.ERROR
    assert "SOURCE_HASH_MISMATCH" in emit.calls[1]["message"]


def test_failure_outcome_without_code_falls_back_to_render_failed(
    monkeypatch: pytest.MonkeyPatch, emit: _EmitRecorder
) -> None:
    """`RenderOutcome.code` が未設定(例: 想定外の内部失敗)でも `RENDER_FAILED` に
    フォールバックし、`errors` が空でも汎用メッセージを出す(投入元に必ず何かを
    返す契約を守る)。
    """
    outcome = RenderOutcome(ok=False)
    monkeypatch.setattr(render_job, "render_scene", lambda spec, **kwargs: outcome)

    job = _make_job()
    run = JobRunContext(job=job, emit=emit)

    render_job.render_scene_job_handler(run)

    assert run._finish_state == JobState.FAILED
    assert run._finish_error is not None
    assert run._finish_error["code"] == "RENDER_FAILED"
    assert run._finish_error["message"] == "レンダリングに失敗しました。"
