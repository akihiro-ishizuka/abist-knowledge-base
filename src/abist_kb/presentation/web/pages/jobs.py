"""画面3: ジョブ(§7.1)。実行中進捗、ログ、キャンセル、再実行。"""

from __future__ import annotations

from nicegui import ui

from abist_kb.presentation.console.theme import SemanticToken
from abist_kb.presentation.web.theme import badge_label, token_color
from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from ._confirm import confirm_dialog
from .layout import page_shell


def render_list(container: ServiceContainer) -> None:
    with page_shell("/jobs", title="ジョブ"):
        data = screens.jobs_list(container)
        for job in data["jobs"]:
            token = SemanticToken(job["state_token"])
            with ui.row().classes("items-center gap-2"):
                ui.badge(badge_label(token, job["state"]), color=token_color(token))
                ui.link(f"{job['kind']} ({job['id']})", f"/jobs/{job['id']}")


def render_detail(container: ServiceContainer, job_id: str) -> None:
    with page_shell("/jobs", title=f"ジョブ詳細: {job_id}"):
        data = screens.job_detail(container, job_id)
        if "error" in data:
            ui.label(data["error"]["message"]).classes("text-danger")
            return

        job = data["job"]
        token = SemanticToken(job["state_token"])
        with ui.row().classes("items-center gap-2"):
            ui.badge(badge_label(token, job["state"]), color=token_color(token))
            ui.label(job["kind"])

        result_area = ui.column().classes("w-full")

        def show_result(outcome: dict, *, success: str) -> None:
            result_area.clear()
            with result_area:
                if "error" in outcome:
                    error = outcome["error"]
                    ui.label(f"エラー: {error['code']}").classes("text-danger")
                    ui.label(error["message"]).classes("text-danger")
                else:
                    ui.label(success)

        async def do_cancel() -> None:
            if not await confirm_dialog(
                "キャンセルしますか?",
                detail=f"対象: ジョブ {job_id}",
            ):
                return
            outcome = screens.job_cancel(container, job_id, confirmed=True)
            state = outcome.get("job", {}).get("state")
            show_result(outcome, success=f"キャンセル要求: {state}")

        async def do_retry() -> None:
            if not await confirm_dialog(
                "再実行しますか?",
                detail=f"対象: ジョブ {job_id}",
            ):
                return
            outcome = screens.job_retry(container, job_id)
            retry_id = outcome.get("job", {}).get("id")
            show_result(outcome, success=f"再実行ジョブ: {retry_id}")

        with ui.row():
            ui.button("キャンセル", on_click=do_cancel)
            ui.button("再実行", on_click=do_retry)

        ui.label("進捗履歴").classes("text-md font-bold mt-4")
        columns = [
            {"name": "phase", "label": "工程", "field": "phase"},
            {"name": "current", "label": "完了数", "field": "current"},
            {"name": "total", "label": "全体", "field": "total"},
            {"name": "severity", "label": "重大度", "field": "severity"},
            {"name": "message", "label": "メッセージ", "field": "message"},
        ]
        ui.table(columns=columns, rows=data["history"], row_key="timestamp")


__all__ = ["render_detail", "render_list"]
