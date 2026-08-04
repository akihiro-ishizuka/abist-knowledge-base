"""`jobs` CLI コマンド群の受入テスト。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from abist_kb.domain.errors import ErrorCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema
from abist_kb.presentation.cli.app import app

runner = CliRunner()


def _root_args(tmp_root, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


def test_jobs_submit_noop_runs_inline_and_succeeds(tmp_root):
    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "jobs", "submit", "noop"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state"] == "succeeded"
    assert payload["kind"] == "noop"


def test_jobs_submit_unknown_kind_is_invalid_input(tmp_root):
    result = runner.invoke(app, _root_args(tmp_root, "jobs", "submit", "unknown-kind"))
    assert result.exit_code == 2
    assert ErrorCode.INVALID_INPUT.value in result.output


def test_jobs_submit_detach_without_worker_fails_worker_unavailable(tmp_root):
    result = runner.invoke(app, _root_args(tmp_root, "jobs", "submit", "noop", "--detach"))
    assert result.exit_code != 0
    assert "WORKER_UNAVAILABLE" in result.output


def test_jobs_submit_detach_with_live_worker_enqueues_without_running(tmp_root):
    settings_db = tmp_root / "data" / "app.sqlite"
    settings_db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings_db)
    ensure_jobs_schema(conn)
    leases.try_acquire_worker_lease(conn, "live-worker", ttl_seconds=30.0)
    conn.close()

    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "jobs", "submit", "noop", "--detach")
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state"] == "queued"


def test_jobs_list_shows_submitted_jobs(tmp_root):
    runner.invoke(app, _root_args(tmp_root, "jobs", "submit", "noop"))
    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "jobs", "list"))
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["jobs"]) == 1
    assert payload["jobs"][0]["kind"] == "noop"


def test_jobs_show_includes_progress_history(tmp_root):
    submit_result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "jobs", "submit", "noop")
    )
    job_id = json.loads(submit_result.stdout)["id"]
    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "jobs", "show", job_id))
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["id"] == job_id
    assert len(payload["history"]) == 1


def test_jobs_cancel_queued_job(tmp_root):
    settings_db = tmp_root / "data" / "app.sqlite"
    settings_db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings_db)
    ensure_jobs_schema(conn)
    from abist_kb.infrastructure.jobs.repository import JobRepository

    job = JobRepository(conn).submit("noop")
    conn.close()

    result = runner.invoke(app, _root_args(tmp_root, "jobs", "cancel", job.id))
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, _root_args(tmp_root, "--output", "json", "jobs", "show", job.id))
    payload = json.loads(result.stdout)
    assert payload["state"] == "cancelled"


def test_jobs_retry_requires_confirmation_and_fails_non_interactively_without_yes(tmp_root):
    settings_db = tmp_root / "data" / "app.sqlite"
    settings_db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings_db)
    ensure_jobs_schema(conn)
    from abist_kb.domain.job import JobState
    from abist_kb.infrastructure.jobs.repository import JobRepository

    repo = JobRepository(conn)
    job = repo.submit("noop")
    repo.claim("dead-owner", ttl_seconds=5.0)
    repo.finish(job.id, state=JobState.FAILED, error={"code": "FAILURE", "message": "boom"})
    conn.close()

    result = runner.invoke(app, _root_args(tmp_root, "jobs", "retry", job.id))
    assert result.exit_code != 0


def test_jobs_retry_with_yes_creates_new_job(tmp_root):
    settings_db = tmp_root / "data" / "app.sqlite"
    settings_db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings_db)
    ensure_jobs_schema(conn)
    from abist_kb.domain.job import JobState
    from abist_kb.infrastructure.jobs.repository import JobRepository

    repo = JobRepository(conn)
    job = repo.submit("noop")
    repo.claim("dead-owner", ttl_seconds=5.0)
    repo.finish(job.id, state=JobState.FAILED, error={"code": "FAILURE", "message": "boom"})
    conn.close()

    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "--yes", "jobs", "retry", job.id)
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["retry_of"] == job.id
    assert payload["state"] == "queued"
