"""宣言的 `story_requirements` による構成ゲートの検証。

以前は「設計効率化」+「2026」というタイトル部分一致で必須テーマ・シーン数・
出典期間を検査していた（=特定の1本のためのロジックが汎用バリデータに漏出していた）。
本テストは、その検査が **spec が自己申告した要件** で駆動されることを固定する。
"""

from __future__ import annotations

from typing import Any

from abist_kb.application.video.content_quality import validate_story_content
from abist_kb.domain.video_project_spec import validate_video_project_spec


def _scene(
    scene_id: str,
    *,
    title: str = "見出し",
    narration: str = "本文です。",
    kind: str = "explain",
    source_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": scene_id,
        "kind": kind,
        "title": title,
        "narration": {"text": narration},
        "scene_spec": {
            "beats": [],
            "sources": [{"path": path} for path in source_paths],
        },
    }


def _base_spec(**overrides: Any) -> dict[str, Any]:
    spec = {
        "title": "テスト動画",
        "inputs": {"kb_paths": ["esa/a.md"]},
    }
    spec.update(overrides)
    return spec


# -- 要件が無いときは汎用チェックのみ ------------------------------------------


def test_no_requirements_means_only_generic_checks() -> None:
    scenes = [_scene("s01"), _scene("s02")]
    assert validate_story_content(scenes, []) == []


def test_generic_checks_still_run_without_requirements() -> None:
    scenes = [_scene("s01", title="凡例")]
    codes = {error["code"] for error in validate_story_content(scenes, [])}
    assert "FORBIDDEN_VIDEO_HEADING" in codes


# -- required_topics ------------------------------------------------------------


def test_missing_required_topic_is_reported() -> None:
    scenes = [_scene("s01", narration="三桜工業様の進捗です。")]
    errors = validate_story_content(scenes, [], requirements={"required_topics": ["三桜", "TMEJ"]})
    assert [error["code"] for error in errors] == ["MISSING_STORY_TOPIC"]
    assert "TMEJ" in errors[0]["message"]


def test_present_topics_pass() -> None:
    scenes = [_scene("s01", narration="三桜とTMEJの状況です。")]
    assert (
        validate_story_content(scenes, [], requirements={"required_topics": ["三桜", "TMEJ"]}) == []
    )


# -- required_scene_kinds -------------------------------------------------------


def test_missing_required_scene_kind_is_reported() -> None:
    scenes = [_scene("s01", kind="explain")]
    errors = validate_story_content(scenes, [], requirements={"required_scene_kinds": ["timeline"]})
    assert [error["code"] for error in errors] == ["MISSING_REQUIRED_SCENE_KIND"]
    assert "timeline" in errors[0]["message"]


def test_present_scene_kind_passes() -> None:
    scenes = [_scene("s01", kind="timeline")]
    assert (
        validate_story_content(scenes, [], requirements={"required_scene_kinds": ["timeline"]})
        == []
    )


# -- scene_count ----------------------------------------------------------------


def test_scene_count_below_minimum_is_reported() -> None:
    scenes = [_scene("s01"), _scene("s02")]
    errors = validate_story_content(scenes, [], requirements={"scene_count": {"min": 3, "max": 5}})
    assert [error["code"] for error in errors] == ["INVALID_STORY_SCENE_COUNT"]


def test_scene_count_inside_range_passes() -> None:
    scenes = [_scene("s01"), _scene("s02"), _scene("s03")]
    assert (
        validate_story_content(scenes, [], requirements={"scene_count": {"min": 3, "max": 5}}) == []
    )


# -- source_path_pattern --------------------------------------------------------


def test_source_outside_the_declared_period_is_reported() -> None:
    scenes = [_scene("s01", source_paths=("esa/2024_01_古い議事録.md",))]
    errors = validate_story_content(scenes, [], requirements={"source_path_pattern": r"2026_\d{2}"})
    assert [error["code"] for error in errors] == ["SOURCE_OUTSIDE_TARGET_PERIOD"]
    assert "2024_01_古い議事録.md" in errors[0]["message"]


