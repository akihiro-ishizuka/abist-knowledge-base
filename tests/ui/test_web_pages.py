"""NiceGUI 画面のスモークテスト(`nicegui.testing.User` を使う Python レベルの fixture)。

Playwright 経由の E2E は次パスの範囲(design/plans/M6-M10-remaining.md Task 6.4)。
ここでは各画面が例外なく描画され、主要なラベル/要素が出ることだけを確認する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nicegui.testing import User

from abist_kb.config import Settings
from abist_kb.presentation.web.app import register_pages
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

pytest_plugins = ["nicegui.testing.plugin"]


@pytest.fixture
def wired_container(tmp_path: Path) -> ServiceContainer:
    settings = Settings(root_dir=tmp_path / "root", _env_file=None)
    settings.ensure_directories()
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    cont = ServiceContainer(settings, check_same_thread=False)
    register_pages(cont)
    return cont


async def test_dashboard_page_renders(user: User, wired_container: ServiceContainer) -> None:
    await user.open("/")
    await user.should_see("ダッシュボード")


async def test_sources_page_renders(user: User, wired_container: ServiceContainer) -> None:
    await user.open("/sources")
    await user.should_see("ソース")


async def test_jobs_page_renders(user: User, wired_container: ServiceContainer) -> None:
    await user.open("/jobs")


async def test_documents_page_renders(user: User, wired_container: ServiceContainer) -> None:
    await user.open("/documents")


async def test_search_page_renders(user: User, wired_container: ServiceContainer) -> None:
    await user.open("/search")
    await user.should_see("検索")


async def test_chat_page_shows_stub_notice(user: User, wired_container: ServiceContainer) -> None:
    # openai_api_key 未設定のテスト環境ではチャットはスタブへフォールバックする。
    await user.open("/chat")
    await user.should_see("チャットは利用できません")


async def test_visualization_page_shows_stub_notice(
    user: User, wired_container: ServiceContainer
) -> None:
    await user.open("/visualization")
    await user.should_see("M7 で実装予定")


async def test_quality_page_renders(user: User, wired_container: ServiceContainer) -> None:
    # 品質監査4種(M7 Task 7.2)は配線済み。
    await user.open("/quality")
    await user.should_see("品質監査")


async def test_settings_page_renders(user: User, wired_container: ServiceContainer) -> None:
    await user.open("/settings")
    await user.should_see("設定")
