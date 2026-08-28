"""chart シーン種別（数値を図として見せる）。

これまで数値は色つきテキストにしかならず、唯一のグラフは comparison_v1 の
隠し特殊ケース（観点がちょうど {計画, 実績} で中身が数値のとき）だけだった。
発見しようがないので、第一級の kind として出す。

**数値は必ず出典を伴う。** 既存の metric と同じ扱い（decorative では逃げられない）。
"""

from __future__ import annotations

from typing import Any

import pytest

from abist_kb.domain.scene_spec import (
    CHART_VARIANTS,
    MAX_CHART_POINTS,
    MAX_CHART_SERIES,
    SCENE_KINDS,
    validate_scene_spec,
)


def _series(label: str, *values: tuple[str, float], **extra: Any) -> dict[str, Any]:
    beat = {
        "type": "chart_series",
        "label": label,
        "values": [{"name": name, "value": value} for name, value in values],
        "source_refs": ["s1"],
    }
    beat.update(extra)
    return beat


def _spec(beats: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    spec = {
        "schema_version": "1.0",
        "scene_kind": "chart",
        "output_format": "mp4",
        "template": "chart_v1",
        "title": "工数の計画と実績",
        "chart": {"variant": "grouped_bar", "value_label": "工数"},
        "sources": [
            {
                "id": "s1",
                "path": "a/b.md",
                "start_line": 1,
                "end_line": 5,
                "content_hash": "0" * 64,
            }
        ],
        "beats": beats,
    }
    spec.update(overrides)
    return spec


# -- kind の登録 ---------------------------------------------------------------------


def test_chart_is_a_listed_scene_kind() -> None:
    kinds = {k["kind"] for k in SCENE_KINDS}
    assert "chart" in kinds


def test_chart_declares_its_beat_types() -> None:
    chart = next(k for k in SCENE_KINDS if k["kind"] == "chart")
    assert "chart_series" in chart["beat_types"]
    assert chart["template"] == "chart_v1"


# -- 受け入れ -----------------------------------------------------------------------


@pytest.mark.parametrize("variant", CHART_VARIANTS)
def test_each_variant_is_accepted(variant: str) -> None:
    spec = _spec([_series("案件A", ("計画", 40), ("実績", 36))], chart={"variant": variant})
    result = validate_scene_spec(spec)
    assert result.ok, [e.to_dict() for e in result.errors]


def test_unit_is_carried() -> None:
    spec = _spec([_series("案件A", ("計画", 40), ("実績", 36), unit="h")])
    assert validate_scene_spec(spec).ok


# -- 拒否 ---------------------------------------------------------------------------


def test_numbers_without_a_source_are_rejected() -> None:
    """数値こそ出典が要る（グラフは「それらしく」見えてしまう）。"""
    beat = _series("案件A", ("計画", 40))
    beat.pop("source_refs")
    result = validate_scene_spec(_spec([beat]))
    assert not result.ok


def test_decorative_cannot_bypass_the_source_requirement() -> None:
    beat = _series("案件A", ("計画", 40))
    beat.pop("source_refs")
    beat["decorative"] = True
    assert not validate_scene_spec(_spec([beat])).ok


def test_unknown_variant_is_rejected() -> None:
    result = validate_scene_spec(
        _spec([_series("案件A", ("計画", 40))], chart={"variant": "radar"})
    )
    assert not result.ok
    assert any(e.path == "chart.variant" for e in result.errors)


def test_too_many_series_is_rejected() -> None:
    beats = [_series(f"案件{i}", ("計画", 10)) for i in range(MAX_CHART_SERIES + 1)]
    result = validate_scene_spec(_spec(beats))
    assert not result.ok
    assert any("chart_series" in e.path or e.path == "beats" for e in result.errors)


def test_too_many_points_on_a_line_is_rejected() -> None:
    values = tuple((f"{i}月", float(i)) for i in range(MAX_CHART_POINTS + 1))
    result = validate_scene_spec(_spec([_series("推移", *values)], chart={"variant": "line"}))
    assert not result.ok


def test_non_numeric_values_are_rejected() -> None:
    beat = {
        "type": "chart_series",
        "label": "案件A",
        "values": [{"name": "計画", "value": "たくさん"}],
        "source_refs": ["s1"],
    }
    assert not validate_scene_spec(_spec([beat])).ok


def test_negative_values_are_rejected() -> None:
    """棒の長さで表す以上、負の値は描けない（見せかけの図になる）。"""
    assert not validate_scene_spec(_spec([_series("案件A", ("計画", -5))])).ok


def test_empty_series_is_rejected() -> None:
    beat = {"type": "chart_series", "label": "案件A", "values": [], "source_refs": ["s1"]}
    assert not validate_scene_spec(_spec([beat])).ok


# -- テンプレート側 -------------------------------------------------------------------


def test_template_file_exists() -> None:
    from pathlib import Path

    root = Path(__file__).parents[2]
    assert (root / "tools" / "visualize" / "templates" / "chart_v1.py").is_file()


def test_template_is_registered_in_the_renderer() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[2] / "tools" / "visualize" / "render_scene.py").read_text(
        encoding="utf-8"
    )
    assert '"chart_v1"' in source


