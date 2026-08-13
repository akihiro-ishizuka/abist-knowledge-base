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
