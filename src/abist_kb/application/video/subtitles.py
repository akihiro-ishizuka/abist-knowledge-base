"""字幕（SRT / VTT / 焼き込み）。

ナレーション文と**実測タイムライン**から字幕を組む。
尺は Phase 4 の `SceneTiming` が決めたものを使い、ここで独自に推測しない。

日本語の折り返しは purring の `layout.wrap_cjk` と同じ考え方
（CJK 幅を数え、禁則を追い出しで処理）を使う。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 1行あたりの最大文字数（全角換算）。読みやすさの実用値。
DEFAULT_MAX_CHARS_PER_LINE = 20
#: 1キューの最大行数。
DEFAULT_MAX_LINES = 2
#: 1キューの最短表示時間（短すぎると読めない）。
MIN_CUE_SEC = 1.0

#: 文の区切り（ここで優先的に割る）。
_SENTENCE_END = "。．!？?！"
#: 行頭に置きたくない文字。
_NO_LINE_START = "。、，．・）」』】〉》!?！？：；ー々ぁぃぅぇぉっゃゅょゎ…"


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    index: int
    start_sec: float
    end_sec: float
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    cues: list[SubtitleCue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _display_width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F", "A") else 1 for c in text)


def split_sentences(text: str) -> list[str]:
    """文末で分ける（区切りが無ければ全体を1文として扱う）。"""
    if not text:
        return []
    parts = re.split(rf"(?<=[{re.escape(_SENTENCE_END)}])", text)
    return [p.strip() for p in parts if p.strip()]


def wrap_line(text: str, max_chars: int) -> list[str]:
    """全角換算 `max_chars` で折り返す（行頭禁則は追い出し）。"""
    budget = max_chars * 2  # 全角1文字=2カラム
    lines: list[str] = []
    current = ""
    width = 0
    for char in text:
        char_width = _display_width(char)
        if width + char_width <= budget:
            current += char
            width += char_width
            continue
        if char in _NO_LINE_START and len(current) > 1:
            lines.append(current[:-1])
            current = current[-1] + char
        else:
            lines.append(current)
            current = char
        width = _display_width(current)
    if current:
        lines.append(current)
    return lines or [""]


def chunk_narration(
    text: str, *, max_chars: int = DEFAULT_MAX_CHARS_PER_LINE, max_lines: int = DEFAULT_MAX_LINES
) -> list[list[str]]:
    """ナレーションを「1キュー分の行の並び」へ分ける。"""
    cues: list[list[str]] = []
    for sentence in split_sentences(text):
        lines = wrap_line(sentence, max_chars)
        for start in range(0, len(lines), max_lines):
            cues.append(lines[start : start + max_lines])
    return cues


def build_track(
    scenes: list[dict[str, Any]],
    *,
    offsets: dict[str, float],
    durations: dict[str, float],
    max_chars: int = DEFAULT_MAX_CHARS_PER_LINE,
    max_lines: int = DEFAULT_MAX_LINES,
) -> SubtitleTrack:
    """シーンのナレーションから字幕トラックを組む。

    **シーンの実測尺を等分する。** 文字数比で配分すると、短い文が一瞬で消えて
    読めなくなるため、下限（`MIN_CUE_SEC`）を守りつつ均等に割る。
    """
    cues: list[SubtitleCue] = []
    warnings: list[str] = []
    index = 1

    for scene in scenes:
        scene_id = str(scene.get("id"))
        narration = (scene.get("narration") or {}).get("text")
        if not narration:
            continue
        start = offsets.get(scene_id)
        duration = durations.get(scene_id)
        if start is None or duration is None:
            warnings.append(f"{scene_id}: タイムラインが無いため字幕を作れません")
            continue

        chunks = chunk_narration(narration, max_chars=max_chars, max_lines=max_lines)
        if not chunks:
            continue
        per_cue = duration / len(chunks)
        if per_cue < MIN_CUE_SEC:
            warnings.append(
                f"{scene_id}: 字幕が {len(chunks)} 枚に対しシーンが短いため"
                "表示時間が下限を下回ります"
            )
        for position, lines in enumerate(chunks):
            cue_start = start + per_cue * position
            cue_end = cue_start + max(per_cue, MIN_CUE_SEC * 0.5)
            cues.append(
                SubtitleCue(
                    index=index,
                    start_sec=round(cue_start, 3),
                    end_sec=round(min(cue_end, start + duration), 3),
                    lines=lines,
                )
            )
            index += 1
    return SubtitleTrack(cues=cues, warnings=warnings)


def _timestamp(seconds: float, *, separator: str) -> str:
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def to_srt(track: SubtitleTrack) -> str:
    blocks: list[str] = []
    for cue in track.cues:
        blocks.append(
            f"{cue.index}\n"
            f"{_timestamp(cue.start_sec, separator=',')} --> "
            f"{_timestamp(cue.end_sec, separator=',')}\n"
            f"{cue.text}\n"
        )
    return "\n".join(blocks)


def to_vtt(track: SubtitleTrack) -> str:
    blocks = ["WEBVTT\n"]
    for cue in track.cues:
        blocks.append(
            f"{_timestamp(cue.start_sec, separator='.')} --> "
            f"{_timestamp(cue.end_sec, separator='.')}\n"
            f"{cue.text}\n"
        )
    return "\n".join(blocks)


def write_subtitles(
    track: SubtitleTrack, out_dir: Path, *, stem: str = "narration"
) -> dict[str, Path]:
    """SRT と VTT を書き出す（手動 YouTube 登録では SRT を使う）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    srt = out_dir / f"{stem}.srt"
    vtt = out_dir / f"{stem}.vtt"
    srt.write_text(to_srt(track), encoding="utf-8")
    vtt.write_text(to_vtt(track), encoding="utf-8")
    return {"srt": srt, "vtt": vtt}


def burn_in_filter(srt_path: Path, *, font_size: int = 28) -> str:
    """焼き込み用の ffmpeg フィルタ。

    Windows のパスは `subtitles` フィルタでエスケープが要る
    （`C:\\x` の `:` がオプション区切りと解釈されるため）。
    """
    escaped = str(srt_path.resolve()).replace("\\", "/").replace(":", r"\:")
    style = f"FontName=Yu Gothic UI,FontSize={font_size},Outline=2,Shadow=0"
    return f"subtitles='{escaped}':force_style='{style}'"


__all__ = [
    "DEFAULT_MAX_CHARS_PER_LINE",
    "DEFAULT_MAX_LINES",
    "MIN_CUE_SEC",
    "SubtitleCue",
    "SubtitleTrack",
    "build_track",
    "burn_in_filter",
    "chunk_narration",
    "split_sentences",
    "to_srt",
    "to_vtt",
    "wrap_line",
    "write_subtitles",
]
