"""テロップ（焼き込み字幕）の尺配分と、無音前提のパイプライン。

この動画にナレーション音声は無い。台本の `narration.text` は読み上げ原稿ではなく
**画面に出すテロップ**であり、視聴者は音を切ったままでも内容を追い切れなければ
ならない。ここで固定するのは:

- シーンの尺は読速から見積もる（TTS の読み上げ速度ではない）
- キューは文字量に比例して時間を貰う（等分ではない）
- 焼き込みが既定で、SRT/VTT は手動アップロード用に残る
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from abist_kb.application.video.caption_timing import (
    MIN_CAPTION_SEC,
    READING_CHARS_PER_SECOND,
    caption_chars,
    estimate_caption_duration,
)
from abist_kb.application.video import subtitles
from abist_kb.application.video.subtitles import (
    MIN_CUE_SEC,
    SUBTITLE_MAX_CHARS,
    TAIL_MARGIN_SEC,
    build_track,
    burn_in_filter,
)


def _scene(scene_id: str, narration: str) -> dict[str, Any]:
    return {"id": scene_id, "narration": {"text": narration}}


# -- 読速からの尺見積り -----------------------------------------------------------


def test_longer_caption_needs_more_time() -> None:
    short = estimate_caption_duration("短い文です。")
    long = estimate_caption_duration("短い文です。" * 5)
    assert long > short


def test_estimate_uses_the_reading_rate() -> None:
    text = "あ" * 45
    assert estimate_caption_duration(text) == pytest.approx(45 / READING_CHARS_PER_SECOND, abs=0.01)


def test_reading_is_slower_than_speech() -> None:
    """黙読は読み上げより遅い前提を置く（速いと読み切れない）。"""
    from abist_kb.infrastructure.video.tts_provider import CHARS_PER_SECOND

    assert READING_CHARS_PER_SECOND < CHARS_PER_SECOND


def test_duration_plan_budgets_text_at_reading_speed() -> None:
    """構成の逆算も読速で見積もる。

    読み上げ速度（6.5字/秒）で文字数を割り当てると、視聴者が読み切れない量の
    テロップを載せた構成が「目標尺どおり」として通ってしまう。
    """
    from abist_kb.application.video.duration_planner import NARRATION_FILL_RATIO, plan_duration

    plan = plan_duration({"min": 120, "max": 180}, available_materials=200)
    assert plan.ok, plan.message
    expected = int(plan.target_sec * NARRATION_FILL_RATIO * READING_CHARS_PER_SECOND)
    assert plan.narration_chars_total == expected


def test_short_caption_still_gets_a_readable_floor() -> None:
    assert estimate_caption_duration("はい") == MIN_CAPTION_SEC


def test_empty_caption_takes_no_time() -> None:
    assert estimate_caption_duration("") == 0.0
    assert caption_chars("  ") == 0


# -- キューの比例配分 -------------------------------------------------------------


def test_cues_are_allocated_in_proportion_to_text_length() -> None:
    """長い文が長く出る。等分だと短い文が無駄に居座り、長い文が読めない。"""
    scenes = [_scene("s01", "短い。" + "とても長い説明がここに続きます。" * 3)]
    track = build_track(scenes, offsets={"s01": 0.0}, durations={"s01": 30.0})

    assert len(track.cues) >= 2
    first, second = track.cues[0], track.cues[1]
    assert (second.end_sec - second.start_sec) > (first.end_sec - first.start_sec)


def test_cues_abut_without_gaps() -> None:
    scenes = [_scene("s01", "一文目です。二文目です。三文目です。")]
    track = build_track(scenes, offsets={"s01": 0.0}, durations={"s01": 20.0})
    for previous, following in zip(track.cues, track.cues[1:], strict=False):
        assert following.start_sec == pytest.approx(previous.end_sec, abs=0.01)


def test_last_cue_clears_the_end_of_the_scene() -> None:
    """シーン末尾は空けておく（画面切り替えに文字が重ならないように）。"""
    scenes = [_scene("s01", "一文目です。二文目です。")]
    track = build_track(scenes, offsets={"s01": 0.0}, durations={"s01": 20.0})
    assert track.cues[-1].end_sec <= 20.0 - TAIL_MARGIN_SEC + 0.01


def test_cues_never_start_before_their_scene() -> None:
    scenes = [_scene("s02", "本文です。")]
    track = build_track(scenes, offsets={"s02": 12.5}, durations={"s02": 10.0})
    assert track.cues[0].start_sec >= 12.5


def test_short_scene_warns_instead_of_silently_flashing() -> None:
    scenes = [_scene("s01", "一文目です。二文目です。三文目です。四文目です。")]
    track = build_track(scenes, offsets={"s01": 0.0}, durations={"s01": 2.0})
    assert track.warnings
    assert all(cue.end_sec - cue.start_sec >= MIN_CUE_SEC * 0.5 for cue in track.cues)


def test_allocation_is_deterministic() -> None:
    scenes = [_scene("s01", "一文目です。二文目はもう少し長い説明になります。")]
    first = build_track(scenes, offsets={"s01": 0.0}, durations={"s01": 18.0})
    second = build_track(scenes, offsets={"s01": 0.0}, durations={"s01": 18.0})
    assert [(c.start_sec, c.end_sec, c.lines) for c in first.cues] == [
        (c.start_sec, c.end_sec, c.lines) for c in second.cues
    ]


# -- 縦型（Shorts）のテロップ ------------------------------------------------------


def test_portrait_lines_are_shorter_than_landscape() -> None:
    assert SUBTITLE_MAX_CHARS["9:16"] < SUBTITLE_MAX_CHARS["16:9"]


def test_subtitle_width_mirrors_the_template_side() -> None:
    """src 側の写しが tools 側（別 venv）とずれていない。"""
    from templates import layout  # conftest が sys.path を通している

    for aspect, expected in SUBTITLE_MAX_CHARS.items():
        assert layout.subtitle_max_chars(aspect) == expected


def test_portrait_track_wraps_at_the_portrait_width() -> None:
    scenes = [_scene("s01", "縦型では一行を短くして文字そのものを大きく出します。")]
    track = build_track(scenes, offsets={"s01": 0.0}, durations={"s01": 20.0}, aspect_ratio="9:16")
    assert all(len(line) <= SUBTITLE_MAX_CHARS["9:16"] for cue in track.cues for line in cue.lines)


# -- 焼き込みフィルタ --------------------------------------------------------------


def test_burn_in_escapes_windows_paths(tmp_path: Path) -> None:
    srt = tmp_path / "narration.srt"
    srt.write_text("1\n", encoding="utf-8")
    expression = burn_in_filter(srt)
    assert "subtitles=" in expression
    assert ":\\" not in expression.split("force_style")[0].replace(r"\:", "")


@pytest.mark.parametrize("aspect", ["16:9", "9:16"])
def test_a_full_caption_line_fits_the_frame(aspect: str) -> None:
    """行が画面幅を超えると libass が勝手に折り返し、本文や出典にかぶる。

    実際に縦型でこれが起きた: 公称 34pt のテロップが3行に増え、出典フッタを
    覆っていた。公称サイズは映像の高さで拡大されるので、数値の大小だけでは
    判断できない（横型 26 > 縦型 12 でも、縦型のほうが大きく描かれる）。
    """
    from abist_kb.application.video.subtitles import (
        estimated_caption_width_px,
        usable_caption_width_px,
    )

    assert estimated_caption_width_px(aspect) <= usable_caption_width_px(aspect), (
        f"{aspect} のテロップが画面幅を超える（libass が勝手に折り返す）"
    )


def test_portrait_captions_fill_more_of_the_frame(tmp_path: Path) -> None:
    """縦型は画面幅に対して大きく出す（Shorts で読めるように）。"""
    from abist_kb.application.video.subtitles import (
        estimated_caption_width_px,
        usable_caption_width_px,
    )

    def fill(aspect: str) -> float:
        return estimated_caption_width_px(aspect) / usable_caption_width_px(aspect)

    # 1文字あたりの占有率で比べる（縦型は1行が短いぶん、文字自体は大きい）
    portrait = fill("9:16") / SUBTITLE_MAX_CHARS["9:16"]
    landscape = fill("16:9") / SUBTITLE_MAX_CHARS["16:9"]
    assert portrait > landscape


def test_burn_in_has_an_outline_for_legibility(tmp_path: Path) -> None:
    srt = tmp_path / "narration.srt"
    srt.write_text("1\n", encoding="utf-8")
    assert "Outline=" in burn_in_filter(srt)


# -- テロップの読みやすさ（分割の質） -------------------------------------------------
#
# 実際に動画を1本作って初めて見つかった不備。字幕は「切れてはいない」が、
# 切り方が読みを壊していた。


def test_a_number_is_not_split_across_lines() -> None:
    """「2179点」が「2」と「179点」に割れないこと。

    数字が行をまたぐと、読み手には別の数として見える（2 と 179点）。
    テンプレート側の折返し（`layout.wrap_cjk`）は ASCII 語を守るのに、
    字幕側の `wrap_line` は守っていなかった。
    """
    lines = subtitles.wrap_line("ファイルから1行ずつ読んで描く実装では、2179点で20分25秒。", 20)
    assert any("2179" in line for line in lines), lines


def test_an_english_word_is_not_split_across_lines() -> None:
    lines = subtitles.wrap_line("値の取得をやめて、Range関数で範囲ごとまとめて取得します。", 12)
    assert any("Range" in line for line in lines), lines


def test_a_long_sentence_does_not_leave_an_orphan_cue() -> None:
    """最後に「と。」だけのキューを残さないこと。

    1文が3行になると `[行1,行2]` `[行3]` に切れ、2文字だけの字幕が
    数秒間ぽつんと出る。行数をキューの容量で割り切れるように均す。
    """
    text = "CATIAで表示できること、色の分布が分かること、5000個を5分から10分で描き切ること。"
    cues = subtitles.chunk_narration(text, max_chars=20, max_lines=2)

    assert len(cues) > 1, "この文は1キューに収まらない前提のテスト"
    assert all(len(cue) == 2 for cue in cues), [["".join(c)] for c in cues]


def test_balancing_evens_out_the_line_lengths() -> None:
    """行数を割り切れるようにするだけでは足りない。

    最後の行が「と。」の2文字だけ、という絵は依然として悪い。均すのは
    キューの数ではなく**1行の長さ**。
    """
    text = "CATIAで表示できること、色の分布が分かること、5000個を5分から10分で描き切ること。"
    lines = [line for cue in subtitles.chunk_narration(text, max_chars=20, max_lines=2) for line in cue]

    widths = [subtitles._display_width(line) for line in lines]
    assert min(widths) >= max(widths) * 0.5, dict(zip(lines, widths))


def test_balancing_never_loses_or_reorders_text() -> None:
    text = "CATIAで表示できること、色の分布が分かること、5000個を5分から10分で描き切ること。"
    cues = subtitles.chunk_narration(text, max_chars=20, max_lines=2)
    assert "".join("".join(cue) for cue in cues) == text


def test_a_short_sentence_still_makes_one_short_cue() -> None:
    """均しの対象は「キューをまたぐ文」だけ。短い文を無理に増やさない。"""
    cues = subtitles.chunk_narration("この3つが出発点の条件でした。", max_chars=20, max_lines=2)
    assert len(cues) == 1


def test_balanced_lines_stay_within_the_declared_width() -> None:
    text = "パートファイル分割、間引き、まとめ読みを重ねると、同じ2179点が3分41秒でした。"
    for cue in subtitles.chunk_narration(text, max_chars=20, max_lines=2):
        for line in cue:
            assert subtitles._display_width(line) <= 40, line


def test_word_char_rule_mirrors_the_template_side() -> None:
    """字幕側とテンプレート側で「割ってはいけない字」の判定を揃える。

    別 venv なので import で共有できない。写しが食い違うと、同じ文が図では
    割れず字幕では割れる（あるいは逆）ことになる。
    """
    from templates import layout

    samples = "0123456789abcXYZ_-.あア漢、。（ ％/:"
    assert [subtitles._is_word_char(c) for c in samples] == [
        layout._is_ascii_word_char(c) for c in samples
    ]
