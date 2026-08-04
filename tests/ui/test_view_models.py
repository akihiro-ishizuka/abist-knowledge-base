"""view-model 層(`presentation/web/viewmodels`)のユニットテスト。

NiceGUI/FastAPI に依存せず、`ServiceContainer` を直接叩く。将来の Textual TUI も
同じ `screens.py` を再利用する前提のため、ここで戻り値の形を固定する。
"""

from __future__ import annotations

from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.console.theme import SemanticToken
from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer
from abist_kb.presentation.web.viewmodels.markdown_render import render_markdown_safe
from abist_kb.presentation.web.viewmodels.tokens import job_state_token


def test_dashboard_reports_zero_documents_on_empty_corpus(container: ServiceContainer) -> None:
    data = screens.dashboard(container)
    assert data["document_count"] == 0
    assert data["document_count_by_source"] == {}
    assert data["recent_jobs"] == []
    assert data["warnings"] == []
    assert set(data["index_corpora"]) == {"work", "reference"}
    assert data["index_corpora"]["work"]["available"] is False


def test_partial_job_is_warning_not_success() -> None:
    """M3 の申し送り: 一部失敗ジョブは PARTIAL であり、緑(success)に塗ってはならない。"""
    assert job_state_token(JobState.PARTIAL) is SemanticToken.WARNING
    assert job_state_token(JobState.SUCCEEDED) is SemanticToken.SUCCESS
    assert job_state_token(JobState.PARTIAL) is not job_state_token(JobState.SUCCEEDED)


def test_dashboard_surfaces_partial_job_as_warning(container: ServiceContainer) -> None:
    repo = JobRepository(container.conn)
    job = repo.submit("sync", {"target": "all"})
    claimed = repo.claim("owner-1", ttl_seconds=30)
    assert claimed is not None and claimed.id == job.id
    repo.finish(job.id, state=JobState.PARTIAL, result={"failed_batches": 3})

    data = screens.dashboard(container)
    assert len(data["warnings"]) == 1
    warning = data["warnings"][0]
    assert warning["job_id"] == job.id
    assert warning["state"] == str(JobState.PARTIAL)
    assert warning["state_token"] == SemanticToken.WARNING.value


def test_jobs_list_and_detail_round_trip(container: ServiceContainer) -> None:
    repo = JobRepository(container.conn)
    job = repo.submit("noop", {})

    listing = screens.jobs_list(container)
    assert any(entry["id"] == job.id for entry in listing["jobs"])

    detail = screens.job_detail(container, job.id)
    assert detail["job"]["id"] == job.id
    assert detail["history"] == []


def test_job_detail_not_found_returns_error_dict(container: ServiceContainer) -> None:
    result = screens.job_detail(container, "does-not-exist")
    assert result["error"]["code"] == "NOT_FOUND"


def test_job_cancel_and_retry(container: ServiceContainer) -> None:
    repo = JobRepository(container.conn)
    job = repo.submit("noop", {})
    repo.claim("owner-1", ttl_seconds=30)
    repo.finish(job.id, state=JobState.FAILED, error={"code": "FAILURE", "message": "boom"})

    retried = screens.job_retry(container, job.id)
    assert "job" in retried
    assert retried["job"]["retry_of"] == job.id
    assert retried["job"]["state"] == str(JobState.QUEUED)

    cancel_outcome = screens.job_cancel(container, retried["job"]["id"])
    assert cancel_outcome["job"]["state"] == str(JobState.CANCELLED)


def test_sources_and_batches_list_are_empty_by_default(container: ServiceContainer) -> None:
    assert screens.sources_list(container) == {"sources": []}
    assert screens.batches_list(container) == {"batches": []}


def test_batch_run_not_found_returns_error_dict(container: ServiceContainer) -> None:
    outcome = screens.batch_run(container, "missing-batch")
    assert outcome["error"]["code"] == "NOT_FOUND"


def test_document_update_metadata_rejects_empty_fields(container: ServiceContainer) -> None:
    outcome = screens.document_update_metadata(container, "missing.md", {})
    assert outcome["error"]["code"] == "INVALID_INPUT"


def test_chat_actions_return_config_error_when_service_is_unavailable(
    container: ServiceContainer,
) -> None:
    assert screens.chat_start(container)["error"]["code"] == "CONFIG_ERROR"
    assert (
        screens.chat_ask(container, conversation_id="missing", question="質問")["error"]["code"]
        == "CONFIG_ERROR"
    )
    assert (
        screens.chat_history(container, conversation_id="missing")["error"]["code"]
        == "CONFIG_ERROR"
    )


def test_documents_list_empty(container: ServiceContainer) -> None:
    assert screens.documents_list(container) == {"documents": []}


def test_document_detail_reads_file_and_sanitizes_html(container: ServiceContainer) -> None:
    container.documents.upsert(
        {
            "path": "note.md",
            "source": "manual",
            "status": "active",
            "sync_status": "synced",
        }
    )
    doc_path = container.settings.docs_dir / "note.md"
    doc_path.write_text("# タイトル\n\n本文<script>alert(1)</script>です。\n", encoding="utf-8")

    result = screens.document_detail(container, "note.md")
    assert result["body_missing"] is False
    assert "<script>" not in result["body_html"]
    assert "本文" in result["body_html"]


