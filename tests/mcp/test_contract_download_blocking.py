"""kb-download のブロッキング実処理(M5 task-3b)を検証するテスト。

fixture が存在しない領域(旧実装が子プロセス spawn を伴うブロッキング処理
だったため、契約採取時に実行されていない)なので、`replay.py` 経由のゴールデン
比較ではなく、実際にローカルの使い捨てリソース(ローカル git リポジトリ・
ローカル HTTP サーバー・ローカル esa モックサーバー)を使って動作を検証する。
ネットワーク・旧リポジトリには一切触れない。
"""

from __future__ import annotations

# `tests/sources/conftest.py` の MockEsaServer を再利用する(pytest の conftest
# スコープは tests/mcp から自動では見えないため、`tests.conftest` との名前衝突を
# 避けつつ importlib でパス指定 import する)。
import importlib.util as _importlib_util  # noqa: E402
import json
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from abist_kb.application.batch_service import BatchService
from abist_kb.domain.job import ResourceKind
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.infrastructure.jobs import leases
from abist_kb.presentation.mcp.kb_download import KbDownloadTools

_sources_conftest_path = Path(__file__).resolve().parents[1] / "sources" / "conftest.py"
_spec = _importlib_util.spec_from_file_location(
    "_sources_conftest_for_mcp_tests", _sources_conftest_path
)
assert _spec is not None and _spec.loader is not None
_sources_conftest = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_sources_conftest)
FAKE_TOKEN = _sources_conftest.FAKE_TOKEN
MockEsaServer = _sources_conftest.MockEsaServer


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )


def _make_upstream_repo(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], directory)
    _git(["config", "user.email", "test@example.com"], directory)
    _git(["config", "user.name", "Test"], directory)
    (directory / "README.md").write_text("# hello\n", encoding="utf-8")
    _git(["add", "."], directory)
    _git(["commit", "-m", "initial"], directory)
    return directory


class _StaticHandler(BaseHTTPRequestHandler):
    server: _StaticServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        body = self.server.pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class _StaticServer:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StaticHandler)
        self._httpd.pages = pages  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"


@pytest.fixture
def static_site() -> Iterator[_StaticServer]:
    server = _StaticServer({"/": "<html><body><h1>Top</h1></body></html>"})
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def esa_server() -> Iterator[MockEsaServer]:
    server = MockEsaServer(team="testteam", token=FAKE_TOKEN)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _make_tools(tmp_root: Path, *, name: str = "app.sqlite") -> KbDownloadTools:
    conn = connect(tmp_root / name)
    ensure_app_schema(conn)
    return KbDownloadTools(conn, root_dir=tmp_root)


def _payload(result: object) -> dict:
    return json.loads(result.content[0].text)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# download_git
# ---------------------------------------------------------------------------


def test_download_git_clones_local_repo_and_reports_ok(tmp_root: Path) -> None:
    upstream = _make_upstream_repo(tmp_root / "upstream")
    tools = _make_tools(tmp_root)

    result = tools.download_git({"repository": str(upstream)})
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["exitCode"] == 0
    assert payload["batchType"] == "git"
    assert payload["sync"]["totals"]["added"] >= 1
    assert (tmp_root / payload["outputDir"] / "README.md").is_file()


def test_download_git_nonexistent_repository_is_not_ok(tmp_root: Path) -> None:
    tools = _make_tools(tmp_root)
    result = tools.download_git({"repository": str(tmp_root / "does-not-exist")})
    payload = _payload(result)
    assert payload["ok"] is False
    assert result.isError


# ---------------------------------------------------------------------------
# run_batch(git バッチ経由)
# ---------------------------------------------------------------------------


def test_run_batch_git_batch_succeeds(tmp_root: Path) -> None:
    upstream = _make_upstream_repo(tmp_root / "upstream")
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    BatchService(conn).add(
        name="gitバッチ",
        type="git",
        output_dir="docs/gitバッチ",
        items=[{"options": {"repository": str(upstream)}}],
    )
    tools = KbDownloadTools(conn, root_dir=tmp_root)

    result = tools.run_batch({"batch": "gitバッチ"})
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["command"] == ["run_batch", "gitバッチ"]
    assert (tmp_root / "docs" / "gitバッチ" / "README.md").is_file()


# ---------------------------------------------------------------------------
# download_web
# ---------------------------------------------------------------------------


def test_download_web_crawls_local_site(tmp_root: Path, static_site: _StaticServer) -> None:
    tools = _make_tools(tmp_root)
    result = tools.download_web({"url": static_site.base_url + "/"})
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["batchType"] == "web"
    assert payload["sync"]["totals"]["added"] == 1


# ---------------------------------------------------------------------------
# download_esa_post / download_esa_category / download_esa_search
# ---------------------------------------------------------------------------


