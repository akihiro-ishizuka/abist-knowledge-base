"""Playwright E2E(§13.2): 主要フロー + アクセシビリティ確認。

実 HTTP サーバー(uvicorn 上の NiceGUI アプリ)をバックグラウンドスレッドで
起動し、ブラウザから実際に操作する。`nicegui.testing.User`(Python レベル)
では検証できない、実ブラウザでのレンダリング・ナビゲーション・キーボード
操作可能性を確認する。
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from nicegui import ui

pytest.importorskip(
    "playwright",
    reason=(
        "playwright is an opt-in dev dependency (real-browser E2E, "
        "installed separately via `uv run playwright install`); "
        "skipping means this file's browser-level checks are unverified "
        "in this environment"
    ),
)
from playwright.sync_api import Page, expect  # noqa: E402

from abist_kb.config import Settings
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.web.app import build_web_app
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

pytestmark = [pytest.mark.timing_sensitive, pytest.mark.e2e_browser]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, ServiceContainer]]:
    """モジュールにつき1つの実サーバー(`nicegui.app` はプロセス内シングルトンで、
    2回目の `build_web_app()`(=2回目の `add_middleware`)はスターレットが
    "already started" で拒否するため、テストごとに新しいサーバーは作れない)。"""
    tmp_path = tmp_path_factory.mktemp("e2e")
    settings = Settings(root_dir=tmp_path / "root", _env_file=None)
    settings.ensure_directories()
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    container = ServiceContainer(settings, check_same_thread=False)
    app = build_web_app(container, bind_host="127.0.0.1", start_worker=False)
    # `ui.run()`(実プロセスの `ui_cmd.py::ui_web`)を呼ばずに `nicegui.app` を
    # 単体の ASGI アプリとして uvicorn へ渡すには、`ui.run_with()` で NiceGUI
    # 内部のスタートアップフラグを立てる必要がある(未設定だと `nicegui.nicegui.
    # _startup()` が `RuntimeError('You must call ui.run() ...')` を送出する)。
    ui.run_with(app, show_welcome_message=False)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    # 別プロセスが埋め込み計測などで CPU を専有していると起動が遅延しうるため
    # (design/plans/M6-M10-remaining.md 実行環境の注記)、余裕を持たせる。
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:  # pragma: no cover - 起動失敗時のみ
        pytest.fail("live NiceGUI server did not start in time")

    try:
        yield base_url, container
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        container.close()


def test_dashboard_loads_and_navigates_to_jobs(
    page: Page, live_server: tuple[str, ServiceContainer]
) -> None:
    base_url, _container = live_server
    page.goto(base_url + "/")
    expect(page.get_by_role("link", name="ダッシュボード")).to_be_visible()

    page.get_by_role("link", name="ジョブ", exact=True).click()
    expect(page).to_have_url(base_url + "/jobs")


def test_search_flow_returns_to_visible_page(
    page: Page, live_server: tuple[str, ServiceContainer]
) -> None:
    base_url, _container = live_server
    page.goto(base_url + "/search")
    expect(page.get_by_role("link", name="検索", exact=True)).to_be_visible()


def test_partial_job_badge_carries_symbol_not_only_color(
    page: Page, live_server: tuple[str, ServiceContainer]
) -> None:
    """§6.1: 色だけで状態を伝えない。ブラウザ側でバッジのテキストに記号が
    実際に描画されていることを確認する(CSS 色は取得しない、テキストのみ検査)。
    """
    base_url, container = live_server
    repo = JobRepository(container.conn)
    job = repo.submit("batch", {"batch_id": "release-notes"})
    claimed = repo.claim("owner-1", ttl_seconds=30)
    assert claimed is not None
    repo.finish(job.id, state=JobState.PARTIAL, result={"failed_batches": 1})

    page.goto(base_url + "/")
    badge = page.get_by_text("! partial")
    expect(badge).to_be_visible()


def test_accessibility_basics_html_lang_and_page_title(
    page: Page, live_server: tuple[str, ServiceContainer]
) -> None:
    """最低限のアクセシビリティ回帰ガード: `<html lang>` とページタイトルが
    空でないこと、主要な操作要素(リンク)がアクセシブルな名前を持つこと。
    """
    base_url, _container = live_server
    page.goto(base_url + "/")

    lang = page.eval_on_selector("html", "el => el.getAttribute('lang')")
    assert lang, "html[lang] が設定されていない(スクリーンリーダーが言語を判定できない)"

    assert page.title() != ""

    links = page.locator("a")
    count = links.count()
    for i in range(count):
        link = links.nth(i)
        accessible_text = (link.inner_text() or link.get_attribute("aria-label") or "").strip()
        assert accessible_text, "リンクにアクセシブルな名前がない(空リンク)"
