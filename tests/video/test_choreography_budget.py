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


# -- 減光の復元 ------------------------------------------------------------------
#
# 実際に動画を1本作って見つかった不備。`unfocus_all` が `set_opacity(1.0)` を
# 呼んでおり、Manim の `set_opacity` は**塗りと線の両方**を 1.0 にする。
# 塗らない前提の図形（折れ線、`progress` variant の目標枠）が、シーンの最後で
# 塗り潰されていた。減光は「戻す」ものであって「1.0 にする」ものではない。


class _FakeMobject:
    """`fill_opacity` / `stroke_opacity` と set 系だけを持つ最小の代役。"""

    def __init__(self, fill: float, stroke: float = 1.0) -> None:
        self.fill_opacity = fill
        self.stroke_opacity = stroke

    def set_fill(self, opacity: float) -> None:
        self.fill_opacity = opacity

    def set_stroke(self, opacity: float) -> None:
        self.stroke_opacity = opacity


@pytest.fixture(scope="module")
def memo_cls(layout_module):
    return layout_module.OpacityMemo


def test_restoring_brings_back_the_original_fill(memo_cls) -> None:
    """塗らない図形は、減光から戻しても塗らないまま。"""
    polyline = _FakeMobject(fill=0.0)
    memo = memo_cls()

    memo.dim(polyline, 0.25)
    assert polyline.fill_opacity == 0.0, "塗らない図形は減光しても塗られない"
    assert polyline.stroke_opacity == 0.25, "線は沈む"
    memo.restore(polyline)

    assert polyline.fill_opacity == 0.0
    assert polyline.stroke_opacity == 1.0


def test_a_half_filled_shape_keeps_its_own_opacity(memo_cls) -> None:
    """`progress` variant の目標枠（薄い塗り）を塗り潰さない。"""
    frame_bar = _FakeMobject(fill=0.12)
    memo = memo_cls()
    memo.dim(frame_bar, 0.25)
    memo.restore(frame_bar)
    assert frame_bar.fill_opacity == pytest.approx(0.12)


def test_restoring_something_never_dimmed_changes_nothing(memo_cls) -> None:
    """減光していない図形には触らない（演出を切ったときに壊さない）。"""
    shape = _FakeMobject(fill=0.0, stroke=0.4)
    memo_cls().restore(shape)
    assert (shape.fill_opacity, shape.stroke_opacity) == (0.0, 0.4)


def test_dimming_twice_still_restores_the_first_value(memo_cls) -> None:
    """減光を重ねても、覚えるのは最初の値だけ。"""
    shape = _FakeMobject(fill=0.0)
    memo = memo_cls()
    memo.dim(shape, 0.25)
    memo.dim(shape, 0.1)
    memo.restore(shape)
    assert shape.fill_opacity == 0.0


def test_target_opacity_scales_the_original(memo_cls) -> None:
    """減光は「相対的に沈める」。元が薄いものを濃くしてはいけない。"""
    memo = memo_cls()
    assert memo.dim_targets(_FakeMobject(fill=0.0), 0.25) == (0.0, 0.25)
    assert memo.dim_targets(_FakeMobject(fill=1.0), 0.25) == (0.25, 0.25)
    assert memo.dim_targets(_FakeMobject(fill=0.12), 0.25) == pytest.approx((0.03, 0.25))


def test_dimming_twice_does_not_compound(memo_cls) -> None:
    """減光を重ねても濃さは変わらない。

    beat ごとに `focus` を呼ぶと、既に沈めた要素をもう一度沈めることになる。
    **現在値**を基準に掛け算すると 0.25 → 0.0625 → … と消えていき、
    比較表の中身が画面から消える（実際に動画で消えた）。
    """
    shape = _FakeMobject(fill=1.0)
    memo = memo_cls()
    memo.dim(shape, 0.25)
    memo.dim(shape, 0.25)
    memo.dim(shape, 0.25)
    assert shape.fill_opacity == pytest.approx(0.25)
    assert shape.stroke_opacity == pytest.approx(0.25)