def test_download_esa_post_downloads_single_post(
    tmp_root: Path, esa_server: MockEsaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESA_TEAM_NAME", esa_server.team)
    monkeypatch.setenv("ESA_ACCESS_TOKEN", esa_server.expected_token)
    monkeypatch.setenv("ESA_BASE_URL", esa_server.base_url)
    esa_server.add_post(
        {
            "number": 1,
            "name": "テスト記事",
            "category": "設計",
            "body_md": "本文",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
            "url": "https://example.esa.io/posts/1",
        }
    )
    tools = _make_tools(tmp_root)

    result = tools.download_esa_post({"post": 1})
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["sync"]["totals"]["added"] == 1


def test_download_esa_category_downloads_matching_posts(
    tmp_root: Path, esa_server: MockEsaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESA_TEAM_NAME", esa_server.team)
    monkeypatch.setenv("ESA_ACCESS_TOKEN", esa_server.expected_token)
    monkeypatch.setenv("ESA_BASE_URL", esa_server.base_url)
    esa_server.add_post(
        {
            "number": 2,
            "name": "カテゴリ記事",
            "category": "設計/対象",
            "body_md": "本文2",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
            "url": "https://example.esa.io/posts/2",
        }
    )
    tools = _make_tools(tmp_root)

    result = tools.download_esa_category({"category": "設計/対象"})
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["sync"]["totals"]["added"] == 1


def test_download_esa_search_downloads_hits(
    tmp_root: Path, esa_server: MockEsaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESA_TEAM_NAME", esa_server.team)
    monkeypatch.setenv("ESA_ACCESS_TOKEN", esa_server.expected_token)
    monkeypatch.setenv("ESA_BASE_URL", esa_server.base_url)
    esa_server.add_post(
        {
            "number": 3,
            "name": "検索記事",
            "category": "任意",
            "body_md": "本文3",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
            "url": "https://example.esa.io/posts/3",
        }
    )
    tools = _make_tools(tmp_root)

    result = tools.download_esa_search({"query": "検索記事"})
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["sync"]["totals"]["added"] == 1


def test_download_esa_post_without_credentials_fails(tmp_root: Path) -> None:
    tools = _make_tools(tmp_root)
    result = tools.download_esa_post({"post": 1})
    payload = _payload(result)
    assert payload["ok"] is False
    assert result.isError


# ---------------------------------------------------------------------------
# add_web_batch
# ---------------------------------------------------------------------------


def test_add_web_batch_rejects_non_http_scheme(tmp_root: Path) -> None:
    tools = _make_tools(tmp_root)
    result = tools.add_web_batch({"name": "x", "url": "ftp://example.com/"})
    payload = _payload(result)
    assert payload["ok"] is False
    assert result.isError


@pytest.mark.parametrize("bad_output_dir", ["/etc/passwd", "../escape", "docs/../../escape"])
def test_add_web_batch_rejects_absolute_or_traversal_output_dir(
    tmp_root: Path, bad_output_dir: str
) -> None:
    tools = _make_tools(tmp_root)
    result = tools.add_web_batch(
        {"name": "x", "url": "https://example.com/", "outputDir": bad_output_dir}
    )
    payload = _payload(result)
    assert payload["ok"] is False


def test_add_web_batch_prefixes_output_dir_with_docs(tmp_root: Path) -> None:
    tools = _make_tools(tmp_root)
    result = tools.add_web_batch(
        {"name": "prefix-test", "url": "https://example.com/", "outputDir": "somewhere"}
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["batch"]["outputDir"] == "docs/somewhere"


def test_add_web_batch_refuses_overwrite_without_flag(tmp_root: Path) -> None:
    tools = _make_tools(tmp_root)
    tools.add_web_batch({"name": "dup", "url": "https://example.com/"})
    result = tools.add_web_batch({"name": "dup", "url": "https://example.com/other"})
    payload = _payload(result)
    assert payload["ok"] is False


def test_add_web_batch_overwrites_when_flag_set(tmp_root: Path) -> None:
    tools = _make_tools(tmp_root)
    tools.add_web_batch({"name": "dup2", "url": "https://example.com/"})
    result = tools.add_web_batch(
        {"name": "dup2", "url": "https://example.com/other", "overwrite": True}
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["batch"]["url"] == "https://example.com/other"


def test_add_web_batch_refuses_overwrite_of_non_web_batch(tmp_root: Path) -> None:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    BatchService(conn).add(name="esaバッチ", type="esa", output_dir="docs/esaバッチ", items=[])
    tools = KbDownloadTools(conn, root_dir=tmp_root)

    result = tools.add_web_batch(
        {"name": "esaバッチ", "url": "https://example.com/", "overwrite": True}
    )
    payload = _payload(result)
    assert payload["ok"] is False


# ---------------------------------------------------------------------------
# docs-write リースの busy 応答(single-flight)
# ---------------------------------------------------------------------------


def test_run_batch_returns_busy_when_docs_write_lease_is_held(tmp_root: Path) -> None:
    upstream = _make_upstream_repo(tmp_root / "upstream")
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    BatchService(conn).add(
        name="busyバッチ",
        type="git",
        output_dir="docs/busyバッチ",
        items=[{"options": {"repository": str(upstream)}}],
    )
    tools = KbDownloadTools(conn, root_dir=tmp_root)

    with leases.acquire_resource_lease(
        conn, ResourceKind.DOCS_WRITE, owner_id="other-process", ttl_seconds=30.0, wait=False
    ):
        result = tools.run_batch({"batch": "busyバッチ"})

    payload = _payload(result)
    assert payload["ok"] is False
    assert result.isError
    assert "実行中" in payload["error"]
    assert payload["exitCode"] is None
