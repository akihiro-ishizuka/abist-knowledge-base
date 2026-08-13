"""見た目に関わる SceneSpec の任意フィールド（motion / theme / transition）。

**すべて閉じた列挙にする。** エージェントが台本を書く以上、自由な色や
アニメーション名を書けてしまうと、読めない配色や破綻した演出が動画に載る。
選べるのは「用意された段」だけ。
"""

from __future__ import annotations

from typing import Any

import pytest

from abist_kb.domain.scene_spec import (
    MOTION_VALUES,
    THEME_VALUES,
    validate_scene_spec,
)


def _spec(**overrides: Any) -> dict[str, Any]:
    spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "mp4",
        "template": "step_explanation",
        "title": "テスト",
        "sources": [
            {
                "id": "s1",
                "path": "a/b.md",
                "start_line": 1,
                "end_line": 5,
                "content_hash": "0" * 64,
            }
        ],
        "beats": [{"type": "statement", "text": "本文", "source_refs": ["s1"]}],
    }
    spec.update(overrides)
    return spec


# -- motion -------------------------------------------------------------------------


def test_motion_is_optional() -> None:
    assert validate_scene_spec(_spec()).ok


@pytest.mark.parametrize("motion", MOTION_VALUES)
def test_known_motion_levels_are_accepted(motion: str) -> None:
    assert validate_scene_spec(_spec(motion=motion)).ok


def test_unknown_motion_is_rejected() -> None:
    result = validate_scene_spec(_spec(motion="cinematic"))
    assert not result.ok
    assert any(e.path == "motion" for e in result.errors)


# -- theme --------------------------------------------------------------------------


@pytest.mark.parametrize("theme", THEME_VALUES)
def test_known_themes_are_accepted(theme: str) -> None:
    assert validate_scene_spec(_spec(theme=theme)).ok


def test_agents_cannot_invent_colors() -> None:
    """テーマ名しか受け付けない（色コードを直接書かせない）。"""
    result = validate_scene_spec(_spec(theme="#ff00ff"))
    assert not result.ok
    assert any(e.path == "theme" for e in result.errors)


# -- transition ---------------------------------------------------------------------


def test_transition_accepts_the_known_styles() -> None:
    assert validate_scene_spec(_spec(transition={"style": "dip", "duration_sec": 0.35})).ok
    assert validate_scene_spec(_spec(transition={"style": "none"})).ok


def test_unknown_transition_style_is_rejected() -> None:
    result = validate_scene_spec(_spec(transition={"style": "star_wipe"}))
    assert not result.ok
    assert any(e.path == "transition.style" for e in result.errors)


def test_absurd_transition_duration_is_rejected() -> None:
    """繋ぎが長すぎると、内容が映っていない時間が尺を食う。"""
    result = validate_scene_spec(_spec(transition={"style": "dip", "duration_sec": 5.0}))
    assert not result.ok
    assert any(e.path == "transition.duration_sec" for e in result.errors)


# -- accent_index -------------------------------------------------------------------


def test_accent_index_must_be_a_non_negative_integer() -> None:
    assert validate_scene_spec(_spec(accent_index=0)).ok
    assert validate_scene_spec(_spec(accent_index=3)).ok
    assert not validate_scene_spec(_spec(accent_index=-1)).ok
