"""`CONCURRENT_RENDER` と「描画後のリース喪失」の区別。

この経路はこれまでテストが1本も無く（`grep -rn "CONCURRENT_RENDER" tests/` は
fixture の description 文字列しかヒットしなかった）、唯一の排他機構が
無検証のまま残っていた。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from abist_kb.application.visualization.renderer import RenderOutcome
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import ResourceKind
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.infrastructure.jobs.leases import acquire_resource_lease
from abist_kb.presentation.mcp.kb_visualize import KbVisualizeTools


def _payload(result: Any) -> dict[str, Any]:
    import json

    return json.loads(result.content[0].text)


@pytest.fixture
def tools_factory(tmp_path: Path):
    """`KbVisualizeTools` と、同じ DB を指す別接続を作るファクトリ。"""
    conns: list[Any] = []

    def make(render_fn: Any, *, ttl: float = 30.0) -> tuple[KbVisualizeTools, Any]:
        conn = open_app_db(tmp_path / "app.sqlite")
        conns.append(conn)
        docs = tmp_path / "docs"
        docs.mkdir(exist_ok=True)
        tools = KbVisualizeTools(
            conn,
            docs_dir=docs,
            reports_dir=tmp_path / "reports" / "visualizations",
            repo_root=tmp_path,
            lease_ttl_seconds=ttl,
            render_scene_fn=render_fn,
        )
        return tools, conn

    yield make
    for conn in conns:
        conn.close()


def _minimal_spec() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "png",
        "template": "step_explanation",
        "title": "排他の検証",
        "sources": [],
        "beats": [{"type": "statement", "text": "装飾", "decorative": True}],
    }


def test_concurrent_render_is_reported_without_invoking_the_renderer(tools_factory) -> None:
    """リースが他所に保持されている間は、レンダラを**呼ばずに**busy を返すこと。

    「取得前に弾いている」ことの証拠。呼んでしまうと Manim が2本走る。
    """
    calls: list[dict] = []

    def render_fn(spec, **kwargs):
        calls.append(kwargs)
        return RenderOutcome(ok=True, visualization_id="x")

    tools, conn = tools_factory(render_fn)
    holder = open_app_db(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    try:
        with acquire_resource_lease(
            holder, ResourceKind.RENDER, owner_id=str(uuid.uuid4()), ttl_seconds=30.0
        ):
            result = tools.render_scene({"sceneSpec": _minimal_spec()})
        payload = _payload(result)
    finally:
        holder.close()

    assert result.isError is True
    assert payload["code"] == "CONCURRENT_RENDER"
    assert calls == [], "リースが取れていないのにレンダラを呼んでいる"


def test_render_succeeds_after_the_lease_is_released(tools_factory) -> None:
    def render_fn(spec, **kwargs):
        return RenderOutcome(
            ok=True,
            visualization_id="20260101T000000Z-x-0000",
            output_dir=kwargs["reports_dir"] / "20260101T000000Z-x-0000",
            manifest_path=kwargs["reports_dir"] / "20260101T000000Z-x-0000" / "manifest.json",
            outputs=[],
        )

    tools, conn = tools_factory(render_fn)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    holder = open_app_db(db_path)
    try:
        with acquire_resource_lease(
            holder, ResourceKind.RENDER, owner_id=str(uuid.uuid4()), ttl_seconds=30.0
        ):
            assert _payload(tools.render_scene({"sceneSpec": _minimal_spec()}))["code"] == (
                "CONCURRENT_RENDER"
            )
        # 解放後は成功する
        payload = _payload(tools.render_scene({"sceneSpec": _minimal_spec()}))
    finally:
        holder.close()
    assert payload["ok"] is True


def test_lease_lost_after_render_keeps_the_outcome(tools_factory, monkeypatch) -> None:
    """描画後にリースを喪失しても成果物を捨てないこと。

    `held_resource_lease` は `with` を正常に抜ける際、更新失敗があれば
    `AppError(CONFLICT)` を送出する。これを無条件に `CONCURRENT_RENDER` へ
    変換すると「レンダリングは成功したのに busy と報告する」ことになる。
    `outcome is None`（本来の busy）と区別できていることを固定する。
    """
    produced = RenderOutcome(
        ok=True,
        visualization_id="20260101T000000Z-y-0000",
        output_dir=None,
        manifest_path=None,
        outputs=[],
        warnings=[],
    )

    def render_fn(spec, **kwargs):
        return produced

    tools, _conn = tools_factory(render_fn)

    import abist_kb.presentation.mcp.kb_visualize as module

    real = module.held_resource_lease

    class _LosingLease:
        """本体は通すが、抜ける際に CONFLICT を出すコンテキスト。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._inner = real(*args, **kwargs)

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, exc_type, exc, tb):
            self._inner.__exit__(exc_type, exc, tb)
            if exc_type is None:
                raise AppError(code=ErrorCode.CONFLICT, message="render リースを喪失しました")
            return False

    monkeypatch.setattr(module, "held_resource_lease", _LosingLease)

    result = tools.render_scene({"sceneSpec": _minimal_spec()})
    payload = _payload(result)

    assert payload["ok"] is True, "描画は成功しているのに busy を返している"
    assert any("喪失" in w for w in payload["warnings"]), payload["warnings"]
    # 元の outcome を壊していないこと（replace でコピーしている）
    assert produced.warnings == []


def test_non_conflict_app_errors_are_not_swallowed(tools_factory, monkeypatch) -> None:
    def render_fn(spec, **kwargs):
        raise AppError(code=ErrorCode.CONFIG_ERROR, message="設定が壊れています")

    tools, _conn = tools_factory(render_fn)
    with pytest.raises(AppError) as excinfo:
        tools.render_scene({"sceneSpec": _minimal_spec()})
    assert excinfo.value.code is ErrorCode.CONFIG_ERROR


def test_replace_is_used_for_warning_injection() -> None:
    """dataclass が frozen なので replace でコピーする必要がある(退行防止)。"""
    outcome = RenderOutcome(ok=True, warnings=["a"])
    updated = replace(outcome, warnings=[*outcome.warnings, "b"])
    assert outcome.warnings == ["a"]
    assert updated.warnings == ["a", "b"]
