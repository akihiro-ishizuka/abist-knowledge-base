"""可視化レンダリングの永続ジョブ化(設計書 §10: `render_scene` をジョブ種別として実行する)。

**リースの責任分担(固定)**: `infrastructure/jobs/execution.run_job` は
`BUILTIN_RENDER_RESOURCES` の登録内容から `ResourceKind.RENDER` のリソース
リースを、ハンドラを呼ぶ**前**に自動取得・自動更新・(with を抜ける際に)解放
する(§10.2)。このハンドラ自身がリースを再取得してはならない — 自分自身が
既に保持しているリースと競合し、デッドロックまたは二重解放の温床になる。
同期 MCP `render_scene`(`presentation/mcp/kb_visualize.py`)はジョブ基盤を
経由しないため、従来どおり明示的にリースを取得し続ける(責任は重複させない)。

`kb_visualize.py` と共有するのは `application.visualization.renderer.render_scene`
の呼び出しと `RenderOutcome` → エラー形の変換だけで、リース制御は共有しない。

ジョブ `params` には投入側(`presentation/common/actions.
visualization_submit_render`/`presentation/mcp/jobs_tools.start_render_scene`)が
解決済みの `docs_dir`/`reports_dir`/`repo_root` を文字列として含める。ハンドラ
自身は `Settings` を再解決しない — `WorkerSupervisor`(`worker run`)は別プロセス
で動く想定であり、ジョブ行に必要な情報を持たせるのが唯一の確実な経路のため。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from abist_kb.application.visualization.renderer import RenderOutcome, render_scene
from abist_kb.domain.job import JobState, ResourceKind, Severity
from abist_kb.infrastructure.jobs.supervisor import JobRunContext

RENDER_JOB_KIND = "render_scene"


def _outcome_to_error(outcome: RenderOutcome) -> dict[str, Any]:
    message = outcome.errors[0]["message"] if outcome.errors else "レンダリングに失敗しました。"
    return {
        "code": outcome.code or "RENDER_FAILED",
        "message": message,
        "errors": list(outcome.errors),
        "warnings": list(outcome.warnings),
    }


def _outcome_to_result(outcome: RenderOutcome) -> dict[str, Any]:
    return {
        "visualization_id": outcome.visualization_id,
        "output_dir": str(outcome.output_dir) if outcome.output_dir else None,
        "outputs": list(outcome.outputs),
        "manifest_path": str(outcome.manifest_path) if outcome.manifest_path else None,
        "warnings": list(outcome.warnings),
        "duration_ms": outcome.duration_ms,
    }


def render_scene_job_handler(run: JobRunContext) -> None:
    """`render_scene` ジョブ種別のハンドラ。

    `run.job.params` は投入側が組み立てた次のキーを持つ前提:
    `scene_spec`(dict)、`docs_dir`/`reports_dir`/`repo_root`(str)、
    任意で `slug`(str)。

    レンダリングは単発の不可分な操作(サブプロセス1回)であり、複数回の副作用を
    繰り返すループではないため `run.check_lease()` の協調的ポーリングは不要
    (`infrastructure.jobs.supervisor.JobRunContext` docstring参照: 一度きりの
    操作は `run_job` の事後検知バックストップで十分)。
    """
    params = run.job.params
    spec = params["scene_spec"]
    docs_dir = Path(params["docs_dir"])
    reports_dir = Path(params["reports_dir"])
    repo_root = Path(params["repo_root"])
    slug = params.get("slug")

    run.emit(phase="render", current=0, total=1, message="レンダリングを開始しました")

    outcome = render_scene(
        spec,
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        repo_root=repo_root,
        slug=slug,
    )

    if not outcome.ok:
        error = _outcome_to_error(outcome)
        run.finish_as(JobState.FAILED, error=error)
        run.emit(
            phase="render",
            current=1,
            total=1,
            message=f"レンダリングに失敗しました({error['code']})",
            severity=Severity.ERROR,
        )
        return

    result = _outcome_to_result(outcome)
    run.finish_as(JobState.SUCCEEDED, result=result)
    run.emit(
        phase="render",
        current=1,
        total=1,
        message=f"レンダリングが完了しました: {result['visualization_id']}",
    )


BUILTIN_RENDER_HANDLERS: dict[str, Any] = {RENDER_JOB_KIND: render_scene_job_handler}
#: `JobService(resource_for_kind=...)`/`WorkerSupervisor(resource_for_kind=...)` へ
#: そのまま渡せる既定リソース要求。`key=None`: `render` は全プロセス横断で単一区画
#: (`kb_visualize.py` の `CONCURRENT_RENDER` 互換の単一実行制限と同じ区画)。
BUILTIN_RENDER_RESOURCES: dict[str, tuple[ResourceKind, str | None]] = {
    RENDER_JOB_KIND: (ResourceKind.RENDER, None)
}


__all__ = [
    "BUILTIN_RENDER_HANDLERS",
    "BUILTIN_RENDER_RESOURCES",
    "RENDER_JOB_KIND",
    "render_scene_job_handler",
]
