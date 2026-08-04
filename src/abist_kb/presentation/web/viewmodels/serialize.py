"""`Job`/`ProgressEvent`/`AppError` を JSON 互換の dict へ変換する共通ヘルパー。

SSE ペイロード、FastAPI レスポンス、NiceGUI の描画データはすべてこの形を
経由する。CLI(`presentation/cli/jobs_cmd.py::_job_to_dict`)と同じ列を返す
ことで、同一ジョブが Web/CLI/MCP で同じ状態・件数・エラーコードを表示する
という §15 の受入条件を満たす。
"""

from __future__ import annotations

from typing import Any

from abist_kb.domain.errors import AppError
from abist_kb.domain.job import Job, ProgressEvent

from .tokens import job_state_token


def job_to_dict(job: Job) -> dict[str, Any]:
    token = job_state_token(job.state)
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
