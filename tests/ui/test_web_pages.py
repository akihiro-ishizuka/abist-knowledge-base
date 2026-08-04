"""NiceGUI 画面のスモークテスト(`nicegui.testing.User` を使う Python レベルの fixture)。

Playwright 経由の E2E は次パスの範囲(design/plans/M6-M10-remaining.md Task 6.4)。
ここでは各画面が例外なく描画され、主要なラベル/要素が出ることだけを確認する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing import User

from abist_kb.config import Settings
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.web.app import register_pages
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

pytest_plugins = ["nicegui.testing.plugin"]


def _select_row(user: User, marker: str, row: dict) -> None:
    table = next(iter(user.find(kind=ui.table, marker=marker).elements))
    table.selected = [row]


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


async def test_job_cancel_decline_has_no_side_effect(
    user: User, wired_container: ServiceContainer
) -> None:
    repo = JobRepository(wired_container.conn)
    job = repo.submit("noop", {})

    await user.open(f"/jobs/{job.id}")
    user.find("キャンセル").click()
    await user.should_see(f"対象: ジョブ {job.id}")
    await user.should_see("キャンセルしますか?")

    user.find("いいえ").click()
    unchanged = repo.get(job.id)
    assert unchanged is not None
    assert unchanged.state == JobState.QUEUED


async def test_job_retry_decline_has_no_side_effect(
    user: User, wired_container: ServiceContainer
) -> None:
    repo = JobRepository(wired_container.conn)
    job = repo.submit("noop", {})

    await user.open(f"/jobs/{job.id}")
    user.find("再実行").click()
    await user.should_see(f"対象: ジョブ {job.id}")
    await user.should_see("再実行しますか?")

    user.find("いいえ").click()
    assert len(repo.list()) == 1


async def test_job_retry_error_shows_code_and_message(
    user: User, wired_container: ServiceContainer
) -> None:
    repo = JobRepository(wired_container.conn)
    job = repo.submit("noop", {})

    await user.open(f"/jobs/{job.id}")
    user.find("再実行").click()
    await user.should_see("再実行しますか?")
    user.find("はい").click()

    await user.should_see("INVALID_INPUT")
    await user.should_see("再試行できません")


async def test_batch_run_decline_shows_target_and_output_without_running(
    user: User, wired_container: ServiceContainer
) -> None:
    batch = wired_container.batches.add(
        name="定例取り込み",
        type="web",
        output_dir="docs/weekly",
        items=[],
    )
    repo = JobRepository(wired_container.conn)

    await user.open("/sources")
    _select_row(user, "batches-table", batch)
    user.find("バッチ実行").click()
    await user.should_see("対象: バッチ 定例取り込み")
    await user.should_see("出力先: docs/weekly")
    await user.should_see("実行しますか?")

    user.find("いいえ").click()
    assert repo.list() == []
    assert batch["id"]


async def test_batch_run_confirm_shows_unset_output_destination(
    user: User, wired_container: ServiceContainer
) -> None:
    batch = wired_container.batches.add(
        name="出力先なし",
        type="web",
        output_dir=None,
        items=[],
    )

    await user.open("/sources")
    _select_row(user, "batches-table", batch)
    user.find("バッチ実行").click()

    await user.should_see("対象: バッチ 出力先なし")
    await user.should_see("出力先: 未設定")


async def test_batch_run_confirm_after_selection_creates_job(
    user: User, wired_container: ServiceContainer
) -> None:
    batch = wired_container.batches.add(
        name="定例取り込み",
        type="web",
        output_dir="docs/weekly",
        items=[],
    )
    repo = JobRepository(wired_container.conn)

    await user.open("/sources")
    _select_row(user, "batches-table", batch)
    user.find("バッチ実行").click()
    await user.should_see("実行しますか?")
    user.find("はい").click()
    await user.should_see("実行完了")

    jobs = repo.list()
    assert len(jobs) == 1
    assert jobs[0].kind == "batch"


async def test_source_add_dialog_creates_source(
    user: User, wired_container: ServiceContainer
) -> None:
    await user.open("/sources")
    user.find("ソース追加").click()
    user.find(marker="source-display-name").type("社内Web")
    user.find(marker="source-output-dir").type("docs/internal")
    user.find("保存").click()

    sources = wired_container.sources.list()
    assert len(sources) == 1
    assert sources[0]["display_name"] == "社内Web"


async def test_source_remove_confirm_after_selection_deletes_source(
    user: User, wired_container: ServiceContainer
) -> None:
    source = wired_container.sources.add(
        type="web",
        display_name="削除対象",
        connection={"url": "https://example.invalid"},
        output_dir="docs/remove-me",
    )

    await user.open("/sources")
    _select_row(user, "sources-table", source)
    user.find("ソース削除").click()
    await user.should_see("対象: ソース 削除対象")
    user.find("はい").click()
    await user.should_see("ソースを削除しました。")

    assert wired_container.sources.list() == []


async def test_sources_batches_page_exposes_all_web_actions(
    user: User, wired_container: ServiceContainer
) -> None:
    await user.open("/sources")
    for label in (
        "ソース追加",
        "ソース編集",
        "ソース削除",
        "接続テスト",
        "バッチ追加",
        "バッチ編集",
        "バッチ削除",
        "バッチ実行",
    ):
        await user.should_see(label)


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
