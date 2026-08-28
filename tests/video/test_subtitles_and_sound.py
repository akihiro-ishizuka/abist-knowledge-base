"""Phase 5（字幕）と Phase 6（効果音）。

Phase 6 の要点:
- 音源名を LLM／利用者に選ばせない（意味イベントだけ）
- 既定 subtle、1シーン最大 2〜3 回、最短間隔、ナレーション競合回避
- 決定的（同じ入力 → 同じ音源）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.video.sound_events import (
    DEFAULT_GAIN_DB,
    EVENT_TO_CATEGORY,
    MAX_PER_SCENE,
    MIN_INTERVAL_SEC,
    load_palette,
    resolve_sound_events,
    used_attributions,
)
from abist_kb.application.video.subtitles import (
    build_track,
    burn_in_filter,
    chunk_narration,
    split_sentences,
    to_srt,
    to_vtt,
    wrap_line,
    write_subtitles,
)

PALETTE = Path(__file__).resolve().parents[2] / "assets" / "sound-design" / "manifest.json"


@pytest.fixture
def assets():
    loaded, warnings = load_palette(PALETTE)
    assert loaded, f"同梱パレットを読めない: {warnings}"
    return loaded


class TestSubtitleText:
    def test_split_sentences(self) -> None:
        assert split_sentences("これは一文目。これは二文目！") == [
            "これは一文目。",
            "これは二文目！",
        ]

    def test_no_terminator_is_one_sentence(self) -> None:
        assert split_sentences("区切りのない文") == ["区切りのない文"]

    def test_wrap_respects_width(self) -> None:
        for line in wrap_line("あ" * 50, 10):
            assert len(line) <= 11  # 禁則の追い出しで±1

    def test_line_does_not_start_with_forbidden_char(self) -> None:
        for line in wrap_line("これは、とても長い日本語の文章です。折り返します。", 8):
            assert line[0] not in "。、）」"

    def test_chunk_respects_max_lines(self) -> None:
        chunks = chunk_narration("あ" * 120, max_chars=10, max_lines=2)
        assert all(len(c) <= 2 for c in chunks)
        assert len(chunks) > 1


class TestSubtitleTrack:
    def _scenes(self):
        return [
            {"id": "s01", "narration": {"text": "最初の説明です。次の文もあります。"}},
            {"id": "s02", "narration": {"text": None}},
            {"id": "s03", "narration": {"text": "最後のまとめです。"}},
        ]

    def test_cues_follow_the_measured_timeline(self) -> None:
        track = build_track(
            self._scenes(),
            offsets={"s01": 0.0, "s02": 10.0, "s03": 20.0},
            durations={"s01": 10.0, "s02": 10.0, "s03": 6.0},
        )
        assert track.cues
        assert track.cues[0].start_sec == 0.0
        # s03 の字幕はシーン開始以降
        last = [c for c in track.cues if c.start_sec >= 20.0]
        assert last, "s03 の字幕が出ていない"
        for cue in track.cues:
            assert cue.end_sec > cue.start_sec

    def test_scene_without_narration_has_no_cue(self) -> None:
        track = build_track(
            self._scenes(),
            offsets={"s01": 0.0, "s02": 10.0, "s03": 20.0},
            durations={"s01": 10.0, "s02": 10.0, "s03": 6.0},
        )
        # s02 は 10.0〜20.0。その範囲に始まる cue は無い
        assert not [c for c in track.cues if 10.0 <= c.start_sec < 20.0]

    def test_missing_timeline_warns(self) -> None:
        track = build_track(self._scenes(), offsets={}, durations={})
        assert track.warnings

    def test_srt_and_vtt_format(self, tmp_path: Path) -> None:
        track = build_track(
            self._scenes(),
            offsets={"s01": 0.0, "s03": 20.0},
            durations={"s01": 10.0, "s03": 6.0},
        )
        srt = to_srt(track)
        assert "1\n00:00:00,000 --> " in srt
        vtt = to_vtt(track)
        assert vtt.startswith("WEBVTT")
        assert "00:00:00.000 --> " in vtt

        written = write_subtitles(track, tmp_path)
        assert written["srt"].is_file() and written["vtt"].is_file()

    def test_burn_in_filter_escapes_windows_path(self, tmp_path: Path) -> None:
        srt = tmp_path / "n.srt"
        srt.write_text("", encoding="utf-8")
        expression = burn_in_filter(srt)
        assert expression.startswith("subtitles='")
        assert r"\:" in expression, "Windows のドライブ区切りをエスケープしていない"


class TestSoundPalette:
    def test_bundled_palette_is_valid(self, assets) -> None:
        """同梱パレットはライセンスと SHA-256 を持つ。"""
        assert len(assets) >= 9
        for asset in assets:
            assert asset.license
            assert len(asset.sha256) == 64
            assert Path(asset.path).is_file()

    def test_every_event_has_a_category(self, assets) -> None:
        available = {c for a in assets for c in a.categories}
        for event, category in EVENT_TO_CATEGORY.items():
            assert category in available, f"{event} 用の音源が無い"

    def test_tampered_asset_is_rejected(self, tmp_path: Path) -> None:
        import json

        (tmp_path / "professional").mkdir()
        audio = tmp_path / "professional" / "x.wav"
        audio.write_bytes(b"not-the-original")
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "assets": [
                        {
                            "id": "x",
                            "categories": ["accent"],
                            "path": "professional/x.wav",
                            "license": "in-house",
                            "sha256": "0" * 64,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        loaded, warnings = load_palette(manifest)
        assert loaded == []
        assert any("SHA-256" in w for w in warnings)


class TestSoundResolution:
    def _events(self, scene_id="s01", count=1, event="key_point"):
        return [
            {"scene_id": scene_id, "event": event, "anchor": f"beat-{i + 1}.reveal"}
            for i in range(count)
        ]

    def test_resolution_is_deterministic(self, assets) -> None:
        """同じ入力からは同じ音源が選ばれる。"""
        kwargs = {
            "assets": assets,
            "offsets": {"s01": 0.0},
            "durations": {"s01": 10.0},
        }
        first = resolve_sound_events(self._events(), **kwargs)
        second = resolve_sound_events(self._events(), **kwargs)
        assert [c.sound_id for c in first.cues] == [c.sound_id for c in second.cues]

    def test_subtle_caps_at_two_per_scene(self, assets) -> None:
        outcome = resolve_sound_events(
            self._events(count=5),
            assets=assets,
            offsets={"s01": 0.0},
            durations={"s01": 30.0},
            intensity="subtle",
        )
        assert len(outcome.cues) <= MAX_PER_SCENE["subtle"]
        assert any(d["reason"] == "max_per_scene" for d in outcome.dropped)

    def test_normal_allows_three(self, assets) -> None:
        outcome = resolve_sound_events(
            [
                {"scene_id": "s01", "event": "warning", "anchor": "beat-1.reveal"},
                {"scene_id": "s01", "event": "key_point", "anchor": "beat-3.reveal"},
                {"scene_id": "s01", "event": "success", "anchor": "beat-5.reveal"},
                {"scene_id": "s01", "event": "chapter_change", "anchor": "scene.start"},
            ],
            assets=assets,
            offsets={"s01": 0.0},
            durations={"s01": 60.0},
            intensity="normal",
        )
        assert len(outcome.cues) <= MAX_PER_SCENE["normal"]

    def test_minimum_interval_is_enforced(self, assets) -> None:
        outcome = resolve_sound_events(
            [
                {"scene_id": "s01", "event": "warning", "anchor": "scene.start"},
                {"scene_id": "s01", "event": "key_point", "anchor": "scene.start"},
            ],
            assets=assets,
            offsets={"s01": 0.0},
            durations={"s01": 10.0},
            intensity="normal",
        )
        times = sorted(c.t_sec for c in outcome.cues)
        for a, b in zip(times, times[1:], strict=False):
            assert b - a >= MIN_INTERVAL_SEC
        assert any(d["reason"] == "min_interval" for d in outcome.dropped)

    def test_priority_keeps_warning_over_chapter_change(self, assets) -> None:
        outcome = resolve_sound_events(
            [
                {"scene_id": "s01", "event": "chapter_change", "anchor": "scene.start"},
                {"scene_id": "s01", "event": "warning", "anchor": "scene.start"},
            ],
            assets=assets,
            offsets={"s01": 0.0},
            durations={"s01": 10.0},
            intensity="subtle",
        )
        assert [c.event for c in outcome.cues] == ["warning"]

    def test_narration_conflict_is_shifted_and_ducked(self, assets) -> None:
        outcome = resolve_sound_events(
            [{"scene_id": "s01", "event": "key_point", "anchor": "scene.start"}],
            assets=assets,
            offsets={"s01": 0.0},
            durations={"s01": 10.0},
            narration_starts={"s01": 0.0},
        )
        assert outcome.cues
        cue = outcome.cues[0]
        assert cue.t_sec > 0.0, "ナレーション開始と重なったままになっている"
        assert cue.gain_db < DEFAULT_GAIN_DB, "ダッキングされていない"
        assert outcome.warnings

    def test_unresolved_anchor_is_dropped_not_guessed(self, assets) -> None:
        """推測で真ん中に置いたりしない（誤爆防止）。"""
        outcome = resolve_sound_events(
            [{"scene_id": "s01", "event": "key_point", "anchor": "unknown.anchor"}],
            assets=assets,
            offsets={"s01": 0.0},
            durations={"s01": 10.0},
        )
        assert outcome.cues == []
        assert any(d["reason"] == "unresolved_anchor" for d in outcome.dropped)

    def test_disabled_produces_no_cues(self, assets) -> None:
        for kwargs in ({"enabled": False}, {"intensity": "off"}):
            outcome = resolve_sound_events(
                self._events(),
                assets=assets,
                offsets={"s01": 0.0},
                durations={"s01": 10.0},
                **kwargs,
            )
            assert outcome.cues == []

    def test_empty_palette_does_not_fail(self) -> None:
        """音源が無くても動画生成は続く。"""
        outcome = resolve_sound_events(
            self._events(), assets=[], offsets={"s01": 0.0}, durations={"s01": 10.0}
        )
        assert outcome.cues == []
        assert outcome.warnings

    def test_missing_timeline_is_dropped(self, assets) -> None:
        outcome = resolve_sound_events(self._events(), assets=assets, offsets={}, durations={})
        assert outcome.cues == []
        assert any(d["reason"] == "no_timeline" for d in outcome.dropped)

    def test_beat_times_are_used_when_available(self, assets) -> None:
        outcome = resolve_sound_events(
            [{"scene_id": "s01", "event": "key_point", "anchor": "beat-2.reveal"}],
            assets=assets,
            offsets={"s01": 100.0},
            durations={"s01": 10.0},
            beat_times={"s01": [1.0, 4.0, 7.0]},
        )
        assert outcome.cues[0].t_sec == pytest.approx(104.0, abs=0.01)

    def test_attributions_are_collected(self, assets) -> None:
        outcome = resolve_sound_events(
            self._events(), assets=assets, offsets={"s01": 0.0}, durations={"s01": 10.0}
        )
        attributions = used_attributions(outcome.cues, assets)
        assert attributions
        assert all(a["license"] and a["sha256"] for a in attributions)

    def test_manifest_shape(self, assets) -> None:
        outcome = resolve_sound_events(
            self._events(count=5),
            assets=assets,
            offsets={"s01": 0.0},
            durations={"s01": 30.0},
        )
        manifest = outcome.to_manifest()
        assert set(manifest) == {"cues", "dropped", "warnings"}
        assert all("sound_id" in c and "sha256" in c for c in manifest["cues"])
