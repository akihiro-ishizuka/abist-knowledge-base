"""焼き込みテロップの帯を、図が踏まないこと。

実際に動画を1本作って見つかった不備。`chart`（折れ線）のシーンで、
**出典フッターと目盛りラベルがテロップの下に完全に隠れていた**。
`fit_to_frame` はフレーム全体に収めるだけで、下端の一定割合がテロップに
占められることを知らなかった。

出典を画面に出すのはこのシステムの根幹（事実は必ず出典に紐づく）なので、
「隠れているが存在はする」は成立しない。

帯の高さは理屈ではなく**実測**で決めている（フォントサイズを決めたときと同じ）:

    黒一色の動画へ2行のテロップを焼き、文字の上端を測る
    16:9  1920x1080  下端から 258px = 0.2389
    9:16  1080x1920  下端から 411px = 0.2141
"""

from __future__ import annotations

import pytest

ASPECTS = ("16:9", "9:16")

#: 実測値（上のドキュメンテーション参照）。定数はこれ以上でなければならない。
MEASURED_BAND_RATIO = {"16:9": 0.2389, "9:16": 0.2141}


@pytest.fixture(scope="module")
def layout_module():
    from templates import layout

    return layout


@pytest.mark.parametrize("aspect", ASPECTS)
def test_the_band_covers_what_was_measured(layout_module, aspect: str) -> None:
    """実測より狭い帯を宣言しない（狭いと図がテロップに潜る）。"""
    _, height = layout_module.frame_size(aspect)
    ratio = layout_module.caption_band_height(aspect) / height
    assert ratio >= MEASURED_BAND_RATIO[aspect]


@pytest.mark.parametrize("aspect", ASPECTS)
def test_the_band_does_not_eat_the_whole_frame(layout_module, aspect: str) -> None:
    _, height = layout_module.frame_size(aspect)
    assert layout_module.caption_band_height(aspect) < height / 2


@pytest.mark.parametrize("aspect", ASPECTS)
def test_content_bounds_sit_above_the_band(layout_module, aspect: str) -> None:
    _, height = layout_module.frame_size(aspect)
    bottom, top = layout_module.content_bounds(aspect)
    assert bottom >= -height / 2 + layout_module.caption_band_height(aspect)
    assert top <= height / 2
    assert bottom < top


@pytest.mark.parametrize("aspect", ASPECTS)
def test_the_content_area_is_still_usable(layout_module, aspect: str) -> None:
    """帯を引いても図を置く高さが残ること（縦型で潰れやすい）。"""
    _, height = layout_module.frame_size(aspect)
    bottom, top = layout_module.content_bounds(aspect)
    assert (top - bottom) >= height * 0.5


@pytest.mark.parametrize("aspect", ASPECTS)
def test_the_band_ratio_matches_the_burn_in_style(aspect: str) -> None:
    """帯の定数と、実際に焼く字幕の指定が同じ前提から出ていること。

    `BURN_IN_FONT_SIZE` を変えたのに帯を直し忘れると、また図が潜る。
    """
    from abist_kb.application.video.subtitles import caption_band_ratio
    from templates import layout

    _, height = layout.frame_size(aspect)
    assert layout.caption_band_height(aspect) / height == pytest.approx(
        caption_band_ratio(aspect), rel=1e-6
    )


@pytest.mark.parametrize("aspect", ASPECTS)
def test_the_declared_ratio_tracks_the_measurement(aspect: str) -> None:
    from abist_kb.application.video.subtitles import caption_band_ratio

    assert caption_band_ratio(aspect) >= MEASURED_BAND_RATIO[aspect]
    assert caption_band_ratio(aspect) <= MEASURED_BAND_RATIO[aspect] + 0.05


# -- 収め方（縮小率と縦位置） ---------------------------------------------------------


@pytest.mark.parametrize("aspect", ASPECTS)
def test_a_small_figure_is_centred_in_the_content_area(layout_module, aspect: str) -> None:
    """帯を避けた領域の中央へ置く。フレーム中央ではない。"""
    bottom, top = layout_module.content_bounds(aspect)
    scale, center_y = layout_module.fit_box((1.0, 1.0), aspect)
    assert scale == 1.0, "小さい図は拡大しない"
    assert center_y == pytest.approx((bottom + top) / 2)


@pytest.mark.parametrize("aspect", ASPECTS)
def test_a_tall_figure_is_shrunk_to_the_content_area(layout_module, aspect: str) -> None:
    width, height = layout_module.frame_size(aspect)
    bottom, top = layout_module.content_bounds(aspect)
    scale, center_y = layout_module.fit_box((1.0, height), aspect)
    assert height * scale <= (top - bottom) + 1e-9
    assert (center_y - height * scale / 2) >= bottom - 1e-9


@pytest.mark.parametrize("aspect", ASPECTS)
def test_a_wide_figure_is_shrunk_to_the_safe_width(layout_module, aspect: str) -> None:
    width, _ = layout_module.frame_size(aspect)
    safe_width, _ = layout_module.safe_area(aspect)
    scale, _ = layout_module.fit_box((width, 0.5), aspect)
    assert width * scale <= safe_width + 1e-9


@pytest.mark.parametrize("aspect", ASPECTS)
def test_nothing_ever_lands_in_the_caption_band(layout_module, aspect: str) -> None:
    """どんな寸法の図でも、下端が帯へ入らないこと。"""
    width, height = layout_module.frame_size(aspect)
    band_top = -height / 2 + layout_module.caption_band_height(aspect)
    for w in (0.5, width / 2, width, width * 2):
        for h in (0.5, height / 2, height, height * 2):
            scale, center_y = layout_module.fit_box((w, h), aspect)
            assert center_y - h * scale / 2 >= band_top - 1e-9, (w, h)
