"""`/api/v1` FastAPI 層のテスト(§7.1, §10.3)。SSE・トークン認証・エラーコード変換。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.api.app import create_api_app
from abist_kb.presentation.web.viewmodels.container import ServiceContainer


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


def test_chat_visualization_quality_endpoints(client: TestClient) -> None:
    # 可視化は次パス(M7 Task 7.3/7.4)のためスタブのまま。
    response = client.get("/api/v1/visualization")
    assert response.status_code == 200
    assert response.json()["available"] is False

    # チャットは openai_api_key 未設定のテスト環境ではスタブへフォールバックする
    # (`_clear_app_env` autouse fixture が ABIST_KB_* を毎回消すため)。
    response = client.get("/api/v1/chat")
    assert response.status_code == 200
    assert response.json()["available"] is False

    # 品質監査4種(M7 Task 7.2)は配線済みで常に利用可能。
    response = client.get("/api/v1/quality")
    assert response.status_code == 200
    assert response.json()["available"] is True


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
