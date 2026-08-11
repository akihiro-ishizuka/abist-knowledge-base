"""Phase 4: TtsProvider / none / 手動音声 / 音声実測同期。

**外部 TTS の設定が無くても完走する**ことと、**音声を黙って切らない**ことの
回帰テスト。
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from abist_kb.application.video.audio_sync import (
    MAX_TEMPO,
    atempo_filter,
    plan_scene_timing,
    plan_timeline,
    scene_offsets,
)
from abist_kb.infrastructure.video.tts_provider import (
    ManualAudioProvider,
    NoneTtsProvider,
    SilenceTtsProvider,
    build_provider,
    estimate_duration,
    write_silence,
    write_test_tone,
)


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


class TestProviders:
    def test_none_provider_estimates_without_files(self, tmp_path: Path) -> None:
        """`none` は音声を作らないが尺は返す（無音動画が作れる）。"""
        result = NoneTtsProvider().synthesize("これはテストです", tmp_path / "a.wav")
        assert result.ok
        assert result.audio_path is None
        assert result.duration_sec > 0
        assert result.measured is False

    def test_silence_provider_writes_a_real_file(self, tmp_path: Path) -> None:
        result = SilenceTtsProvider().synthesize("あ" * 65, tmp_path / "a.wav")
        assert result.ok
        assert result.audio_path is not None and result.audio_path.is_file()
        assert abs(_wav_duration(result.audio_path) - result.duration_sec) < 0.05

    def test_estimate_is_monotonic_and_bounded(self) -> None:
        assert estimate_duration("") == 0.0
        short = estimate_duration("あ")
        long = estimate_duration("あ" * 200)
        assert short < long
        assert short >= 1.2
        assert long <= 60.0

    def test_test_tone_is_deterministic(self, tmp_path: Path) -> None:
        """同じ入力からはバイト単位で同じ音声（決定的なテスト用音声）。"""
        first = write_test_tone(tmp_path / "a.wav", 0.5)
        second = write_test_tone(tmp_path / "b.wav", 0.5)
        assert first.read_bytes() == second.read_bytes()

    def test_unknown_provider_falls_back_to_none(self) -> None:
        assert build_provider("azure-neural").name == "none"

    def test_build_manual_requires_dir(self, tmp_path: Path) -> None:
        assert build_provider("manual", audio_dir=tmp_path).name == "manual"
        # audio_dir が無ければ none へ落ちる（動画は作れる）
        assert build_provider("manual").name == "none"


class TestManualAudio:
    def test_finds_and_measures_real_audio(self, tmp_path: Path) -> None:
        """人が用意した音声を実測して使う（外部 TTS 不要の経路）。"""
        audio_dir = tmp_path / "audio"
        write_silence(audio_dir / "s02.wav", 3.0)
        provider = ManualAudioProvider(audio_dir)
        result = provider.synthesize_scene("s02", "ナレーション")
        assert result.ok
        assert result.measured is True, "手動音声は実測する"
        assert abs(result.duration_sec - 3.0) < 0.1

    def test_missing_audio_is_reported(self, tmp_path: Path) -> None:
        provider = ManualAudioProvider(tmp_path)
        result = provider.synthesize_scene("s99", "本文")
        assert not result.ok
        assert result.code == "MANUAL_AUDIO_NOT_FOUND"
        # 見積もり尺は返すので、無音で続行する判断ができる
        assert result.duration_sec > 0


class TestSceneTiming:
    def test_short_narration_keeps_video_length(self) -> None:
        timing, error = plan_scene_timing("s1", video_sec=10.0, narration_sec=3.0)
        assert error is None
        assert timing.strategy == "as_is"
        assert timing.final_sec == 10.0

    def test_longer_narration_extends_the_scene(self) -> None:
        """情報を削らずに映像側を伸ばす。"""
        timing, error = plan_scene_timing("s1", video_sec=5.0, narration_sec=8.0)
        assert error is None
        assert timing.strategy == "pad"
        assert timing.final_sec >= 8.0

    def test_split_before_tempo_change(self) -> None:
        """上限超過はまず分割で受ける（話速を変える前に）。"""
        timing, error = plan_scene_timing(
            "s1", video_sec=5.0, narration_sec=25.0, max_scene_sec=15.0
        )
        assert error is None
        assert timing.strategy == "split"
        assert timing.split_into >= 2
        assert timing.warnings

    def test_tempo_within_tolerance(self) -> None:
        """分割で解けない僅かな超過は許容範囲の話速調整で吸収する。"""
        timing, error = plan_scene_timing(
            "s1", video_sec=5.0, narration_sec=10.5, max_scene_sec=10.0
        )
        assert error is None
        assert timing.strategy in ("split", "atempo")
        if timing.strategy == "atempo":
            assert 1.0 < timing.tempo <= MAX_TEMPO

    def test_impossible_case_is_an_explicit_error(self) -> None:
        """⚠ 黙って切らない。収まらないなら明示エラーにする。"""
        timing, error = plan_scene_timing(
            "s1", video_sec=5.0, narration_sec=300.0, max_scene_sec=5.0
        )
        assert timing is None
        assert error is not None
        assert error.code == "NARRATION_TOO_LONG"
        assert "台本を短く" in error.message

    def test_no_narration_is_fine(self) -> None:
        timing, error = plan_scene_timing("s1", video_sec=6.0, narration_sec=0.0)
        assert error is None
        assert timing.final_sec == 6.0


class TestTimeline:
    def _scenes(self) -> list[dict]:
        return [{"id": "s01"}, {"id": "s02"}, {"id": "s03"}]

    def test_total_and_offsets(self) -> None:
        result = plan_timeline(
            self._scenes(),
            scene_durations={"s01": 5.0, "s02": 6.0, "s03": 4.0},
            narration_durations={"s01": 2.0, "s02": 9.0, "s03": 0.0},
        )
        assert result.ok
        assert len(result.timings) == 3
        # s02 はナレーションに合わせて伸びる
        assert result.timings[1].final_sec > 6.0
        offsets = scene_offsets(result.timings)
        assert offsets["s01"] == 0.0
        assert offsets["s02"] == result.timings[0].final_sec
        assert result.total_sec == pytest.approx(sum(t.final_sec for t in result.timings), abs=0.01)

    def test_failure_is_reported_per_scene(self) -> None:
        result = plan_timeline(
            self._scenes(),
            scene_durations={"s01": 5.0, "s02": 5.0, "s03": 5.0},
            narration_durations={"s02": 500.0},
            max_scene_sec=6.0,
        )
        assert not result.ok
        assert [e.code for e in result.errors] == ["NARRATION_TOO_LONG"]
        assert result.errors[0].scene_id == "s02"

    def test_no_narration_at_all_still_works(self) -> None:
        """TTS 未設定（全シーン無音）でもタイムラインは成立する。"""
        result = plan_timeline(
            self._scenes(),
            scene_durations={"s01": 5.0, "s02": 5.0, "s03": 5.0},
            narration_durations={},
        )
        assert result.ok
        assert all(t.strategy == "as_is" for t in result.timings)
        assert result.total_sec == pytest.approx(15.0, abs=0.01)


class TestAtempoFilter:
    def test_identity_returns_none(self) -> None:
        assert atempo_filter(1.0) is None

    def test_within_range(self) -> None:
        assert atempo_filter(1.1) == "atempo=1.100"

    @pytest.mark.parametrize("tempo", [0.5, 1.5, 2.0])
    def test_out_of_range_is_rejected(self, tempo: float) -> None:
        assert atempo_filter(tempo) is None
