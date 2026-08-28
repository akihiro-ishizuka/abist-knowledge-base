"""品質段（draft / standard / high）が動画パイプラインから届くこと。

`quality` は画素寸法の段だけを決め、アスペクト比は `frame` が決める。以前は
`format.width` を覗いて品質を推測していたため、パイプラインが 1920x1080 しか
組まない以上 `high`（2560x1440 / 60fps）へは決して到達できなかった。
"""

from __future__ import annotations

import pytest

from abist_kb.application.video.pipeline import _format_for
from abist_kb.application.video.video_renderer import _scene_spec_for
from abist_kb.domain.video_project_spec import (
    FRAME_PIXELS,
    QUALITY_FRAME_RATES,
    VIDEO_QUALITIES,
)


def test_high_quality_reaches_its_pixel_tier() -> None:
    fmt = _format_for("16:9", None, quality="high")
    assert (fmt["width"], fmt["height"]) == (2560, 1440)
    assert fmt["fps"] == 60


def test_default_quality_is_unchanged() -> None:
    fmt = _format_for("16:9", None)
    assert (fmt["width"], fmt["height"], fmt["fps"]) == (1920, 1080, 30)


def test_portrait_high_quality() -> None:
    fmt = _format_for("9:16", None, quality="high")
    assert (fmt["width"], fmt["height"]) == (1440, 2560)


@pytest.mark.parametrize("quality", VIDEO_QUALITIES)
def test_scene_inherits_the_project_quality(quality: str) -> None:
    """シーンは `format.quality` をそのまま受け取る（幅から推測しない）。"""
    spec = {"format": _format_for("16:9", None, quality=quality)}
    scene = {"id": "s01", "scene_spec": {"scene_kind": "explain", "beats": []}}
    enriched = _scene_spec_for(scene, spec)
    assert enriched is not None
    assert enriched["quality"] == quality


def test_unknown_quality_falls_back_to_standard() -> None:
    fmt = _format_for("16:9", None, quality="cinematic")
    assert (fmt["width"], fmt["height"], fmt["fps"]) == (1920, 1080, 30)


def test_pixel_table_mirrors_the_template_side() -> None:
    """src 側の写しが tools 側（別 venv）とずれていない。"""
    from templates import layout  # conftest が sys.path を通している

    assert FRAME_PIXELS == layout.FRAME_PIXELS


def test_frame_rate_table_mirrors_the_render_script() -> None:
    import importlib.util
    from pathlib import Path

    render_scene = Path(__file__).parents[2] / "tools" / "visualize" / "render_scene.py"
    source = render_scene.read_text(encoding="utf-8")
    for quality, fps in QUALITY_FRAME_RATES.items():
        assert f'"{quality}": {fps}' in source, (
            f"{quality} の fps が render_scene.py とずれています"
        )
    assert importlib.util.find_spec is not None