# -- 実レンダリング（variant ごと） -----------------------------------------------------
#
# 検証を通ることと描けることは別。実際 `line` は検証を通ったまま描画で落ちていた
# （手元で描いたのが grouped_bar だけだったため気付けなかった）。

import os  # noqa: E402
from pathlib import Path  # noqa: E402

_render = pytest.mark.skipif(
    os.environ.get("KB_RUN_MANIM_TESTS") != "1",
    reason="実 Manim / ffmpeg を伴うため KB_RUN_MANIM_TESTS=1 で実行",
)


@_render
@pytest.mark.parametrize("variant", CHART_VARIANTS)
def test_every_variant_actually_renders(variant: str, tmp_path: Path) -> None:
    """**4つとも実際に描けること。** 検証を通っただけでは描けるとは限らない。"""
    from abist_kb.application.visualization.renderer import render_scene
    from abist_kb.domain.line_range import range_hash

    docs = tmp_path / "docs"
    docs.mkdir()
    body = "\n".join(f"- 行{n}" for n in range(1, 21))
    (docs / "src.md").write_text(body, encoding="utf-8")

    spec = _spec(
        [
            _series("案件A", ("1月", 40), ("2月", 36), ("3月", 28)),
            _series("案件B", ("1月", 32), ("2月", 35), ("3月", 30)),
        ],
        chart={"variant": variant, "value_label": "単位: 時間"},
        output_format="png",
    )
    spec["sources"] = [
        {
            "id": "s1",
            "path": "src.md",
            "start_line": 1,
            "end_line": 20,
            "content_hash": range_hash(body, 1, 20).hash,
        }
    ]

    outcome = render_scene(
        spec, docs_dir=docs, reports_dir=tmp_path / "out", repo_root=Path.cwd()
    )

    assert outcome.ok, (variant, outcome.code, outcome.errors)
    assert Path(outcome.outputs[0]["path"]).stat().st_size > 1000


# -- 折れ線の目盛り範囲 ---------------------------------------------------------------
#
# 実際に動画を1本作って見つかった不備。315〜408 秒の推移を 0 起点で描くと
# 変化が上端2割に潰れ、**そのシーンが言いたいこと（50点が最速）が見えない**。
# 一方、0 起点をやめるなら目盛りの値を出さないと誇張になる。


class TestLineChartRange:
    @pytest.fixture(scope="class")
    def layout_module(self):
        from templates import layout

        return layout

    def test_a_narrow_band_of_values_fills_the_plot(self, layout_module) -> None:
        lo, hi = layout_module.line_chart_range([328, 320, 315, 327, 334, 408])
        span = hi - lo
        assert (315 - lo) / span < 0.2, "最小値が下端付近に来ること"
        assert (408 - lo) / span > 0.8, "最大値が上端付近に来ること"

    def test_the_range_contains_every_value(self, layout_module) -> None:
        values = [328, 320, 315, 327, 334, 408]
        lo, hi = layout_module.line_chart_range(values)
        assert lo < min(values) and hi > max(values)

    def test_values_near_zero_keep_a_zero_baseline(self, layout_module) -> None:
        """0 に近い値まで下がるなら 0 起点のままにする（誇張を避ける）。"""
        lo, _ = layout_module.line_chart_range([0.4, 2.0, 5.0])
        assert lo == 0.0

    def test_a_flat_series_does_not_collapse(self, layout_module) -> None:
        lo, hi = layout_module.line_chart_range([100, 100, 100])
        assert hi > lo

    def test_an_empty_series_is_safe(self, layout_module) -> None:
        lo, hi = layout_module.line_chart_range([])
        assert hi > lo

    def test_the_range_is_deterministic(self, layout_module) -> None:
        values = [328, 320, 315, 327, 334, 408]
        assert layout_module.line_chart_range(values) == layout_module.line_chart_range(values)

    def test_the_ends_are_round_numbers(self, layout_module) -> None:
        """軸に「303.84秒」と出ると、読み手には不具合に見える。"""
        for values in (
            [328, 320, 315, 327, 334, 408],
            [5.3, 4.7, 3.7],
            [1200, 1180, 1340],
            [0.42, 0.51, 0.48],
            [0.4, 2.0, 5.0],  # 0 起点の枝も丸める
        ):
            lo, hi = layout_module.line_chart_range(values)
            for end in (lo, hi):
                assert end == float(f"{end:.4g}"), (values, end)

    def test_round_ends_still_contain_every_value(self, layout_module) -> None:
        for values in ([328, 320, 315, 327, 334, 408], [5.3, 4.7, 3.7], [0.42, 0.51, 0.48]):
            lo, hi = layout_module.line_chart_range(values)
            assert lo <= min(values) and hi >= max(values), values
