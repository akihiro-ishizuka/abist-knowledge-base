"""組み込みジョブ種別を `worker run --once` が終端状態まで処理できることの受入テスト。

Phase 0: MCP `start_*` が投入する `kb_download_*` と UI の `batch` を
`infrastructure.jobs.builtin_registry` 経由で実行できるようにする。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from abist_kb.application.batch_service import BatchService
from abist_kb.application.job_service import JobService
from abist_kb.application.sync_service import new_sync_summary
from abist_kb.config import Settings
from abist_kb.domain.job import TERMINAL_STATES, JobState, ResourceKind
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.infrastructure.jobs.builtin_registry import (
    MCP_DOWNLOAD_JOB_KINDS,
    build_builtin_handlers,
    build_builtin_resources,
)
from abist_kb.presentation.cli.app import app

runner = CliRunner()

_EXPECTED_KINDS = frozenset(
    {
        "noop",
        "batch",
        "kb_download_batch",
        "kb_download_esa_post",
        "kb_download_esa_category",
        "kb_download_esa_search",
        "kb_download_web",
        "render_scene",
        # 動画レンダリング（video Phase 10）。`render` 区画を render_scene と共有する。
        "render_video",
    }
)


def _root_args(tmp_root: Path, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


def _settings(tmp_root: Path) -> Settings:
    settings = Settings(root_dir=tmp_root, _env_file=None)
    settings.ensure_directories()
    return settings


def _submit_and_run_once(
    tmp_root: Path, kind: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """キュー投入(`submit`) → `worker run --once` → `jobs show` の JSON。

    `detach` は生きた worker が必要だが、CLI の `--once` は終了後も別 owner の
    リースを残すため、2回目の `--once` がリーダーになれない。ここでは
    `JobService.submit`(WORKER_UNAVAILABLE 検査なし)で投入し、1回の
    `--once` がリーダー選出とジョブ消費をまとめて行う。
    """
    settings = _settings(tmp_root)
    conn = open_app_db(settings.app_db_path)
    try:
        handlers = build_builtin_handlers(settings=settings, conn=conn)
        service = JobService(
            conn,
            owner_id=str(uuid.uuid4()),
            handlers=handlers,
            resource_for_kind=build_builtin_resources(),
        )
        job = service.submit(kind, params or {})
        job_id = job.id
        assert job.state is JobState.QUEUED
    finally:
        conn.close()

    run = runner.invoke(app, _root_args(tmp_root, "worker", "run", "--once"))
    assert run.exit_code == 0, run.output

    show = runner.invoke(app, _root_args(tmp_root, "--output", "json", "jobs", "show", job_id))
    assert show.exit_code == 0, show.output
    return json.loads(show.stdout)


def test_build_builtin_handlers_registers_all_mcp_and_ui_kinds(tmp_root: Path) -> None:
    settings = _settings(tmp_root)
    conn = open_app_db(settings.app_db_path)
    try:
        handlers = build_builtin_handlers(settings=settings, conn=conn)
        resources = build_builtin_resources()
    finally:
        conn.close()

    assert set(handlers) == _EXPECTED_KINDS
    assert handlers["batch"] is handlers["kb_download_batch"]
    for kind in MCP_DOWNLOAD_JOB_KINDS:
        assert kind in handlers
        assert resources[kind] == (ResourceKind.DOCS_WRITE, None)
    assert resources["batch"] == (ResourceKind.DOCS_WRITE, None)
    assert resources["render_scene"][0] == ResourceKind.RENDER


def test_worker_once_completes_noop_to_terminal(tmp_root: Path) -> None:
    payload = _submit_and_run_once(tmp_root, "noop")
    assert JobState(payload["state"]) in TERMINAL_STATES
    assert payload["state"] == "succeeded"


def test_worker_once_completes_batch_by_batch_id(tmp_root: Path) -> None:
    settings = _settings(tmp_root)
    conn = open_app_db(settings.app_db_path)
    try:
        batch = BatchService(conn, settings=settings).add(
            name="empty-web", type="web", output_dir="docs/empty-web", items=[]
        )
        batch_id = batch["id"]
    finally:
        conn.close()

    payload = _submit_and_run_once(tmp_root, "batch", {"batch_id": batch_id})
    assert JobState(payload["state"]) in TERMINAL_STATES
    assert payload["state"] == "succeeded"
    assert payload["kind"] == "batch"


def test_worker_once_completes_kb_download_batch_by_name(tmp_root: Path) -> None:
    settings = _settings(tmp_root)
    conn = open_app_db(settings.app_db_path)
    try:
        BatchService(conn, settings=settings).add(
            name="mcp-empty-web", type="web", output_dir="docs/mcp-empty", items=[]
        )
    finally:
        conn.close()

    payload = _submit_and_run_once(tmp_root, "kb_download_batch", {"batch": "mcp-empty-web"})
    assert JobState(payload["state"]) in TERMINAL_STATES
    assert payload["state"] == "succeeded"
    assert payload["kind"] == "kb_download_batch"


def test_worker_once_completes_render_scene_invalid_spec_to_failed(tmp_root: Path) -> None:
    """不正 SceneSpec はネットワーク不要で FAILED(終端)になる。"""
    settings = _settings(tmp_root)
    params = {
        "scene_spec": {"kind": "not-a-real-kind"},
        "docs_dir": str(settings.docs_dir),
        "reports_dir": str(settings.reports_dir / "visualizations"),
        "repo_root": str(settings.root_dir),
    }
    payload = _submit_and_run_once(tmp_root, "render_scene", params)
    assert JobState(payload["state"]) in TERMINAL_STATES
    assert payload["state"] == "failed"
    assert payload["error"] is not None


def test_network_kind_handlers_reach_succeeded_when_sync_mocked(tmp_root: Path) -> None:
    """esa/web/git 種別が登録され、SyncService をモックすれば終端 SUCCEEDED になる。"""
    settings = _settings(tmp_root)
    summary = new_sync_summary("web")
    summary.full_sync_succeeded = True
    summary.finished_at = summary.started_at

    conn = open_app_db(settings.app_db_path)
    try:
        handlers = build_builtin_handlers(settings=settings, conn=conn)
        for kind in (
            "kb_download_esa_post",
            "kb_download_esa_category",
            "kb_download_esa_search",
            "kb_download_web",
        ):
            assert kind in handlers
            assert build_builtin_resources()[kind] == (ResourceKind.DOCS_WRITE, None)

        service = JobService(
            conn,
            owner_id=str(uuid.uuid4()),
            handlers=handlers,
            resource_for_kind=build_builtin_resources(),
        )
        job = service.submit(
            "kb_download_web",
            {
                "url": "https://example.invalid/",
                "outputDir": "docs/example",
                "maxDepth": 1,
                "delay": 0,
                "concurrency": 1,
            },
        )
        job_id = job.id
    finally:
        conn.close()

    with patch(
        "abist_kb.infrastructure.jobs.builtin_registry.SyncService._sync_web_target",
        return_value=(summary, None),
    ):
        run = runner.invoke(app, _root_args(tmp_root, "worker", "run", "--once"))
        assert run.exit_code == 0, run.output

    show = runner.invoke(app, _root_args(tmp_root, "--output", "json", "jobs", "show", job_id))
    assert show.exit_code == 0, show.output
    payload = json.loads(show.stdout)
    assert JobState(payload["state"]) in TERMINAL_STATES
    assert payload["state"] == "succeeded"


def test_batch_and_kb_download_batch_share_handler_identity(tmp_root: Path) -> None:
    settings = _settings(tmp_root)
    conn = open_app_db(settings.app_db_path)
    try:
        handlers = build_builtin_handlers(settings=settings, conn=conn)
        assert handlers["batch"] is handlers["kb_download_batch"]
    finally:
        conn.close()