def test_dim_targets_are_computed_from_the_original(memo_cls) -> None:
    shape = _FakeMobject(fill=1.0)
    memo = memo_cls()
    memo.dim(shape, 0.25)
    assert memo.dim_targets(shape, 0.25) == pytest.approx((0.25, 0.25))


def test_remember_does_not_change_the_mobject(memo_cls) -> None:
    """アニメーションで沈める経路は、控えるだけで値は動かさない。"""
    shape = _FakeMobject(fill=1.0)
    memo = memo_cls()
    memo.remember(shape)
    assert shape.fill_opacity == 1.0
    memo.restore(shape)
    assert shape.fill_opacity == 1.0


def test_focusing_brings_the_current_item_back(memo_cls) -> None:
    """注目させる要素は、前の beat で沈めていても元へ戻す。

    戻さないと、2つ目以降の beat では「全部同じ濃さ」になり、focus が
    何も強調しない演出になる。
    """
    items = [_FakeMobject(fill=1.0) for _ in range(3)]
    memo = memo_cls()

    for current in items:  # beat ごとに注目先が移る
        memo.restore(current)
        for other in items:
            if other is not current:
                memo.dim(other, 0.25)
        assert current.fill_opacity == 1.0, "注目中の要素が沈んだまま"
        assert all(o.fill_opacity == pytest.approx(0.25) for o in items if o is not current)


class _FakeGroup:
    """`submobjects` を持つ入れ物（Manim の VGroup / Text 相当）。

    **入れ物自身は塗りを持たない**（`fill_opacity` は 0.0）。ここを基準に
    減光すると、戻すときに中身がまとめて透明になる（比較表の中身が消えた）。
    """

    def __init__(self, *children) -> None:
        self.submobjects = list(children)
        self.fill_opacity = 0.0
        self.stroke_opacity = 0.0

    def set_fill(self, opacity: float) -> None:
        self.fill_opacity = opacity
        for child in self.submobjects:
            child.set_fill(opacity=opacity)

    def set_stroke(self, opacity: float) -> None:
        self.stroke_opacity = opacity
        for child in self.submobjects:
            child.set_stroke(opacity=opacity)


def test_a_group_restores_each_child_to_its_own_opacity(memo_cls) -> None:
    text, polyline = _FakeMobject(fill=1.0), _FakeMobject(fill=0.0)
    group = _FakeGroup(text, polyline)
    memo = memo_cls()

    memo.dim(group, 0.25)
    memo.restore(group)

    assert text.fill_opacity == 1.0, "入れ物ごしに戻すと中身が消える"
    assert polyline.fill_opacity == 0.0
    assert text.stroke_opacity == 1.0


def test_a_group_is_dimmed_through_its_children(memo_cls) -> None:
    text = _FakeMobject(fill=1.0)
    memo = memo_cls()
    memo.dim(_FakeGroup(text), 0.25)
    assert text.fill_opacity == pytest.approx(0.25)


def test_nested_groups_are_reached(memo_cls) -> None:
    leaf = _FakeMobject(fill=1.0)
    memo = memo_cls()
    outer = _FakeGroup(_FakeGroup(leaf))
    memo.dim(outer, 0.25)
    assert leaf.fill_opacity == pytest.approx(0.25)
    memo.restore(outer)
    assert leaf.fill_opacity == 1.0


def test_reset_drops_the_memo(memo_cls) -> None:
    """シーン境界で控えを捨てる（`id()` の再利用で別物を戻さないため）。"""
    shape = _FakeMobject(fill=1.0)
    memo = memo_cls()
    memo.dim(shape, 0.25)
    memo.reset()
    memo.restore(shape)
    assert shape.fill_opacity == pytest.approx(0.25), "捨てた控えで戻してはいけない"
