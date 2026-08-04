"""画面7: 可視化(§7.1)。`render_scene` は永続ジョブ(設計書 §10)として投入する。

検証(`visualization_validate`)とレンダリング投入(`visualization_submit_render`)は
別操作にする — 検証は外部依存なしで即座に返るが、レンダリングは分単位かかり
Manim/ffmpeg を要する。依存(`visualization_deps`)を画面上部に表示し、
`ready: false` ならレンダリングボタンを無効化して不足物を名指しする
(押せてしまって数分後に失敗するのが最悪の体験なため、事前に分かる形にする)。
"""

from __future__ import annotations

import json
from typing import Any

from nicegui import ui

from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

from .layout import page_shell

_SAMPLE_SPEC = {
    "schema_version": "1.0",
    "scene_kind": "explain",
    "output_format": "mp4",
    "template": "step_explanation",
    "title": "サンプルシーン",
    "sources": [],
    "beats": [],
}


def _deps_summary(deps: dict[str, Any]) -> str:
    if deps.get("ready"):
        return "レンダリング可能です(Python・Manim・ffmpeg すべて検出済み)。"
    messages = deps.get("messages") or []
    return "レンダリング不可: " + ("\n".join(messages) if messages else "依存が不足しています。")


def render(container: ServiceContainer) -> None:
    with page_shell("/visualization", title="可視化"):
        ui.label("可視化(SceneSpec のレンダリング)").classes("text-md font-bold")

        deps = screens.visualization_deps(container)
        ui.label(_deps_summary(deps)).classes(
            "text-sm" if deps.get("ready") else "text-sm text-danger"
        )

        spec_input = (
            ui.textarea(
                "SceneSpec (JSON)",
                value=json.dumps(_SAMPLE_SPEC, ensure_ascii=False, indent=2),
            )
            .classes("w-full font-mono")
            .props("rows=16")
            .mark("visualization-spec")
        )

        result_area = ui.column().classes("w-full")

        def _parse_spec() -> dict[str, Any] | None:
            try:
                parsed = json.loads(spec_input.value or "{}")
            except json.JSONDecodeError as exc:
                result_area.clear()
                with result_area:
                    ui.label(f"SceneSpec の JSON パースに失敗しました: {exc}").classes(
                        "text-danger"
                    )
                return None
            if not isinstance(parsed, dict):
                result_area.clear()
                with result_area:
                    ui.label("SceneSpec は JSON オブジェクトで指定してください。").classes(
                        "text-danger"
                    )
                return None
            return parsed

        def _show_validation(outcome: dict[str, Any]) -> None:
            result_area.clear()
            with result_area:
                if outcome.get("ok"):
                    ui.label("検証OK: レンダリング可能です。").classes("text-positive")
                    for warning in outcome.get("warnings") or []:
                        ui.label(f"警告: {warning}").classes("text-sm text-warning")
                    return
                ui.label(f"検証エラー: {outcome.get('code')}").classes("text-danger")
                for error in outcome.get("errors") or []:
                    ui.label(f"  {error['path']}: {error['message']}").classes(
                        "text-sm text-danger"
                    )

        def do_validate() -> None:
            spec = _parse_spec()
            if spec is None:
                return
            _show_validation(screens.visualization_validate(container, spec))

        def do_render() -> None:
            spec = _parse_spec()
            if spec is None:
                return
            outcome = screens.visualization_submit_render(container, spec)
            result_area.clear()
            with result_area:
                if "error" in outcome:
                    error = outcome["error"]
                    ui.label(f"エラー: {error['code']}").classes("text-danger")
                    ui.label(error["message"]).classes("text-danger")
                    return
                job = outcome["job"]
                ui.label(f"レンダリングを投入しました: state={job['state']}")
                ui.link(f"ジョブ詳細を見る ({job['id']})", f"/jobs/{job['id']}")

        with ui.row().classes("gap-2"):
            ui.button("検証", on_click=do_validate)
            render_button = ui.button("レンダリング", on_click=do_render).mark(
                "visualization-render-button"
            )
            if not deps.get("ready"):
                render_button.props("disable")
                render_button.tooltip("依存が不足しているため無効です。上部の診断を確認してください。")


__all__ = ["render"]
