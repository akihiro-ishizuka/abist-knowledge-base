"""目標尺から構成を逆算する（作者へ渡す目安）。

台本を機械生成する経路は無いので、ここが決めるのは**作者への目安**:
「3分なら何シーンで、テロップは何字くらいか」。

守る性質:

1. `target_duration_sec` から章数・シーン数・1シーンの目標尺・テロップ
   目標文字数が決まる
2. カード面は固定尺、本編は残りを配分（一律にすると完成尺が目標を割る）
3. 文字量の見積もりは**読速**から出す（音声の読み上げ速度ではない）
"""

from __future__ import annotations

import pytest

from abist_kb.application.video.duration_planner import (
    CARD_TARGET_SEC,
    MAX_SCENE_SEC,
    MIN_SCENE_SEC,
    narration_chars_for,
    plan_duration,
    seconds_for_chars,
)


class TestDurationPlan:
    def test_scene_count_follows_the_target(self) -> None:
        short = plan_duration({"min": 120, "max": 180}, available_materials=200)
        long = plan_duration({"min": 300, "max": 600}, available_materials=200)
        assert short.ok and long.ok
        assert long.scene_count > short.scene_count
        assert long.chapter_count >= short.chapter_count

    def test_scene_length_stays_within_bounds(self) -> None:
        for lo, hi in ((60, 120), (120, 180), (300, 600), (600, 900)):
            plan = plan_duration({"min": lo, "max": hi}, available_materials=500)
            assert plan.ok
            assert MIN_SCENE_SEC <= plan.scene_target_sec <= MAX_SCENE_SEC

    def test_role_counts_sum_to_scene_count(self) -> None:
        plan = plan_duration({"min": 120, "max": 180}, available_materials=200)
        assert sum(plan.scenes_by_role.values()) == plan.scene_count

    def test_card_and_body_targets_reconstruct_the_total(self) -> None:
        """カード固定尺 + 本編配分尺 の合計が目標尺に一致すること。

        ここがずれると、完成尺が目標を割る（実測 132 秒目標に対し 103 秒に
        なった不具合の再発防止）。
        """
        plan = plan_duration({"min": 120, "max": 180}, available_materials=200)
        cards = 1 + plan.chapter_count + 1  # 表紙 + 章扉 + エンドカード
        body = sum(
            plan.scenes_by_role.get(role, 0)
            for role in ("explain", "diagram", "comparison", "summary")
        )
        total = cards * plan.card_target_sec + body * plan.body_target_sec
        assert total == pytest.approx(plan.target_sec, abs=0.5)

    def test_caption_budget_is_derived_from_the_reading_rate(self) -> None:
        plan = plan_duration({"min": 120, "max": 180}, available_materials=200)
        assert plan.narration_chars_total == narration_chars_for(plan.target_sec)
        assert plan.narration_chars_per_body_scene == narration_chars_for(plan.body_target_sec)
        # 逆算が往復すること
        assert seconds_for_chars(plan.narration_chars_total) == pytest.approx(
            plan.target_sec, abs=0.5
        )

    def test_card_target_is_shorter_than_body(self) -> None:
        plan = plan_duration({"min": 120, "max": 180}, available_materials=200)
        assert plan.card_target_sec == CARD_TARGET_SEC
        assert plan.body_target_sec > plan.card_target_sec

    def test_insufficient_content_stops_instead_of_padding(self) -> None:
        """素材が足りないなら**水増しせずに止まる**。"""
        plan = plan_duration({"min": 600, "max": 900}, available_materials=4)
        assert not plan.ok
        assert plan.code == "INSUFFICIENT_CONTENT_FOR_DURATION"
        assert "水増し" in (plan.message or "")
        assert "到達可能" in (plan.message or "")

    def test_thin_content_warns_without_failing(self) -> None:
        plan = plan_duration({"min": 120, "max": 180}, available_materials=12)
        assert plan.ok
        assert plan.warnings

    def test_invalid_target_is_rejected(self) -> None:
        assert plan_duration({"min": 0, "max": 100}).code == "INVALID_VIDEO_SPEC"
        assert plan_duration({"min": 200, "max": 100}).code == "INVALID_VIDEO_SPEC"
