"""構成の単調さを機械が指摘する。

QA は技術的な正しさ（解像度・fps・焼き込み・出典・秘密）しか見ておらず、
**動画として単調かどうかを誰も見ていなかった**。実際、手書きの高品質台本でさえ
14シーン中 10 が key_points で、同じ種別が 6 連続していた。

判定は spec の scenes[] だけで完結する純関数にする。描く前（validate_video_script）と
描いた後（QA）の両方から同じ関数を呼ぶため。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from abist_kb.application.video.composition import (
    CARD_RATIO_LIMIT,
    MAX_SAME_KIND_RUN,
    MIN_DISTINCT_KINDS,
    MIN_SCENES_FOR_VARIETY,
    score_composition,
)

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"


def _scenes(*kinds: str) -> list[dict[str, Any]]:
    return [{"id": f"s{i:02d}", "kind": kind} for i, kind in enumerate(kinds, start=1)]


def _codes(scenes: list[dict[str, Any]], **kwargs: Any) -> set[str]:
    return {f["code"] for f in score_composition(scenes, **kwargs).findings}


# -- 指標そのもの -------------------------------------------------------------------


def test_counts_distinct_kinds() -> None:
    report = score_composition(_scenes("title", "key_points", "timeline", "key_points"))
    assert report.distinct_kinds == 3


def test_counts_the_longest_run_of_the_same_kind() -> None:
    report = score_composition(
        _scenes("title", "key_points", "key_points", "key_points", "timeline", "timeline")
    )
    assert report.longest_same_run == 3


def test_card_ratio_counts_scenes_without_a_diagram() -> None:
    """図・グラフを伴わないカードだけの割合。"""
    report = score_composition(_scenes("title", "key_points", "flow", "chart"))
    assert report.card_ratio == pytest.approx(0.5)


def test_an_empty_script_is_not_an_error() -> None:
    report = score_composition([])
    assert report.findings == []
    assert report.distinct_kinds == 0


# -- 単調さの検出 -------------------------------------------------------------------


def test_a_long_run_of_the_same_kind_is_flagged() -> None:
    scenes = _scenes("title", *(["key_points"] * (MAX_SAME_KIND_RUN + 1)), "summary")
    assert "MONOTONOUS_RUN" in _codes(scenes)


def test_a_run_at_the_limit_is_not_flagged() -> None:
    scenes = _scenes("title", *(["key_points"] * MAX_SAME_KIND_RUN), "timeline", "summary")
    assert "MONOTONOUS_RUN" not in _codes(scenes)


def test_too_few_kinds_is_flagged_once_the_video_is_long_enough() -> None:
    scenes = _scenes(*(["key_points", "summary"] * (MIN_SCENES_FOR_VARIETY // 2 + 1)))
    assert "TOO_FEW_SCENE_KINDS" in _codes(scenes)


def test_a_short_video_may_be_uniform() -> None:
    """短い動画で種別が少ないのは単調ではなく妥当。"""
    scenes = _scenes("title", "key_points", "summary")
    assert "TOO_FEW_SCENE_KINDS" not in _codes(scenes)


def test_a_deck_of_cards_is_flagged_as_slideshow_risk() -> None:
    scenes = _scenes("title", "key_points", "key_points", "summary", "ending")
    assert "SLIDESHOW_RISK" in _codes(scenes)


def test_diagrams_and_charts_clear_the_slideshow_risk() -> None:
    scenes = _scenes("title", "flow", "timeline", "chart", "domain", "summary")
    assert "SLIDESHOW_RISK" not in _codes(scenes)


def test_findings_say_how_to_fix_it() -> None:
    scenes = _scenes("title", *(["key_points"] * 8))
    findings = score_composition(scenes).findings
    assert findings
    assert all(f["hint"] for f in findings)
    assert all(f["message"] for f in findings)


def test_findings_carry_the_measured_numbers() -> None:
    """人間の目を誘導するのが役目なので、具体的な数字を出す。"""
    scenes = _scenes("title", *(["key_points"] * 8))
    run = next(f for f in score_composition(scenes).findings if f["code"] == "MONOTONOUS_RUN")
    assert "8" in run["message"]


def test_scoring_is_deterministic() -> None:
    scenes = _scenes("title", "key_points", "key_points", "flow", "summary")
    assert score_composition(scenes).to_dict() == score_composition(scenes).to_dict()


# -- 宣言があれば厳しくする ------------------------------------------------------------


def test_declared_variety_is_enforced() -> None:
    """既定は warn。書き手が宣言したぶんは QA が fail で執行する。"""
    scenes = _scenes("title", "key_points", "key_points", "summary")
    report = score_composition(scenes, requirements={"min_distinct_scene_kinds": 5})
    assert any(f["code"] == "TOO_FEW_SCENE_KINDS" and f["declared"] for f in report.findings)


def test_declared_run_limit_is_enforced() -> None:
    scenes = _scenes("title", "key_points", "key_points", "summary")
    report = score_composition(scenes, requirements={"max_same_kind_run": 1})
    assert any(f["code"] == "MONOTONOUS_RUN" and f["declared"] for f in report.findings)


def test_a_satisfied_declaration_produces_no_declared_finding() -> None:
    scenes = _scenes("title", "flow", "timeline", "chart", "summary")
    report = score_composition(
        scenes, requirements={"min_distinct_scene_kinds": 5, "max_same_kind_run": 2}
    )
    assert not [f for f in report.findings if f["declared"]]


def test_defaults_are_never_marked_as_declared() -> None:
    scenes = _scenes("title", *(["key_points"] * 8))
    report = score_composition(scenes)
    assert report.findings
    assert not any(f["declared"] for f in report.findings)


# -- 既知の実例で実際に鳴ること -------------------------------------------------------


def _golden_scenes(name: str) -> list[dict[str, Any]]:
    golden = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return [
        {"id": s["id"], "kind": (s.get("diagram") or {}).get("kind") or "key_points"}
        for s in golden["script"]["scenes"]
    ]


def test_the_monotonous_golden_script_is_flagged() -> None:
    """**この検査を入れた動機そのもの。**

    activity_story は 14シーン中 10 が key_points、同一種別が 6 連続、
    カードだけの面が 86%。ここが鳴らないなら閾値か実装が間違っている。
    """
    codes = _codes(_golden_scenes("activity_story"))
    assert "MONOTONOUS_RUN" in codes, "同一種別の連続を見逃している"
    assert "SLIDESHOW_RISK" in codes, "カードだけの構成を見逃している"


def test_the_well_composed_golden_script_stays_silent() -> None:
    """**鳴らないことのほうが大事。**

    fiscal_year_script は domain / flow / timeline / comparison を混ぜており、
    同一種別の連続は 3、カード率は 14%。良い構成にまで警告を出す検査は、
    すぐ無視されるようになって役に立たなくなる。
    """
    assert score_composition(_golden_scenes("fiscal_year_story")).findings == []


def test_the_check_discriminates_between_the_two() -> None:
    """同じ書き手・同じ題材でも、構成の良し悪しで結果が分かれること。"""
    monotonous = score_composition(_golden_scenes("activity_story"))
    varied = score_composition(_golden_scenes("fiscal_year_story"))
    assert monotonous.longest_same_run > varied.longest_same_run
    assert monotonous.card_ratio > varied.card_ratio


def test_limits_are_documented_constants() -> None:
    """閾値は実動画を見て調整する前提なので、定数として外から見えること。"""
    assert MAX_SAME_KIND_RUN > 0
    assert MIN_DISTINCT_KINDS > 0
    assert 0 < CARD_RATIO_LIMIT <= 1.0
