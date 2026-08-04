"""M5 task-4: ジョブ指向の新規 MCP ツール(`start_*`/`job_status`/`cancel_job`/
`get_batch`/`list_corpora`/`system_status`)のテスト。

旧実装に前例が無いため fixture 契約はない。ここでは brief の2契約
(`start_*` は生きた worker が居なければ WORKER_UNAVAILABLE、`job_status` は
PARTIAL を成功へ丸めない)と、本タスクで新規設計した応答形が安定していることを
検証する。ネットワーク・旧リポジトリには一切触れない。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.application.batch_service import BatchService
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.mcp.jobs_tools import JobTools


def _payload(result: object) -> dict:
    return json.loads(result.content[0].text)  # type: ignore[attr-defined]


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    return tmp_path


def _make_tools(tmp_root: Path):
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    tools = JobTools(
        conn,
        docs_dir=tmp_root / "docs",
        app_db_path=tmp_root / "app.sqlite",
        work_index_path=tmp_root / "data" / "work-index.sqlite",
        reference_index_path=tmp_root / "data" / "reference-index.sqlite",
    )
    return tools, conn


def _mark_live_worker(conn) -> None:
    leases.try_acquire_worker_lease(conn, "test-worker", ttl_seconds=300)


# ---------------------------------------------------------------------------
# start_* : WORKER_UNAVAILABLE の即座失敗(brief契約)
# ---------------------------------------------------------------------------


def test_start_run_batch_fails_fast_without_live_worker(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    BatchService(conn).add(name="バッチA", type="web", output_dir="docs/a", items=[])

    result = tools.start_run_batch({"batch": "バッチA"})
    payload = _payload(result)

    assert result.isError
    assert payload["ok"] is False
    assert payload["code"] == "WORKER_UNAVAILABLE"


def test_start_run_batch_unknown_batch_is_not_found(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    _mark_live_worker(conn)

    result = tools.start_run_batch({"batch": "存在しない"})
    payload = _payload(result)

    assert result.isError
    assert payload["ok"] is False
    assert payload["code"] == "NOT_FOUND"


def test_start_run_batch_queues_job_when_worker_is_live(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    BatchService(conn).add(name="バッチB", type="web", output_dir="docs/b", items=[])
    _mark_live_worker(conn)

    result = tools.start_run_batch({"batch": "バッチB"})
    payload = _payload(result)

    assert not result.isError
    assert payload == {
        "ok": True,
        "job_id": payload["job_id"],
        "kind": "kb_download_batch",
        "state": "queued",
    }
    assert payload["job_id"]

    job = JobRepository(conn).get(payload["job_id"])
    assert job is not None
    assert job.params == {"batch": "バッチB"}


@pytest.mark.parametrize(
    ("method", "arguments", "expected_kind"),
    [
        ("start_download_esa_post", {"post": 42}, "kb_download_esa_post"),
        (
            "start_download_esa_category",
            {"category": "テスト/カテゴリ"},
            "kb_download_esa_category",
        ),
        ("start_download_esa_search", {"query": "テスト"}, "kb_download_esa_search"),
        ("start_download_web", {"url": "https://example.com"}, "kb_download_web"),
        ("start_download_git", {"repository": "https://example.com/repo.git"}, "kb_download_git"),
    ],
)
def test_start_download_tools_queue_when_worker_is_live(
    tmp_root: Path, method: str, arguments: dict, expected_kind: str
) -> None:
    tools, conn = _make_tools(tmp_root)
    _mark_live_worker(conn)

    result = getattr(tools, method)(arguments)
    payload = _payload(result)

    assert not result.isError
    assert payload["ok"] is True
    assert payload["kind"] == expected_kind
    assert payload["state"] == "queued"


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("start_download_esa_post", {"post": 42}),
        ("start_download_esa_category", {"category": "テスト"}),
        ("start_download_esa_search", {"query": "テスト"}),
        ("start_download_web", {"url": "https://example.com"}),
        ("start_download_git", {"repository": "https://example.com/repo.git"}),
    ],
)
def test_start_download_tools_fail_without_live_worker(
    tmp_root: Path, method: str, arguments: dict
) -> None:
    tools, _conn = _make_tools(tmp_root)

    result = getattr(tools, method)(arguments)
    payload = _payload(result)

    assert result.isError
    assert payload["code"] == "WORKER_UNAVAILABLE"


_VALID_SCENE_SPEC = {
    "schema_version": "1.0",
    "scene_kind": "explain",
    "output_format": "mp4",
    "template": "step_explanation",
    "title": "テスト用シーン",
    "sources": [],
    "beats": [{"type": "statement", "text": "装飾テキスト", "decorative": True}],
}


def test_start_render_scene_fails_fast_without_live_worker(tmp_root: Path) -> None:
    tools, _conn = _make_tools(tmp_root)

    result = tools.start_render_scene({"sceneSpec": _VALID_SCENE_SPEC})
    payload = _payload(result)

    assert result.isError
    assert payload["ok"] is False
    assert payload["code"] == "WORKER_UNAVAILABLE"


def test_start_render_scene_queues_job_when_worker_is_live(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    _mark_live_worker(conn)

    result = tools.start_render_scene({"sceneSpec": _VALID_SCENE_SPEC, "slug": "my-slug"})
    payload = _payload(result)

    assert not result.isError
    assert payload["ok"] is True
    assert payload["kind"] == "render_scene"
    assert payload["state"] == "queued"

    job = JobRepository(conn).get(payload["job_id"])
    assert job is not None
    assert job.params["scene_spec"] == _VALID_SCENE_SPEC
    assert job.params["slug"] == "my-slug"
    assert job.params["docs_dir"]
    assert job.params["reports_dir"].endswith("visualizations")
    assert job.params["repo_root"]


def test_start_render_scene_accepts_scene_spec_as_json_string(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    _mark_live_worker(conn)

    result = tools.start_render_scene({"sceneSpec": json.dumps(_VALID_SCENE_SPEC)})
    payload = _payload(result)

    assert not result.isError
    assert payload["ok"] is True


def test_start_render_scene_rejects_invalid_json_string(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    _mark_live_worker(conn)

    result = tools.start_render_scene({"sceneSpec": "{not json"})
    payload = _payload(result)

    assert result.isError
    assert payload["ok"] is False
    assert payload["code"] == "INVALID_SCENE_SPEC"


# ---------------------------------------------------------------------------
# job_status: PARTIAL を成功へ丸めない(M3 の契約をこの消費者でも守る)
# ---------------------------------------------------------------------------


def test_job_status_reports_partial_distinct_from_succeeded(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    repo = JobRepository(conn)
    job = repo.submit("kb_download_batch", {"batch": "テスト"})
    repo.finish(
        job.id,
        state=JobState.PARTIAL,
        result={"totals": {"added": 1, "error": 1}},
    )

    result = tools.job_status({"job_id": job.id})
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["state"] == "partial"
    assert payload["state"] != "succeeded"


def test_job_status_succeeded_is_reported_as_succeeded(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    repo = JobRepository(conn)
    job = repo.submit("kb_download_batch", {"batch": "テスト"})
    repo.finish(job.id, state=JobState.SUCCEEDED, result={"totals": {"added": 1}})

    result = tools.job_status({"job_id": job.id})
    payload = _payload(result)

    assert payload["state"] == "succeeded"


def test_job_status_unknown_job_id_is_not_found(tmp_root: Path) -> None:
    tools, _conn = _make_tools(tmp_root)

    result = tools.job_status({"job_id": "does-not-exist"})
    payload = _payload(result)

    assert result.isError
    assert payload["ok"] is False
    assert payload["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------


def test_cancel_job_queued_becomes_cancelled(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    repo = JobRepository(conn)
    job = repo.submit("kb_download_batch", {"batch": "テスト"})

    result = tools.cancel_job({"job_id": job.id})
    payload = _payload(result)

    assert not result.isError
    assert payload["ok"] is True
    assert payload["state"] == "cancelled"


def test_cancel_job_already_finished_is_error(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    repo = JobRepository(conn)
    job = repo.submit("kb_download_batch", {"batch": "テスト"})
    repo.finish(job.id, state=JobState.SUCCEEDED, result={})

    result = tools.cancel_job({"job_id": job.id})
    payload = _payload(result)

    assert result.isError
    assert payload["ok"] is False


def test_cancel_job_unknown_job_id_is_not_found(tmp_root: Path) -> None:
    tools, _conn = _make_tools(tmp_root)

    result = tools.cancel_job({"job_id": "does-not-exist"})
    payload = _payload(result)

    assert result.isError
    assert payload["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# get_batch
# ---------------------------------------------------------------------------


def test_get_batch_returns_camel_cased_entry(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    BatchService(conn).add(
        name="Webバッチ",
        type="web",
        output_dir="docs/webバッチ",
        items=[{"options": {"url": "https://example.com", "max_depth": 3, "delay": 1000}}],
    )

    result = tools.get_batch({"batch": "Webバッチ"})
    payload = _payload(result)

    assert not result.isError
    assert payload["ok"] is True
    assert payload["batch"]["name"] == "Webバッチ"
    assert payload["batch"]["type"] == "web"
    assert payload["batch"]["url"] == "https://example.com"


def test_get_batch_unknown_name_is_not_found(tmp_root: Path) -> None:
    tools, _conn = _make_tools(tmp_root)

    result = tools.get_batch({"batch": "存在しない"})
    payload = _payload(result)

    assert result.isError
    assert payload["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# list_corpora / system_status
# ---------------------------------------------------------------------------


def test_list_corpora_reports_both_corpora_as_unavailable_when_no_index(tmp_root: Path) -> None:
    tools, _conn = _make_tools(tmp_root)

    result = tools.list_corpora({})
    payload = _payload(result)

    assert payload["ok"] is True
    names = {entry["name"] for entry in payload["corpora"]}
    assert names == {"work", "reference"}
    assert all(entry["available"] is False for entry in payload["corpora"])


def test_system_status_reports_worker_liveness_and_job_counts(tmp_root: Path) -> None:
    tools, conn = _make_tools(tmp_root)
    repo = JobRepository(conn)
    repo.submit("kb_download_batch", {})
    job2 = repo.submit("kb_download_batch", {})
    repo.finish(job2.id, state=JobState.PARTIAL, result={})

    result = tools.system_status({})
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["worker"]["live"] is False
    assert payload["jobs"]["queued"] == 1
    assert payload["jobs"]["partial"] == 1
    assert {entry["name"] for entry in payload["corpora"]} == {"work", "reference"}

    _mark_live_worker(conn)
    result2 = tools.system_status({})
    payload2 = _payload(result2)
    assert payload2["worker"]["live"] is True