def test_source_matching_the_pattern_passes() -> None:
    scenes = [_scene("s01", source_paths=("esa/2026_07_定例.md",))]
    assert (
        validate_story_content(scenes, [], requirements={"source_path_pattern": r"2026_\d{2}"})
        == []
    )


# -- sound_events ---------------------------------------------------------------


def test_sound_event_count_outside_range_is_reported() -> None:
    events = [{"scene_id": "s01", "event": "intro", "anchor": "scene.start"}]
    errors = validate_story_content(
        [_scene("s01")], events, requirements={"sound_events": {"min": 2, "max": 4}}
    )
    assert [error["code"] for error in errors] == ["INVALID_SOUND_EVENT_COUNT"]


def test_sound_event_count_inside_range_passes() -> None:
    events = [{"scene_id": "s01", "event": "intro", "anchor": "scene.start"}] * 3
    assert (
        validate_story_content(
            [_scene("s01")], events, requirements={"sound_events": {"min": 2, "max": 4}}
        )
        == []
    )


# -- タイトル分岐が消えていること -------------------------------------------------


def test_title_no_longer_triggers_hidden_requirements() -> None:
    """「設計効率化 2026」というタイトルでも、要件を宣言しなければ何も要求されない。"""
    scenes = [_scene("s01", title="設計効率化 2026年活動まとめ")]
    assert validate_story_content(scenes, []) == []


# -- spec スキーマ検証 ------------------------------------------------------------


def test_story_requirements_defaults_to_none() -> None:
    result = validate_video_project_spec(_base_spec())
    assert result.ok
    assert result.spec is not None
    assert result.spec["story_requirements"] is None


def test_valid_story_requirements_is_accepted() -> None:
    result = validate_video_project_spec(
        _base_spec(
            story_requirements={
                "required_topics": ["三桜"],
                "required_scene_kinds": ["timeline"],
                "scene_count": {"min": 12, "max": 14},
                "source_path_pattern": r"2026_\d{2}",
                "sound_events": {"min": 14, "max": 18},
            }
        )
    )
    assert result.ok, [error.to_dict() for error in result.errors]


def test_story_requirements_rejects_non_object() -> None:
    result = validate_video_project_spec(_base_spec(story_requirements=["三桜"]))
    assert not result.ok
    assert any(error.path == "story_requirements" for error in result.errors)


def test_story_requirements_rejects_inverted_range() -> None:
    result = validate_video_project_spec(
        _base_spec(story_requirements={"scene_count": {"min": 14, "max": 12}})
    )
    assert not result.ok
    assert any(error.path == "story_requirements.scene_count" for error in result.errors)


def test_story_requirements_rejects_broken_regex() -> None:
    result = validate_video_project_spec(
        _base_spec(story_requirements={"source_path_pattern": "["})
    )
    assert not result.ok
    assert any(error.path == "story_requirements.source_path_pattern" for error in result.errors)


def test_story_requirements_rejects_non_string_topics() -> None:
    result = validate_video_project_spec(_base_spec(story_requirements={"required_topics": [1, 2]}))
    assert not result.ok
    assert any(error.path == "story_requirements.required_topics" for error in result.errors)


# -- 構成の単調さ（宣言できる分） -------------------------------------------------


def test_variety_requirements_are_accepted() -> None:
    result = validate_video_project_spec(
        _base_spec(story_requirements={"min_distinct_scene_kinds": 5, "max_same_kind_run": 3})
    )
    assert result.ok, [error.to_dict() for error in result.errors]


def test_variety_requirements_must_be_positive_integers() -> None:
    for field in ("min_distinct_scene_kinds", "max_same_kind_run"):
        result = validate_video_project_spec(_base_spec(story_requirements={field: 0}))
        assert not result.ok, field
        assert any(error.path == f"story_requirements.{field}" for error in result.errors)
