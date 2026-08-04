"""Textual TUI のテスト(§13.2): ナビ・検索・モーダル・キャンセル・狭幅。"""

from __future__ import annotations

from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
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
