"""`infrastructure.sources.esa` / `application.sync_service` 用の共有フィクスチャ。

brief の要求(「esa モックサーバー(ローカル HTTP)で閉じる」)に従い、実ネットワークを
一切使わない `http.server.ThreadingHTTPServer` ベースの使い捨てモックを提供する。
`test/sync-esa.test.js` 自身は `savePost` にネットワークを介さず post オブジェクトを
直接渡していたため、多くのシナリオはモックサーバー無しで再現できる。モックサーバーは
`EsaClient`/`SyncService` 経由の統合的な経路(検索・ページネーション・個別記事取得)を
検証するテストでのみ使う。
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema

_CATEGORY_RE = re.compile(r'category:"([^"]*)"')

#: テストが本物の esa トークンと見分けやすいよう、識別しやすい値にする
#: (レポート/ログへ漏れていないかを文字列一致で検証できるようにするため)。
FAKE_TOKEN = "test-secret-esa-token-do-not-leak"  # noqa: S105 - テスト専用のダミー値


class _MockEsaHandler(BaseHTTPRequestHandler):
    server: MockEsaServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - BaseHTTPRequestHandler互換
        return  # テスト出力を汚さない

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {self.server.expected_token}"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler の規約
        if not self._check_auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        posts_prefix = f"/v1/teams/{self.server.team}/posts"

        if parsed.path == posts_prefix:
            self.server.search_calls.append(dict(query))
            q = (query.get("q") or [""])[0]
            match = _CATEGORY_RE.search(q)
            if match:
                category = match.group(1).rstrip("/")
                posts = [
                    p
                    for p in self.server.posts.values()
                    if (p.get("category") or "").rstrip("/") == category
                ]
            else:
                posts = list(self.server.posts.values())
            posts.sort(key=lambda p: p["number"])
            self._send_json(200, {"posts": posts, "next_page": None})
            return

        match = re.fullmatch(rf"{re.escape(posts_prefix)}/(\d+)", parsed.path)
        if match:
            number = int(match.group(1))
            post = self.server.posts.get(number)
            if post is None:
                self._send_json(404, {"error": "not found"})
            else:
                self._send_json(200, post)
            return

        self._send_json(404, {"error": "not found"})


class MockEsaServer:
    """テストが `posts` を直接いじれる、使い捨ての esa API モック。"""

    def __init__(self, *, team: str, token: str) -> None:
        self.team = team
        self.expected_token = token
        self.posts: dict[int, dict] = {}
        self.search_calls: list[dict] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MockEsaHandler)
        self._httpd.team = team  # type: ignore[attr-defined]
        self._httpd.expected_token = token  # type: ignore[attr-defined]
        self._httpd.posts = self.posts  # type: ignore[attr-defined]
        self._httpd.search_calls = self.search_calls  # type: ignore[attr-defined]
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
        return f"http://{host}:{port}/v1/teams/{self.team}"

    def add_post(self, post: dict) -> None:
        self.posts[post["number"]] = post


@pytest.fixture
def esa_server() -> Iterator[MockEsaServer]:
    server = MockEsaServer(team="testteam", token=FAKE_TOKEN)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def documents(tmp_root: Path) -> DocumentRepository:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    return DocumentRepository(conn)


__all__ = ["FAKE_TOKEN", "MockEsaServer", "documents", "esa_server"]
