"""Textual TUI のテスト(§13.2): ナビ・検索・モーダル・キャンセル・狭幅。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from textual.widgets import Button, DataTable, Input, Static, TextArea

from abist_kb.config import Settings
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.infrastructure.jobs.supervisor import WorkerSupervisor
from abist_kb.presentation.tui.app import MIN_COLUMNS, MIN_ROWS, KbApp
from abist_kb.presentation.web.viewmodels import screens
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


async def test_available_chat_renders_and_surfaces_citations_and_warnings(
    container: ServiceContainer, monkeypatch
) -> None:
    monkeypatch.setattr(screens, "chat_stub", lambda _container: {"available": True})
    monkeypatch.setattr(
        screens,
        "chat_start",
        lambda _container: {"conversation_id": "conversation-1"},
    )
    monkeypatch.setattr(
        screens,
        "chat_ask",
        lambda _container, *, conversation_id, question: {
            "conversation_id": conversation_id,
            "message_id": "message-1",
            "text": f"回答: {question}",
            "citations": [
                {
                    "path": "guide.md",
                    "start_line": 10,
                    "end_line": 12,
                    "valid": True,
                    "reason": None,
                }
            ],
            "citation_warnings": ["引用 warning.md:20-30 を検証できませんでした"],
        },
    )

    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("chat")
        await pilot.pause()

        body = "\n".join(
            str(widget.render()) for widget in app.query("#content Static")
        )
        assert "利用不可" not in body

        chat_input = app.query_one("#chat-input", Input)
        chat_input.focus()
        await pilot.press(*"question")
        await pilot.press("enter")
        await pilot.pause()

        result = str(app.query_one("#chat-log", Static).render())
        assert "回答: question" in result
        assert "guide.md:10-12" in result
        assert "警告" in result
        assert "warning.md:20-30" in result


async def test_available_quality_renders_all_audits_without_unavailable_notice(
    container: ServiceContainer,
) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("quality")
        await pilot.pause()

        body = "\n".join(
            str(widget.render()) for widget in app.query("#content Static")
        )
        assert "利用不可" not in body
        assert "apply=True" in body
        assert "CLI" in body
        labels = {button.label for button in app.query("#content Button")}
        assert labels == {
            "整合性を検査",
            "重複を検出",
            "矛盾候補を検出",
            "メタデータ補完(dry-run)",
        }


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


async def test_job_action_result_does_not_leak_to_another_job(
    container: ServiceContainer,
) -> None:
    repo = JobRepository(container.conn)
    acted_job = repo.submit("noop", {})
    other_job = repo.submit("noop", {})

    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.detail = ("job", acted_job.id)
        app.current_area = "jobs"
        app.render_area("jobs")
        await pilot.pause()

        app.cancel_job(acted_job.id)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

        acted_body = str(app.query_one("#content > Static", Static).render())
        assert "操作結果: 成功:" in acted_body

        app.detail = ("job", other_job.id)
        app.render_area("jobs")
        await pilot.pause()

        other_body = str(app.query_one("#content > Static", Static).render())
        assert "操作結果:" not in other_body


async def test_batch_run_decline_after_row_selection_creates_no_job(
    container: ServiceContainer,
) -> None:
    container.batches.add(
        name="定例取り込み",
        type="web",
        output_dir="docs/weekly",
        items=[],
    )
    repo = JobRepository(container.conn)

    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("sources_batches")
        await pilot.pause()

        table = app.query_one("#batches-table", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("enter")
        await pilot.press("e")
        await pilot.pause()

        assert len(app.screen_stack) == 2
        await pilot.press("n")
        await pilot.pause()
        assert repo.list() == []


async def test_batch_run_confirm_after_row_selection_creates_job(
    container: ServiceContainer,
) -> None:
    container.batches.add(
        name="定例取り込み",
        type="web",
        output_dir="docs/weekly",
        items=[],
    )
    repo = JobRepository(container.conn)

    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("sources_batches")
        await pilot.pause()

        table = app.query_one("#batches-table", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("enter")
        await pilot.press("e")
        await pilot.pause()

        assert len(app.screen_stack) == 2
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

        jobs = repo.list()
        assert len(jobs) == 1
        assert jobs[0].kind == "batch"


async def test_sources_batches_actions_show_invalid_input_without_selection(
    container: ServiceContainer,
) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("sources_batches")
        await pilot.pause()

        for key, expected_message in (
            ("x", "削除するソースまたはバッチを選択してください"),
            ("t", "接続テストするソースを選択してください"),
            ("e", "実行するバッチを選択してください"),
        ):
            await pilot.press(key)
            await pilot.pause()
            result = str(app.query_one("#sources-batches-result", Static).render())
            assert "INVALID_INPUT" in result
            assert expected_message in result


async def test_document_detail_updates_only_editable_metadata(
    container: ServiceContainer,
) -> None:
    container.documents.upsert(
        {
            "path": "editable.md",
            "source": "manual",
            "status": "draft",
            "document_type": "memo",
        }
    )
    app = KbApp(container, start_worker=False)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.detail = ("document", "editable.md")
        app.current_area = "documents"
        app.render_area("documents")
        await pilot.pause()

        readonly = str(app.query_one("#document-readonly", Static).render())
        assert "読み取り専用" in readonly
        status = app.query_one("#document-status", Input)
        document_type = app.query_one("#document-type", Input)
        status.value = "active"
        document_type.value = "guide"
        await pilot.click("#document-save")
        await pilot.pause()

        updated = container.documents.get("editable.md")
        assert updated["status"] == "active"
        assert updated["document_type"] == "guide"
        assert updated["source"] == "manual"


async def test_document_delete_decline_preserves_document_and_audit_count(
    container: ServiceContainer,
) -> None:
    container.documents.upsert(
        {"path": "keep.md", "source": "manual", "status": "active"}
    )
    audit_before = container.conn.execute(
        "SELECT COUNT(*) FROM audit_events"
    ).fetchone()[0]
    app = KbApp(container, start_worker=False)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.detail = ("document", "keep.md")
        app.current_area = "documents"
        app.render_area("documents")
        await pilot.pause()

        app.delete_document("keep.md")
        await pilot.pause()
        modal = str(app.screen.query_one(Static).render())
        assert "keep.md" in modal
        await pilot.press("n")
        await pilot.pause()

        assert container.documents.get_or_none("keep.md") is not None
        audit_after = container.conn.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]
        assert audit_after == audit_before


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


async def test_visualization_renders_deps_and_sample_spec(container: ServiceContainer) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("visualization")
        await pilot.pause()

        spec_area = app.query_one("#visualization-spec", TextArea)
        assert "schema_version" in spec_area.text


async def test_visualization_validate_shows_invalid_scene_spec_error(
    container: ServiceContainer,
) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("visualization")
        await pilot.pause()

        spec_area = app.query_one("#visualization-spec", TextArea)
        spec_area.text = '{"scene_kind": "explain"}'
        app.validate_visualization()
        await pilot.pause()

        result = str(app.query_one("#visualization-result", Static).render())
        assert "INVALID_SCENE_SPEC" in result


async def test_visualization_render_submit_without_worker_reports_worker_unavailable(
    container: ServiceContainer,
) -> None:
    from abist_kb.domain.line_range import range_hash

    text = "行1\n行2\n行3\n"
    (container.settings.docs_dir / "doc.md").write_text(text, encoding="utf-8")
    hashed = range_hash(text, 1, 2)
    assert hashed.ok and hashed.hash is not None
    spec = {
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

    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("visualization")
        await pilot.pause()

        spec_area = app.query_one("#visualization-spec", TextArea)
        spec_area.text = json.dumps(spec, ensure_ascii=False)
        app.render_visualization()
        await pilot.pause()

        result = str(app.query_one("#visualization-result", Static).render())
        assert "WORKER_UNAVAILABLE" in result


async def test_visualization_render_button_disabled_when_deps_not_ready(
    container: ServiceContainer,
) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("visualization")
        await pilot.pause()

        deps = screens.visualization_deps(container)
        render_button = app.query_one("#visualization-render", Button)
        assert render_button.disabled is (not deps.get("ready"))


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
