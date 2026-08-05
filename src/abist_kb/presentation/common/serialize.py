"""`Job`/`ProgressEvent`/`AppError` を JSON 互換の dict へ変換する共通ヘルパー。

SSE ペイロード、FastAPI レスポンス、MCP ツール応答はすべてこの形を経由する。
CLI(`presentation/cli/jobs_cmd.py::_job_to_dict`)と同じ列を返すことで、
同一ジョブが API/CLI/MCP で同じ状態・件数・エラーコードを表示する
という §15 の受入条件を満たす。
"""

from __future__ import annotations

from typing import Any

from abist_kb.domain.errors import AppError
from abist_kb.domain.job import Job, JobState, ProgressEvent
from abist_kb.presentation.console.theme import SemanticToken

_JOB_STATE_TOKENS: dict[JobState, SemanticToken] = {
    JobState.QUEUED: SemanticToken.MUTED,
    JobState.RUNNING: SemanticToken.INFO,
    JobState.SUCCEEDED: SemanticToken.SUCCESS,
    JobState.PARTIAL: SemanticToken.WARNING,
    JobState.FAILED: SemanticToken.DANGER,
    JobState.CANCELLED: SemanticToken.MUTED,
    JobState.INTERRUPTED: SemanticToken.WARNING,
}


def job_to_dict(job: Job) -> dict[str, Any]:
    token = _JOB_STATE_TOKENS.get(job.state, SemanticToken.MUTED)
    return {
        "id": job.id,
        "kind": job.kind,
        "state": str(job.state),
        "state_token": token.value,
        "params": job.params,
        "result": job.result,
        "error": job.error,
        "progress": job.progress,
        "cancel_requested": job.cancel_requested,
        "retry_of": job.retry_of,
        "owner_id": job.owner_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def event_to_dict(event: ProgressEvent) -> dict[str, Any]:
    return {
        "job_id": event.job_id,
        "phase": event.phase,
        "current": event.current,
        "total": event.total,
        "message": event.message,
        "severity": str(event.severity),
        "item": event.item,
        "timestamp": event.timestamp.isoformat(),
    }


def error_to_dict(err: AppError) -> dict[str, Any]:
    payload = err.to_dict()
    payload["exit_code"] = int(err.exit_code)
    return payload


__all__ = ["error_to_dict", "event_to_dict", "job_to_dict"]
