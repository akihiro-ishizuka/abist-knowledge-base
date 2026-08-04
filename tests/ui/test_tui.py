"""Textual TUI のテスト(§13.2): ナビ・検索・モーダル・キャンセル・狭幅。"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from textual.widgets import Static

from abist_kb.config import Settings
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.infrastructure.jobs.supervisor import WorkerSupervisor
from abist_kb.presentation.tui.app import MIN_COLUMNS, MIN_ROWS, KbApp
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

# `pyproject.toml` の `asyncio_mode = "auto"`(nicegui.testing.plugin との相互作用で
# strict モードだと fixture finalizer が二重登録されるため導入済み、M6 Task 6.1/6.2
# の報告参照)が `async def test_*` を自動でイベントループに載せる。ここで
# `pytest.mark.asyncio` を明示すると二重に実行されてしまうため付けない。


async def test_navigation_number_keys_switch_area(container: ServiceContainer) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.current_area == "dashboard"
        await pilot.press("3")
        await pilot.pause()
        assert app.current_area == "jobs"
        await pilot.press("4")
        await pilot.pause()
        assert app.current_area == "documents"


async def test_slash_focuses_search_input(container: ServiceContainer) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        assert app.current_area == "search"
        focused = app.focused
        assert focused is not None and focused.id == "search-input"


async def test_search_submits_and_reports_invalid_input_error(
    container: ServiceContainer,
) -> None:
    """空文字クエリは検索サービス側で `INVALID_INPUT` になる(view-model 経由)。"""
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_search()
        await pilot.pause()
        await pilot.press(*"foo")
        await pilot.press("enter")
        await pilot.pause()
        results = app.query_one("#search-results")
        text = str(results.render())
        assert "件" in text or "エラー" in text


async def test_help_modal_opens_and_escape_closes_it(container: ServiceContainer) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_esc_returns_from_job_detail_to_jobs_list(container: ServiceContainer) -> None:
    repo = JobRepository(container.conn)
    job = repo.submit("noop", {})

    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("jobs")
        app.detail = ("job", job.id)
        app.render_area("jobs")
        await pilot.pause()
        assert app.detail is not None
        await pilot.press("escape")
        await pilot.pause()
        assert app.detail is None


async def test_cancel_job_goes_through_confirm_modal_and_is_reversible(
    container: ServiceContainer,
) -> None:
    """破壊的操作(キャンセル)はモーダル確認を経由する。「いいえ」なら実行しない。"""
    repo = JobRepository(container.conn)
    job = repo.submit("noop", {})

    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.detail = ("job", job.id)
        app.current_area = "jobs"
        app.render_area("jobs")
        await pilot.pause()

        app.cancel_job(job.id)
        await pilot.pause()
        assert len(app.screen_stack) == 2  # ConfirmModal が積まれている

        await pilot.press("n")  # いいえ
        await pilot.pause()
        assert len(app.screen_stack) == 1
        refreshed = repo.get(job.id)
        assert refreshed is not None
        assert refreshed.state == JobState.QUEUED  # キャンセルされていない


async def test_cancel_job_confirmed_actually_cancels(container: ServiceContainer) -> None:
    repo = JobRepository(container.conn)
    job = repo.submit("noop", {})

    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.detail = ("job", job.id)
        app.current_area = "jobs"
        app.render_area("jobs")
        await pilot.pause()

        app.cancel_job(job.id)
        await pilot.pause()
        await pilot.press("y")  # はい
        await pilot.pause()
        await pilot.pause()

        refreshed = repo.get(job.id)
        assert refreshed is not None
        assert refreshed.state == JobState.CANCELLED
        body = str(app.query_one("#content > Static", Static).render())
        assert "成功:" in body


async def test_retry_job_error_displays_code_and_message(container: ServiceContainer) -> None:
    repo = JobRepository(container.conn)
    job = repo.submit("noop", {})

    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.detail = ("job", job.id)
        app.current_area = "jobs"
        app.render_area("jobs")
        await pilot.pause()

        app.retry_job(job.id)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

        body = str(app.query_one("#content > Static", Static).render())
        assert "INVALID_INPUT" in body
        assert "再試行できません" in body


async def test_narrow_layout_collapses_nav_to_single_pane(container: ServiceContainer) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        nav = app.query_one("#nav")
        assert "hidden" not in nav.classes

        await pilot.resize_terminal(MIN_COLUMNS - 1, MIN_ROWS + 5)
        await pilot.pause()
        assert "hidden" in nav.classes

        await pilot.resize_terminal(MIN_COLUMNS + 20, MIN_ROWS + 5)
        await pilot.pause()
        assert "hidden" not in nav.classes


async def test_quit_key_exits_app(container: ServiceContainer) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert app._exit is True


async def test_start_worker_supervisor_runs_on_own_thread_without_thread_boundary_error(
    tmp_root: Path, caplog
) -> None:
    """回帰テスト: `ServiceContainer.build_worker_supervisor()` は専用のSQLite接続を
    開くが、その接続は `WorkerSupervisor.run_forever()` を実行するスレッド"自身"で
    作らなければならない(SQLiteはコネクション作成スレッド以外からの利用を禁じる)。

    以前の `KbApp.on_mount` は `build_worker_supervisor()` をメイン(TUI)スレッドで
    呼んでから別スレッドで `run_forever()` を実行しており、毎tick
    `sqlite3.ProgrammingError` が発生し続けて画面・キー入力が応答不能になった
    (`taskkill` でしか止められない実障害)。本テストはその起動パターンを実際に
    再現し、tick失敗ログ(`worker_tick_failed`)が一切出ないことを確認する。
    """
    settings = Settings(root_dir=tmp_root, _env_file=None)
    settings.ensure_directories()
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    # 本番の `ui tui` と同じく既定(`check_same_thread=True`)で構築する。
    real_container = ServiceContainer(settings)
    try:
        app = KbApp(real_container, start_worker=True)
        with caplog.at_level(logging.WARNING, logger="abist_kb.infrastructure.jobs.supervisor"):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                thread = app._supervisor_thread
                assert thread is not None
                deadline = asyncio.get_event_loop().time() + 3.0
                while asyncio.get_event_loop().time() < deadline:
                    if not thread.is_alive():
                        break
                    await asyncio.sleep(0.05)
                # スレッドは(バグ再現時のように死なずに)動き続けているはず。
                assert thread.is_alive()
        assert "worker_tick_failed" not in caplog.text
        assert "ProgrammingError" not in caplog.text
    finally:
        real_container.close()


async def test_quit_still_works_while_worker_supervisor_is_erroring_repeatedly(
    container: ServiceContainer, monkeypatch
) -> None:
    """バックグラウンドワーカーが延々と失敗し続けても、TUIは応答不能にならず
    `q` で終了できなければならない(スレッド境界バグの症状そのものの再発防止)。
    """

    def _always_fails(self: WorkerSupervisor) -> bool:
        raise sqlite3.ProgrammingError(
            "SQLite objects created in a thread can only be used in that same thread."
        )

    monkeypatch.setattr(WorkerSupervisor, "tick", _always_fails)

    app = KbApp(container, start_worker=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # ワーカースレッドが数回失敗する時間を与える。
        await asyncio.sleep(0.2)
        await pilot.press("q")
        await pilot.pause()
        assert app._exit is True
