"""テロップ（画面焼き込み字幕）と SRT / VTT。

**この動画にナレーション音声は無い。** 台本の `narration.text` は画面に出す
テロップであり、音を切ったままでも内容が伝わることが前提。焼き込みが既定で、
SRT/VTT は手動アップロード時の字幕登録用に併せて書き出す。

シーンの尺は Phase 4 の `SceneTiming` が決めたものを使い、ここで独自に推測しない。
そのシーンの持ち時間を、キューへ**文字量に比例して**割り振るのがここの仕事。

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
#: アスペクト比ごとの1行最大文字数。
#: `tools/visualize/templates/layout.subtitle_max_chars` の写し（別 venv のため
#: import できない）。値がずれると縦型で行が溢れるので整合テストで縛る。
SUBTITLE_MAX_CHARS: dict[str, int] = {"16:9": 20, "9:16": 12}
#: アスペクト比ごとの焼き込み文字サイズ（ASS の公称値）。
#:
#: **公称値は見た目の大きさに比例しない。** libass は台本が解像度を宣言していない
#: とき `PlayResY = 288` を基準に字面と余白を拡大する。縦型（高さ 1920）は横型
#: （1080）の約1.8倍に描かれるので、公称値は**小さく**しないと1行が画面幅を超え、
#: libass が勝手に折り返して行数が増える（実際に 34 で3行になり、出典フッタを
#: 覆っていた）。値は実測で決めている。`estimated_caption_width_px` が
#: `usable_caption_width_px` に収まることをテストで縛る。
BURN_IN_FONT_SIZE: dict[str, int] = {"16:9": 26, "9:16": 12}
#: **実測値**: 公称サイズ 1 あたりの全角1文字の字送り（画素）。
#: 1080x1920 に 12pt / 20pt、1920x1080 に 26pt を焼いて計測した。
_CHAR_ADVANCE_PER_SIZE: dict[str, float] = {"16:9": 2.55, "9:16": 4.25}
#: libass が解像度宣言の無い台本に使う基準の高さと、既定の左右マージン。
_ASS_PLAY_RES_Y = 288
_ASS_DEFAULT_SIDE_MARGIN = 10
#: 焼き込みの縁取り幅（背景が明るい図の上でも読めるように）。
BURN_IN_OUTLINE: dict[str, int] = {"16:9": 2, "9:16": 3}
#: 焼き込みの下マージン。
BURN_IN_MARGIN_V: dict[str, int] = {"16:9": 22, "9:16": 40}
#: 1キューの最大行数。
DEFAULT_MAX_LINES = 2
#: 1キューの最短表示時間（短すぎると読めない）。
MIN_CUE_SEC = 1.0
#: シーン末尾に空ける時間。画面が切り替わる瞬間に文字を残さない。
TAIL_MARGIN_SEC = 0.4
#: 配分の重みに足す下駄。短いキューが一瞬で消えないようにする。
_CUE_WIDTH_OVERHEAD = 8

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


def _is_word_char(char: str) -> bool:
    """行の途中で割ってはいけない字（半角英数字と語中の記号）。

    `tools/visualize/templates/layout._is_ascii_word_char` の写し（別 venv のため
    import できない）。両者が食い違わないことはテストで固定している。
    """
    return char.isascii() and (char.isalnum() or char in "_-.")


def _break_before_word(current: str, next_char: str) -> tuple[str, str]:
    """語の途中で折り返そうとしているとき、語の直前で切り直す。

    戻り値は `(前行, 次行へ送る断片)`。切り直す必要が無ければ `("", "")`。
    「2179点」が「2」と「179点」に割れると、読み手には別の数として見える。
    """
    if not current or not _is_word_char(next_char) or not _is_word_char(current[-1]):
        return "", ""
    index = len(current)
    while index > 0 and _is_word_char(current[index - 1]):
        index -= 1
    if index == 0:
        # 行全体が1つの長い語。切り直すと無限に後退するのでそのまま折る。
        return "", ""
    return current[:index], current[index:]


def wrap_line(text: str, max_chars: int) -> list[str]:
    """全角換算 `max_chars` で折り返す（行頭禁則は追い出し、語の途中では割らない）。"""
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
        head, carry = _break_before_word(current, char)
        if head:
            lines.append(head)
            current = carry + char
        elif char in _NO_LINE_START and len(current) > 1:
            lines.append(current[:-1])
            current = current[-1] + char
        else:
            lines.append(current)
            current = char
        width = _display_width(current)
    if current:
        lines.append(current)
    return lines or [""]


def _balanced_lines(sentence: str, max_chars: int, max_lines: int) -> list[str]:
    """キューをまたぐ文を、端数の出ない行数へ均す。

    素直に折り返すと最後のキューに1行だけ残ることがあり、「と。」のような
    2文字の字幕が数秒間ぽつんと出る。行数が `max_lines` で割り切れる中で
    **最も広い幅**を選び直す（幅は宣言値を超えない＝読みやすさは落ちない）。
    """
    lines = wrap_line(sentence, max_chars)
    if len(lines) <= max_lines:
        return lines
    limit = -(-len(lines) // max_lines) * max_lines  # 切り上げてキューを埋める行数

    # 全体を行数で割った幅から始める。行数を割り切れるようにするだけでは足りず、
    # 「と。」だけの行が残る。**1行の長さを均す**ところまでやる。
    start = max(1, -(-_display_width(sentence) // (limit * 2)))
    for width in range(start, max_chars + 1):
        candidate = wrap_line(sentence, width)
        if len(candidate) <= limit:
            return candidate
    return lines


def chunk_narration(
    text: str, *, max_chars: int = DEFAULT_MAX_CHARS_PER_LINE, max_lines: int = DEFAULT_MAX_LINES
) -> list[list[str]]:
    """ナレーションを「1キュー分の行の並び」へ分ける。"""
    cues: list[list[str]] = []
    for sentence in split_sentences(text):
        lines = _balanced_lines(sentence, max_chars, max_lines)
        for start in range(0, len(lines), max_lines):
            cues.append(lines[start : start + max_lines])
    return cues


def _allocate(chunks: list[list[str]], usable: float) -> list[float]:
    """キューへ持ち時間を**文字量に比例して**割り振る。

    等分だと、短い一言が長々と居座る一方で長い文が読み切れずに消える。表示幅に
    比例させ、短いキューには下駄（`_CUE_WIDTH_OVERHEAD`）を履かせて一瞬で消えない
    ようにする。合計は必ず `usable` に一致させる（隙間を作らない）。
    """
    weights = [_display_width("".join(lines)) + _CUE_WIDTH_OVERHEAD for lines in chunks]
    total_weight = sum(weights) or 1
    return [usable * weight / total_weight for weight in weights]


def build_track(
    scenes: list[dict[str, Any]],
    *,
    offsets: dict[str, float],
    durations: dict[str, float],
    max_chars: int | None = None,
    max_lines: int = DEFAULT_MAX_LINES,
    aspect_ratio: str = "16:9",
) -> SubtitleTrack:
    """シーンのテロップ文から字幕トラックを組む。

    シーンの実測尺を**文字量に比例して**配分する。末尾は `TAIL_MARGIN_SEC` だけ
    空け、画面が切り替わる瞬間に文字が残らないようにする。
    """
    resolved_max_chars = max_chars or SUBTITLE_MAX_CHARS.get(
        aspect_ratio, DEFAULT_MAX_CHARS_PER_LINE
    )
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

        chunks = chunk_narration(narration, max_chars=resolved_max_chars, max_lines=max_lines)
        if not chunks:
            continue
        usable = max(duration - TAIL_MARGIN_SEC, duration * 0.5)
        spans = _allocate(chunks, usable)
        if min(spans) < MIN_CUE_SEC:
            warnings.append(
                f"{scene_id}: 字幕が {len(chunks)} 枚に対しシーンが短いため"
                "表示時間が下限を下回ります"
            )
        cursor = start
        for lines, span in zip(chunks, spans, strict=True):
            cue_end = min(cursor + max(span, MIN_CUE_SEC * 0.5), start + duration)
            cues.append(
                SubtitleCue(
                    index=index,
                    start_sec=round(cursor, 3),
                    end_sec=round(cue_end, 3),
                    lines=lines,
                )
            )
            cursor = cue_end
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


#: 焼き込みテロップが画面下端から占める割合。**実測値**（黒一色の動画へ2行の
#: テロップを焼き、文字の上端を測った）:
#:
#:     16:9  1920x1080  下端から 258px = 0.2389
#:     9:16  1080x1920  下端から 411px = 0.2141
#:
#: 少し広く宣言して余裕を持たせる。`tools/visualize/templates/layout
#: .CAPTION_BAND_RATIO` が写しを持ち、食い違わないことをテストで固定している。
CAPTION_BAND_RATIO: dict[str, float] = {"16:9": 0.25, "9:16": 0.23}


def caption_band_ratio(aspect_ratio: str) -> float:
    """テロップが占める画面下端からの割合。図はここへ入ってはいけない。"""
    return CAPTION_BAND_RATIO.get(aspect_ratio, CAPTION_BAND_RATIO["16:9"])


def estimated_caption_width_px(aspect_ratio: str) -> float:
    """1行を最大文字数まで詰めたときの、焼き込み後の実描画幅（画素）。

    これが映像の幅を超えると libass が勝手に折り返し、行数が増えて本文や出典に
    かぶる。設定を変えたときに気付けるよう、テストでここを見る。
    """
    nominal = BURN_IN_FONT_SIZE.get(aspect_ratio, BURN_IN_FONT_SIZE["16:9"])
    chars = SUBTITLE_MAX_CHARS.get(aspect_ratio, DEFAULT_MAX_CHARS_PER_LINE)
    advance = _CHAR_ADVANCE_PER_SIZE.get(aspect_ratio, _CHAR_ADVANCE_PER_SIZE["16:9"])
    return chars * nominal * advance


def usable_caption_width_px(aspect_ratio: str) -> float:
    """字幕に使える横幅（画素）。左右マージンも映像の高さで拡大される。"""
    from abist_kb.domain.video_project_spec import frame_pixels

    width, height = frame_pixels(aspect_ratio, "standard")
    margin = _ASS_DEFAULT_SIDE_MARGIN * height / _ASS_PLAY_RES_Y
    return max(1.0, width - margin * 2)


def burn_in_filter(
    srt_path: Path, *, aspect_ratio: str = "16:9", font_size: int | None = None
) -> str:
    """焼き込み用の ffmpeg フィルタ。

    無音で観る前提なので、これが本体の字幕。図の上に重なっても読めるよう、
    縁取りを必ず付ける（影ではなく縁取り: 背景の明暗に依存しない）。

    Windows のパスは `subtitles` フィルタでエスケープが要る
    （`C:\\x` の `:` がオプション区切りと解釈されるため）。
    """
    escaped = str(srt_path.resolve()).replace("\\", "/").replace(":", r"\:")
    size = font_size or BURN_IN_FONT_SIZE.get(aspect_ratio, BURN_IN_FONT_SIZE["16:9"])
    outline = BURN_IN_OUTLINE.get(aspect_ratio, BURN_IN_OUTLINE["16:9"])
    margin = BURN_IN_MARGIN_V.get(aspect_ratio, BURN_IN_MARGIN_V["16:9"])
    style = (
        f"FontName=Yu Gothic UI,FontSize={size},Bold=1,"
        f"Outline={outline},Shadow=0,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00141414,"
        f"MarginV={margin}"
    )
    return f"subtitles='{escaped}':force_style='{style}'"


__all__ = [
    "BURN_IN_FONT_SIZE",
    "CAPTION_BAND_RATIO",
    "caption_band_ratio",
    "BURN_IN_MARGIN_V",
    "BURN_IN_OUTLINE",
    "DEFAULT_MAX_CHARS_PER_LINE",
    "DEFAULT_MAX_LINES",
    "MIN_CUE_SEC",
    "SUBTITLE_MAX_CHARS",
    "TAIL_MARGIN_SEC",
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
