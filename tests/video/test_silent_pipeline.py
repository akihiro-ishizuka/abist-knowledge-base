"""無音前提のパイプライン（TTS を呼ばず、テロップを焼き込む）。

ナレーション音声は作らない。シーンの尺はテロップの読速から決まり、字幕は
映像に焼き込まれる。音は効果音（と将来の BGM）だけ。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from abist_kb.application.video import pipeline as pipeline_module
from abist_kb.application.video.caption_timing import estimate_caption_duration
from abist_kb.application.video.pipeline import _apply_scene_minimums, _finalize_video


def _scene(scene_id: str, kind: str, narration: str) -> dict[str, Any]:
    return {
        "id": scene_id,
        "kind": kind,
        "role": "body",
        "narration": {"text": narration},
        "scene_spec": {"scene_kind": kind, "beats": []},
    }


# -- 尺は読速から決まる -------------------------------------------------------------


def test_scene_minimum_comes_from_reading_speed() -> None:
    long_caption = "とても長い説明がここに続きます。" * 4
    scenes = [_scene("s01", "key_points", long_caption)]

    _apply_scene_minimums(scenes, None)

    assert scenes[0]["min_duration_sec"] >= estimate_caption_duration(long_caption)


def test_scene_minimum_does_not_use_the_speech_rate() -> None:
    """読み上げ速度（6.5字/秒）で見積もると、黙読には短すぎる。"""
    from abist_kb.infrastructure.video.tts_provider import estimate_duration

    caption = "あ" * 90
    scenes = [_scene("s01", "key_points", caption)]

    _apply_scene_minimums(scenes, None)

    assert scenes[0]["min_duration_sec"] > estimate_duration(caption) + 1.0


# -- 最終工程: 焼き込み ------------------------------------------------------------


def _capture_ffmpeg(monkeypatch, target: Path) -> list[str]:
    captured: list[str] = []

    def fake_run(args, **_kwargs):
        captured.extend(args)
        target.write_bytes(b"final")
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(pipeline_module, "run_ffmpeg", fake_run)
    return captured


def test_captions_are_burned_into_the_video(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "concat.mp4"
    srt = tmp_path / "narration.srt"
    target = tmp_path / "out.mp4"
    video.write_bytes(b"video")
    srt.write_text("1\n00:00:00,000 --> 00:00:03,000\n本文\n", encoding="utf-8")
    captured = _capture_ffmpeg(monkeypatch, target)

    assert _finalize_video(video, [], [], target, subtitle_path=srt)

    command = " ".join(captured)
    assert "subtitles=" in command
    assert "-c:v copy" not in command, "焼き込むなら映像は再エンコードが要る"


def test_video_is_copied_when_captions_are_disabled(tmp_path: Path, monkeypatch) -> None:
    from abist_kb.application.video.sound_events import SoundAsset, SoundCue

    video = tmp_path / "concat.mp4"
    sound = tmp_path / "accent.wav"
    target = tmp_path / "out.mp4"
    video.write_bytes(b"video")
    sound.write_bytes(b"wave")
    captured = _capture_ffmpeg(monkeypatch, target)

    asset = SoundAsset("accent", ("accent",), str(sound), "in-house", "", "a" * 64, 100)
    cue = SoundCue("s01", "key_point", "beat-1.reveal", 3.25, "accent", -16.0, "a" * 64)

    assert _finalize_video(video, [cue], [asset], target, subtitle_path=None)

    command = " ".join(captured)
    assert "subtitles=" not in command
    assert "-c:v" in command and "copy" in command


def test_sound_cues_survive_the_burn_in(tmp_path: Path, monkeypatch) -> None:
    from abist_kb.application.video.sound_events import SoundAsset, SoundCue

    video = tmp_path / "concat.mp4"
    sound = tmp_path / "accent.wav"
    srt = tmp_path / "narration.srt"
    target = tmp_path / "out.mp4"
    for path, payload in ((video, b"video"), (sound, b"wave")):
        path.write_bytes(payload)
    srt.write_text("1\n00:00:00,000 --> 00:00:03,000\n本文\n", encoding="utf-8")
    captured = _capture_ffmpeg(monkeypatch, target)

    asset = SoundAsset("accent", ("accent",), str(sound), "in-house", "", "a" * 64, 100)
    cue = SoundCue("s01", "key_point", "beat-1.reveal", 3.25, "accent", -16.0, "a" * 64)

    assert _finalize_video(video, [cue], [asset], target, subtitle_path=srt)

    command = " ".join(captured)
    assert "adelay=3250|3250" in command
    assert "volume=-16.0dB" in command
    assert "subtitles=" in command


def test_nothing_to_do_returns_false(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "concat.mp4"
    target = tmp_path / "out.mp4"
    video.write_bytes(b"video")
    _capture_ffmpeg(monkeypatch, target)

    assert not _finalize_video(video, [], [], target, subtitle_path=None)


# -- BGM ----------------------------------------------------------------------------


def _bgm_plan(tmp_path: Path):
    from abist_kb.application.video.bgm import BgmAsset, plan_bgm

    loop = tmp_path / "loop.wav"
    loop.write_bytes(b"wave")
    asset = BgmAsset("bgm_x", "neutral", str(loop), "in-house", "Abist", "a" * 64, 10_000)
    return plan_bgm(120.0, asset)


def test_bgm_is_looped_trimmed_and_faded(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "concat.mp4"
    target = tmp_path / "out.mp4"
    video.write_bytes(b"video")
    captured = _capture_ffmpeg(monkeypatch, target)

    assert _finalize_video(video, [], [], target, bgm=_bgm_plan(tmp_path))

    command = " ".join(captured)
    assert "-stream_loop" in command, "ループしていない"
    assert "atrim=duration=120" in command, "動画尺で切っていない"
    assert "afade=t=in" in command and "afade=t=out" in command
    assert "volume=-26.0dB" in command, "床の音量が効いていない"


def test_loudness_is_normalised_once_audio_exists(tmp_path: Path, monkeypatch) -> None:
    """完成尺ごとに体感音量がばらつくと、社内配布物として扱いにくい。"""
    video = tmp_path / "concat.mp4"
    target = tmp_path / "out.mp4"
    video.write_bytes(b"video")
    captured = _capture_ffmpeg(monkeypatch, target)

    assert _finalize_video(video, [], [], target, bgm=_bgm_plan(tmp_path))

    assert "loudnorm=" in " ".join(captured)


def test_bgm_and_sound_effects_are_mixed_together(tmp_path: Path, monkeypatch) -> None:
    from abist_kb.application.video.sound_events import SoundAsset, SoundCue

    video = tmp_path / "concat.mp4"
    sound = tmp_path / "accent.wav"
    target = tmp_path / "out.mp4"
    video.write_bytes(b"video")
    sound.write_bytes(b"wave")
    captured = _capture_ffmpeg(monkeypatch, target)

    asset = SoundAsset("accent", ("accent",), str(sound), "in-house", "", "a" * 64, 100)
    cue = SoundCue("s01", "key_point", "beat-1.reveal", 3.25, "accent", -16.0, "a" * 64)

    assert _finalize_video(video, [cue], [asset], target, bgm=_bgm_plan(tmp_path))

    command = " ".join(captured)
    assert "amix=inputs=2" in command, "BGM と効果音の2系統が混ざっていない"
