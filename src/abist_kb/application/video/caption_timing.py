"""テロップ（画面に焼き込む字幕）の尺見積り。

**この動画にナレーション音声は無い。** 台本の `narration.text` は読み上げ原稿では
なく画面へ出すテロップであり、シーンの尺は「読み手が黙読して追い切れるか」で決まる。
音声用の読み上げ速度（`tts_provider.CHARS_PER_SECOND = 6.5`）をそのまま使うと、
声で聞くより遅い黙読に対して尺が足りなくなるため、別の定数を持つ。

尺に関わる定数はここを唯一の出所にする（`duration_planner` と `pipeline` が参照）。
"""

from __future__ import annotations

#: 黙読で追える速度（1秒あたりの文字数）。日本語のテロップを想定。
#: 音声の読み上げ（6.5字/秒）より遅い値にしているのは、視聴者が「読む」ためで、
#: 実動画を見て調整する前提の初期値。
READING_CHARS_PER_SECOND = 4.5
#: 1シーンのテロップ表示の下限・上限（見積もりが極端にならないように）。
MIN_CAPTION_SEC = 1.5
MAX_CAPTION_SEC = 60.0


def estimate_caption_duration(text: str) -> float:
    """テロップ文から、読み切るのに要る秒数を見積もる。"""
    length = len(str(text or "").strip())
    if length == 0:
        return 0.0
    seconds = length / READING_CHARS_PER_SECOND
    return round(min(max(seconds, MIN_CAPTION_SEC), MAX_CAPTION_SEC), 2)


def caption_chars(text: str) -> int:
    """尺見積りの根拠になる文字数（空白を除いた実文字数）。"""
    return len(str(text or "").strip())


__all__ = [
    "MAX_CAPTION_SEC",
    "MIN_CAPTION_SEC",
    "READING_CHARS_PER_SECOND",
    "caption_chars",
    "estimate_caption_duration",
]
