"""モーション演出の予算（純関数側）。

図に動きを付けると1シーンあたりの描画時間が伸びる。描画は RENDER リースで
直列化されるので、1シーンの増分が動画全体に効いてくる。**要素が増えたら
演出を自動的に落とす**ことで、最悪ケースの尺を抑える。

Manim を要する実際のアニメーションは `tools/visualize/templates/choreography.py`
にあり、こちらは判断だけを持つ（本 venv からテストできるように分けている）。
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def layout_module():
    from templates import layout  # conftest が sys.path を通している

    return layout


# -- モーション段の解決 -------------------------------------------------------------


def test_motion_levels_are_a_closed_set(layout_module) -> None:
    assert layout_module.MOTION_LEVELS == ("minimal", "standard", "rich")


def test_default_is_standard(layout_module) -> None:
    assert layout_module.resolve_motion(None, quality="standard", beat_count=5) == "standard"


def test_unknown_motion_falls_back_to_standard(layout_module) -> None:
    assert layout_module.resolve_motion("cinematic", quality="standard", beat_count=5) == "standard"


def test_draft_quality_drops_to_minimal(layout_module) -> None:
    """下書きは速さが要る。演出は確認の邪魔にしかならない。"""
    assert layout_module.resolve_motion("rich", quality="draft", beat_count=5) == "minimal"


def test_many_beats_drop_to_minimal(layout_module) -> None:
    assert layout_module.resolve_motion("standard", quality="standard", beat_count=25) == "minimal"


def test_rich_is_capped_by_beat_count(layout_module) -> None:
    """カメラを動かす rich は要素が多いと破綻する（寄れる先が無い）。"""
    assert layout_module.resolve_motion("rich", quality="standard", beat_count=8) == "rich"
    assert layout_module.resolve_motion("rich", quality="standard", beat_count=15) == "standard"


# -- 予算 ---------------------------------------------------------------------------


def test_minimal_disables_every_effect(layout_module) -> None:
    budget = layout_module.choreo_budget(5, "minimal")
    assert not budget["focus"]
    assert not budget["flow_pulse"]
    assert not budget["camera"]
    assert budget["extra_sec"] == 0.0


def test_standard_enables_focus_and_flow(layout_module) -> None:
    budget = layout_module.choreo_budget(5, "standard")
    assert budget["focus"] and budget["flow_pulse"]
    assert not budget["camera"], "カメラは rich だけ"
    assert budget["extra_sec"] > 0


def test_rich_enables_the_camera(layout_module) -> None:
    assert layout_module.choreo_budget(5, "rich")["camera"]


def test_focus_becomes_instant_when_beats_pile_up(layout_module) -> None:
    """要素が多いとアニメーションの焦点化はやめ、瞬時の減光に落とす。"""
    few = layout_module.choreo_budget(5, "standard")
    many = layout_module.choreo_budget(16, "standard")
    assert few["focus_run_time"] > 0
    assert many["focus_run_time"] == 0.0
    assert many["focus"], "減光そのものは残す（瞬時に切り替えるだけ）"


def test_flow_pulse_is_dropped_when_edges_pile_up(layout_module) -> None:
    assert layout_module.choreo_budget(5, "standard", edge_count=4)["flow_pulse"]
    assert not layout_module.choreo_budget(5, "standard", edge_count=20)["flow_pulse"]


def test_extra_time_grows_with_beats_but_stays_bounded(layout_module) -> None:
    small = layout_module.choreo_budget(3, "standard")["extra_sec"]
    large = layout_module.choreo_budget(30, "standard")["extra_sec"]
    assert large > small
    assert large <= layout_module.MAX_CHOREO_EXTRA_SEC


def test_budget_is_deterministic(layout_module) -> None:
    assert layout_module.choreo_budget(7, "standard", edge_count=5) == layout_module.choreo_budget(
        7, "standard", edge_count=5
    )
