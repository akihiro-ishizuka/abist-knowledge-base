"""TTS の境界（プロバイダ非依存）。

**外部 TTS の認証情報が無くても動画パイプラインは完走する。**
`none`（無音）と `manual`（人が用意した音声）が常に使えるので、
プロバイダ未設定は「実装が未完成」を意味しない。

`ChatProvider`（`infrastructure/ai/chat_provider.py`）と同じく Protocol で
境界を切り、実装は差し替え可能にする。
"""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from abist_kb.infrastructure.video.ffmpeg_runner import probe

#: 日本語ナレーションの読み上げ速度（1秒あたりの文字数）。
#: `none` プロバイダが尺を見積もるために使う。実測ではないので、
#: 実 TTS を使う場合は必ず生成音声を ffprobe で測り直す。
CHARS_PER_SECOND = 6.5
#: 1発話の下限・上限（見積もりが極端にならないように）。
MIN_SPEECH_SEC = 1.2
MAX_SPEECH_SEC = 60.0

_SAMPLE_RATE = 44100


@dataclass(frozen=True, slots=True)
class SpeechResult:
    """1発話の合成結果。"""

    ok: bool
    audio_path: Path | None = None
    duration_sec: float = 0.0
    provider: str = "none"
    code: str | None = None
    message: str | None = None
    #: 実ファイルを測って得た尺か（見積もりなら False）。
    measured: bool = False


class TtsProvider(Protocol):
    """TTS プロバイダの境界。

    `synthesize` は**必ず尺を返す**。呼び出し側はこの尺で映像を合わせるので、
    「生成したが尺が分からない」状態を作らせない。
    """

    name: str

    def synthesize(self, text: str, out_path: Path) -> SpeechResult: ...


def estimate_duration(text: str) -> float:
    """文字数から尺を見積もる（`none` プロバイダ用）。"""
    if not text:
        return 0.0
    seconds = len(text) / CHARS_PER_SECOND
    return max(MIN_SPEECH_SEC, min(MAX_SPEECH_SEC, seconds))


def write_silence(path: Path, duration_sec: float) -> Path:
    """無音 WAV を書く（決定的。外部依存なし）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(max(0.0, duration_sec) * _SAMPLE_RATE)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * frames)
    return path


def write_test_tone(path: Path, duration_sec: float, *, freq: float = 440.0) -> Path:
    """決定的なテスト用音声（純音）を書く。

    テストで「音声が実際に合成された」ことを確認するために使う。
    同じ入力からは**バイト単位で同じファイル**になる。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(max(0.0, duration_sec) * _SAMPLE_RATE)
    payload = bytearray()
    for index in range(frames):
        value = int(12000 * math.sin(2 * math.pi * freq * index / _SAMPLE_RATE))
        payload += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SAMPLE_RATE)
        handle.writeframes(bytes(payload))
    return path


class NoneTtsProvider:
    """音声を作らないプロバイダ。**尺だけを見積もる。**

    無音動画を作るときの既定。字幕とシーン尺は成立するので、
    「TTS 無しでも視聴できる動画」が作れる。
    """

    name = "none"

    def synthesize(self, text: str, out_path: Path) -> SpeechResult:
        duration = estimate_duration(text)
        return SpeechResult(
            ok=True, audio_path=None, duration_sec=duration, provider=self.name, measured=False
        )


class SilenceTtsProvider:
    """無音 WAV を実際に書き出すプロバイダ。

    `none` と違い**実ファイルを作る**ので、音声トラック付きの動画を
    外部 TTS 無しで検証できる。
    """

    name = "silence"

    def synthesize(self, text: str, out_path: Path) -> SpeechResult:
        duration = estimate_duration(text)
        write_silence(out_path, duration)
        return SpeechResult(
            ok=True,
            audio_path=out_path,
            duration_sec=duration,
            provider=self.name,
            measured=False,
        )


class TestToneTtsProvider:
    """決定的なテスト用音声を書き出すプロバイダ（テスト専用）。"""

    name = "test_tone"

    def synthesize(self, text: str, out_path: Path) -> SpeechResult:
        duration = estimate_duration(text)
        write_test_tone(out_path, duration)
        info = probe(out_path)
        return SpeechResult(
            ok=True,
            audio_path=out_path,
            duration_sec=info.duration_sec or duration,
            provider=self.name,
            measured=info.duration_sec is not None,
        )


class ManualAudioProvider:
    """人が用意した音声を使う。

    `audio_dir/<scene_id>.wav|mp3|m4a` を探し、**実ファイルを ffprobe で測る**。
    外部 TTS が無くてもナレーション付きの動画を作れる経路。
    """

    name = "manual"
    _SUFFIXES = (".wav", ".mp3", ".m4a", ".aac", ".flac")

    def __init__(self, audio_dir: Path) -> None:
        self._audio_dir = audio_dir

    def find(self, scene_id: str) -> Path | None:
        for suffix in self._SUFFIXES:
            candidate = self._audio_dir / f"{scene_id}{suffix}"
            if candidate.is_file():
                return candidate
        return None

    def synthesize_scene(self, scene_id: str, text: str) -> SpeechResult:
        found = self.find(scene_id)
        if found is None:
            return SpeechResult(
                ok=False,
                provider=self.name,
                code="MANUAL_AUDIO_NOT_FOUND",
                message=f"{scene_id} の音声が {self._audio_dir} にありません",
                duration_sec=estimate_duration(text),
            )
        info = probe(found)
        if info.duration_sec is None:
            return SpeechResult(
                ok=False,
                provider=self.name,
                code="MANUAL_AUDIO_UNREADABLE",
                message=f"{found} の尺を測れません",
            )
        return SpeechResult(
            ok=True,
            audio_path=found,
            duration_sec=info.duration_sec,
            provider=self.name,
            measured=True,
        )

    def synthesize(self, text: str, out_path: Path) -> SpeechResult:
        return self.synthesize_scene(out_path.stem, text)


def build_provider(name: str, *, audio_dir: Path | None = None) -> TtsProvider:
    """名前からプロバイダを作る。未知の名前は `none` へ落とす（動画は作れる）。"""
    if name == "manual" and audio_dir is not None:
        return ManualAudioProvider(audio_dir)
    if name == "silence":
        return SilenceTtsProvider()
    if name == "test_tone":
        return TestToneTtsProvider()
    return NoneTtsProvider()


__all__ = [
    "CHARS_PER_SECOND",
    "MAX_SPEECH_SEC",
    "MIN_SPEECH_SEC",
    "ManualAudioProvider",
    "NoneTtsProvider",
    "SilenceTtsProvider",
    "SpeechResult",
    "TestToneTtsProvider",
    "TtsProvider",
    "build_provider",
    "estimate_duration",
    "write_silence",
    "write_test_tone",
]
