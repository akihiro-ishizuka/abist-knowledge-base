"""`/api/v1` FastAPI 層のテスト(§7.1, §10.3)。SSE・トークン認証・エラーコード変換。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.api.app import create_api_app
from abist_kb.presentation.common.container import ServiceContainer


@pytest.fixture
def client(container: ServiceContainer) -> TestClient:
    app = create_api_app(container, bind_host="127.0.0.1")
    return TestClient(app)


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard(client: TestClient) -> None:
    response = client.get("/api/v1/dashboard")
    assert response.status_code == 200
    assert response.json()["document_count"] == 0


def test_jobs_not_found_returns_404_with_error_code(client: TestClient) -> None:
    response = client.get("/api/v1/jobs/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_FOUND"


def test_batch_run_not_found_returns_404(client: TestClient) -> None:
    response = client.post("/api/v1/batches/missing/run")
    assert response.status_code == 404


def test_batch_run_without_worker_returns_409(
    client: TestClient, container: ServiceContainer
) -> None:
    created = container.batches.add(
        name="API待機",
        type="web",
        output_dir="docs/api-wait",
        items=[],
    )
    response = client.post(f"/api/v1/batches/{created['id']}/run")
    assert response.status_code == 409
    assert response.json()["code"] == "WORKER_UNAVAILABLE"
    assert JobRepository(container.conn).list() == []


def test_batch_run_with_live_worker_queues_job(
    client: TestClient, container: ServiceContainer
) -> None:
    from abist_kb.infrastructure.jobs import leases

    created = container.batches.add(
        name="API投入",
        type="web",
        output_dir="docs/api-queued",
        items=[],
    )
    leases.try_acquire_worker_lease(container.conn, "test-worker", ttl_seconds=300)

    response = client.post(f"/api/v1/batches/{created['id']}/run")
    assert response.status_code == 200
    body = response.json()
    assert body["job"]["kind"] == "batch"
    assert body["job"]["state"] == str(JobState.QUEUED)
    assert body["job"]["params"]["batch_id"] == created["id"]


def test_batch_crud_routes(client: TestClient) -> None:
    created = client.post(
        "/api/v1/batches",
        json={
            "name": "定例取り込み",
            "type": "web",
            "output_dir": "docs/weekly",
            "items": [],
        },
    )
    assert created.status_code == 200
    batch = created.json()["batch"]

    updated = client.patch(
        f"/api/v1/batches/{batch['id']}",
        json={"name": "週次取り込み", "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["batch"]["name"] == "週次取り込み"
    assert updated.json()["batch"]["enabled"] is False

    deleted = client.delete(f"/api/v1/batches/{batch['id']}?confirmed=true")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}


def test_batch_delete_without_confirmation_has_no_side_effect(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/batches",
        json={"name": "保持対象", "type": "web", "items": []},
    ).json()["batch"]

    response = client.delete(f"/api/v1/batches/{created['id']}")

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert any(
        batch["id"] == created["id"] for batch in client.get("/api/v1/batches").json()["batches"]
    )


def test_batch_edit_missing_id_returns_404(client: TestClient) -> None:
    response = client.patch("/api/v1/batches/missing", json={"enabled": False})
    assert response.status_code == 404


def test_source_crud_routes(client: TestClient) -> None:
    created = client.post(
        "/api/v1/sources",
        json={
            "type": "web",
            "display_name": "社内サイト",
            "connection": {"url": "https://example.invalid"},
            "output_dir": "docs/web",
        },
    )
    assert created.status_code == 200
    source = created.json()["source"]

    updated = client.patch(
        f"/api/v1/sources/{source['id']}",
        json={"display_name": "社内 Web", "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["source"]["display_name"] == "社内 Web"
    assert updated.json()["source"]["enabled"] is False

    deleted = client.delete(f"/api/v1/sources/{source['id']}?confirmed=true")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}


def test_source_delete_without_confirmation_returns_400(client: TestClient) -> None:
    response = client.delete("/api/v1/sources/missing")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_source_delete_missing_id_returns_404(client: TestClient) -> None:
    response = client.delete("/api/v1/sources/missing?confirmed=true")
    assert response.status_code == 404


def test_document_update_and_delete_routes(client: TestClient, container: ServiceContainer) -> None:
    container.documents.upsert(
        {
            "path": "nested/note.md",
            "source": "manual",
            "status": "active",
            "sync_status": "synced",
        }
    )

    updated = client.patch(
        "/api/v1/documents/nested/note.md",
        json={"status": "archived", "document_type": "memo"},
    )
    assert updated.status_code == 200
    assert updated.json()["document"]["status"] == "archived"
    assert updated.json()["document"]["document_type"] == "memo"

    deleted = client.delete("/api/v1/documents/nested/note.md?confirmed=true")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}


def test_document_update_rejects_disallowed_key(
    client: TestClient, container: ServiceContainer
) -> None:
    container.documents.upsert(
        {
            "path": "note.md",
            "source": "manual",
            "status": "active",
            "sync_status": "synced",
        }
    )
    response = client.patch("/api/v1/documents/note.md", json={"title": "書換禁止"})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_document_delete_without_confirmation_returns_400(client: TestClient) -> None:
    response = client.delete("/api/v1/documents/missing.md")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_chat_and_quality_endpoints(client: TestClient) -> None:
    # チャットは openai_api_key 未設定のテスト環境ではスタブへフォールバックする
    # (`_clear_app_env` autouse fixture が ABIST_KB_* を毎回消すため)。
    response = client.get("/api/v1/chat")
    assert response.status_code == 200
    assert response.json()["available"] is False

    # 品質監査4種(M7 Task 7.2)は配線済みで常に利用可能。
    response = client.get("/api/v1/quality")
    assert response.status_code == 200
    assert response.json()["available"] is True


def test_chat_action_routes_return_config_error_without_api_key(client: TestClient) -> None:
    started = client.post("/api/v1/chat/start", json={})
    asked = client.post(
        "/api/v1/chat/ask",
        json={"conversation_id": "missing", "question": "質問"},
    )
    history = client.get("/api/v1/chat/missing/history")

    for response in (started, asked, history):
        assert response.status_code == 500
        assert response.json()["code"] == "CONFIG_ERROR"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/quality/integrity", {}),
        ("/api/v1/quality/duplicates", {}),
        ("/api/v1/quality/contradictions", {}),
        ("/api/v1/quality/backfill-metadata", {}),
    ],
)
def test_quality_action_routes_smoke(
    client: TestClient, path: str, body: dict[str, object]
) -> None:
    response = client.post(path, json=body)

    assert response.status_code == 200
    assert "run_id" in response.json()


def test_quality_backfill_metadata_apply_returns_400_invalid_input(client: TestClient) -> None:
    """Web からの `apply=True` はハード拒否(§12)。エラー封筒に `code` が
    無いと `run_locked` が `ErrorCode.FAILURE`(500)へ落ちてしまうため、
    `INVALID_INPUT`(400)を明示的に確認する。"""
    response = client.post("/api/v1/quality/backfill-metadata", json={"apply": True})

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# 可視化(設計書 §10: render_scene をジョブとして実行する)
# ---------------------------------------------------------------------------


def test_visualization_deps_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/visualization/deps")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["ready"] in (True, False)


def test_visualization_validate_endpoint_rejects_invalid_scene_spec(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/visualization/validate",
        json={"scene_spec": {"scene_kind": "explain"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "INVALID_SCENE_SPEC"


def test_visualization_render_endpoint_returns_worker_unavailable_without_worker(
    client: TestClient, container: ServiceContainer
) -> None:
    (container.settings.docs_dir / "doc.md").write_text("行1\n行2\n", encoding="utf-8")
    from abist_kb.domain.line_range import range_hash

    hashed = range_hash("行1\n行2\n", 1, 2)
    assert hashed.ok and hashed.hash is not None
    scene_spec = {
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
                "content_hash": hashed.hash,
            }
        ],
        "beats": [{"type": "metric", "label": "テスト指標", "value": "1", "source_refs": ["s1"]}],
    }

    response = client.post("/api/v1/visualization/render", json={"scene_spec": scene_spec})
    assert response.status_code == 409
    assert response.json()["code"] == "WORKER_UNAVAILABLE"


def test_settings_diagnostics_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/settings/diagnostics")
    assert response.status_code == 200
    assert "paths" in response.json()


def test_job_events_sse_streams_history_then_closes_for_terminal_job(
    client: TestClient, container: ServiceContainer
) -> None:
    repo = JobRepository(container.conn)
    job = repo.submit("noop", {})
    repo.claim("owner-1", ttl_seconds=30)
    repo.finish(job.id, state=JobState.SUCCEEDED, result={})

    with client.stream("GET", f"/api/v1/jobs/{job.id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = list(response.iter_lines())
    # 履歴が空(進捗イベントを一切 emit していないジョブ)でも、ストリーム自体は
    # 正常に開始し、終了状態のジョブなのですぐ閉じる(タイムアウトしない)。
    assert lines == [] or all(line.startswith("data:") or line == "" for line in lines)


def test_job_events_sse_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/jobs/does-not-exist/events")
    assert response.status_code == 404


def test_non_loopback_bind_requires_access_token(container: ServiceContainer) -> None:
    with pytest.raises(ValueError):
        create_api_app(container, bind_host="0.0.0.0", access_token=None)


def test_non_loopback_bind_rejects_missing_or_wrong_token(container: ServiceContainer) -> None:
    app = create_api_app(container, bind_host="0.0.0.0", access_token="secret-token")
    with TestClient(app) as authed_client:
        no_token = authed_client.get("/api/v1/dashboard")
        assert no_token.status_code == 401

        wrong_token = authed_client.get("/api/v1/dashboard", headers={"X-API-Token": "nope"})
        assert wrong_token.status_code == 401

        ok = authed_client.get("/api/v1/dashboard", headers={"X-API-Token": "secret-token"})
        assert ok.status_code == 200


def test_non_loopback_bind_health_check_does_not_require_token(
    container: ServiceContainer,
) -> None:
    app = create_api_app(container, bind_host="0.0.0.0", access_token="secret-token")
    with TestClient(app) as authed_client:
        response = authed_client.get("/api/v1/health")
        assert response.status_code == 200


def test_loopback_bind_does_not_require_token(client: TestClient) -> None:
    response = client.get("/api/v1/dashboard")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# スタンドアロン `api serve` / クロスプロセス SSE
# ---------------------------------------------------------------------------


def _abist_kb_argv(*args: str) -> list[str]:
    import shutil

    exe = shutil.which("abist-kb")
    if exe is None:
        raise RuntimeError("abist-kb が PATH にありません(uv sync / editable install を確認)。")
    return [exe, *args]


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_healthy(base_url: str, *, timeout_sec: float = 30.0) -> None:
    import time
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"API が起動しませんでした: {last_error}")


def test_api_serve_subprocess_health(tmp_root: Path) -> None:
    """実プロセスの `abist-kb api serve` が /api/v1/health を返すこと。"""
    import subprocess
    import urllib.request

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        _abist_kb_argv(
            "--root",
            str(tmp_root),
            "api",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _wait_healthy(base_url)
        with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=5.0) as resp:
            assert resp.status == 200
            assert resp.read() == b'{"status":"ok"}'
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_sse_cross_process_db_poll_sees_worker_terminal(tmp_root: Path) -> None:
    """API プロセスの SSE が、別プロセス worker の DB 書き込みを受け取ること。"""
    import json
    import subprocess
    import threading
    import time
    import urllib.request
    import uuid

    from abist_kb.application.job_service import JobService
    from abist_kb.config import Settings
    from abist_kb.domain.job import TERMINAL_STATES, JobState
    from abist_kb.infrastructure.db.schema import open_app_db
    from abist_kb.infrastructure.jobs.builtin_registry import (
        build_builtin_handlers,
        build_builtin_resources,
    )

    settings = Settings(root_dir=tmp_root, _env_file=None)
    settings.ensure_directories()

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    api_proc = subprocess.Popen(
        _abist_kb_argv(
            "--root",
            str(tmp_root),
            "api",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _wait_healthy(base_url)

        conn = open_app_db(settings.app_db_path)
        try:
            handlers = build_builtin_handlers(settings=settings, conn=conn)
            service = JobService(
                conn,
                owner_id=str(uuid.uuid4()),
                handlers=handlers,
                resource_for_kind=build_builtin_resources(),
            )
            job = service.submit("noop", {})
            job_id = job.id
            assert job.state is JobState.QUEUED
        finally:
            conn.close()

        sse_lines: list[str] = []
        sse_error: list[BaseException] = []

        def _read_sse() -> None:
            try:
                req = urllib.request.Request(f"{base_url}/api/v1/jobs/{job_id}/events")
                with urllib.request.urlopen(req, timeout=60.0) as resp:
                    assert resp.status == 200
                    while True:
                        raw = resp.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8").rstrip("\n")
                        sse_lines.append(line)
            except BaseException as exc:  # noqa: BLE001 — スレッドへ運ぶ
                sse_error.append(exc)

        reader = threading.Thread(target=_read_sse, daemon=True)
        reader.start()
        # SSE が queued 状態のポーリングに入ってから worker を回す
        time.sleep(0.5)

        worker = subprocess.run(
            _abist_kb_argv("--root", str(tmp_root), "worker", "run", "--once"),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        assert worker.returncode == 0, worker.stderr or worker.stdout

        reader.join(timeout=30)
        assert not reader.is_alive(), "SSE ストリームが終端で閉じませんでした"
        assert not sse_error, sse_error

        data_payloads: list[dict[str, object]] = []
        for line in sse_lines:
            if line.startswith("data: "):
                data_payloads.append(json.loads(line[len("data: ") :]))

        assert data_payloads, f"進捗イベントが届きませんでした: {sse_lines!r}"
        assert any(p.get("phase") == "noop" for p in data_payloads)

        conn = open_app_db(settings.app_db_path)
        try:
            final = JobService(conn, owner_id="check").get(job_id)
        finally:
            conn.close()
        assert final.state in TERMINAL_STATES
        assert final.state is JobState.SUCCEEDED
    finally:
        api_proc.terminate()
        try:
            api_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api_proc.kill()
            api_proc.wait(timeout=5)