def test_document_detail_not_found(container: ServiceContainer) -> None:
    result = screens.document_detail(container, "missing.md")
    assert result["error"]["code"] == "NOT_FOUND"


def test_document_detail_body_missing_when_file_absent(container: ServiceContainer) -> None:
    container.documents.upsert(
        {"path": "ghost.md", "source": "manual", "status": "active", "sync_status": "synced"}
    )
    result = screens.document_detail(container, "ghost.md")
    assert result["body_missing"] is True
    assert result["body_html"] is None


def test_render_markdown_safe_strips_script_and_event_handlers() -> None:
    html = render_markdown_safe('<img src=x onerror="alert(1)">plain <script>bad()</script>')
    assert "onerror" not in html
    assert "<script>" not in html
    assert "plain" in html


def test_search_without_index_returns_error_dict(container: ServiceContainer) -> None:
    outcome = screens.search(container, "hello")
    assert outcome["error"]["code"] == "INVALID_INPUT"


def test_chat_and_quality_are_stubs(container: ServiceContainer) -> None:
    # チャットは openai_api_key 未設定のテスト環境ではスタブへフォールバックする。
    chat = screens.chat_stub(container)
    assert chat["available"] is False
    assert chat["reason"]
    # 品質監査4種(M7 Task 7.2)は配線済み。
    assert screens.quality_stub(container)["available"] is True


# ---------------------------------------------------------------------------
# 可視化(設計書 §10: render_scene をジョブとして実行する)
# ---------------------------------------------------------------------------


_VALID_SCENE_SPEC: dict[str, object] = {
    "schema_version": "1.0",
    "scene_kind": "explain",
    "output_format": "mp4",
    "template": "step_explanation",
    "title": "テスト用シーン",
    "sources": [
        {
            "id": "s1",
            "path": "doc.md",
            "start_line": 1,
            "end_line": 2,
            "content_hash": "",
        }
    ],
    "beats": [{"type": "metric", "label": "テスト指標", "value": "1", "source_refs": ["s1"]}],
}


def _write_doc_and_valid_spec(container: ServiceContainer) -> dict[str, object]:
    from abist_kb.domain.line_range import range_hash

    text = "行1\n行2\n行3\n"
    (container.settings.docs_dir / "doc.md").write_text(text, encoding="utf-8")
    hashed = range_hash(text, 1, 2)
    assert hashed.ok and hashed.hash is not None
    spec = dict(_VALID_SCENE_SPEC)
    spec["sources"] = [{**_VALID_SCENE_SPEC["sources"][0], "content_hash": hashed.hash}]  # type: ignore[index]
    return spec


def test_visualization_validate_rejects_invalid_scene_spec(
    container: ServiceContainer,
) -> None:
    outcome = screens.visualization_validate(container, {"scene_kind": "explain"})
    assert outcome["ok"] is False
    assert outcome["code"] == "INVALID_SCENE_SPEC"
    assert outcome["errors"]


def test_visualization_validate_surfaces_source_hash_mismatch(
    container: ServiceContainer,
) -> None:
    spec = _write_doc_and_valid_spec(container)
    spec["sources"] = [{**spec["sources"][0], "content_hash": "f" * 64}]  # type: ignore[index]

    outcome = screens.visualization_validate(container, spec)
    assert outcome["ok"] is False
    assert outcome["code"] == "SOURCE_HASH_MISMATCH"


def test_visualization_validate_accepts_valid_scene_spec(container: ServiceContainer) -> None:
    spec = _write_doc_and_valid_spec(container)

    outcome = screens.visualization_validate(container, spec)
    assert outcome["ok"] is True
    assert outcome["spec"]["title"] == "テスト用シーン"


def test_visualization_deps_reports_ready_and_messages(container: ServiceContainer) -> None:
    data = screens.visualization_deps(container)
    assert data["ok"] is True
    assert data["ready"] in (True, False)
    assert isinstance(data["messages"], list)


def test_visualization_submit_render_fails_without_live_worker(
    container: ServiceContainer,
) -> None:
    spec = _write_doc_and_valid_spec(container)

    outcome = screens.visualization_submit_render(container, spec)
    assert outcome["error"]["code"] == "WORKER_UNAVAILABLE"


def test_visualization_submit_render_queues_job_when_worker_is_live(
    container: ServiceContainer,
) -> None:
    from abist_kb.infrastructure.jobs import leases

    leases.try_acquire_worker_lease(container.conn, "test-worker", ttl_seconds=300)
    spec = _write_doc_and_valid_spec(container)

    outcome = screens.visualization_submit_render(container, spec, slug="test-slug")
    assert "job" in outcome
    job = outcome["job"]
    assert job["kind"] == "render_scene"
    assert job["state"] == str(JobState.QUEUED)
    assert job["params"]["scene_spec"]["title"] == "テスト用シーン"
    assert job["params"]["slug"] == "test-slug"


def test_settings_diagnostics_reports_paths_and_probes(container: ServiceContainer) -> None:
    data = screens.settings_diagnostics(container)
    assert data["paths"]["root_dir"] == str(container.settings.root_dir)
    assert data["diagnostics"]["fts5_available"] in (True, False)
    assert data["diagnostics"]["ffmpeg_available"] in (True, False)
    assert data["diagnostics"]["manim_available"] in (True, False)
