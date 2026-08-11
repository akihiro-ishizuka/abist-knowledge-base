"""ffmpeg / ffprobe の呼び出し（動画結合・尺計測）。

Manim の内部が使う ffmpeg とは別に、**シーン結合のために自前で呼ぶ**層。
プロセスの起動・終了は purring の `manim_runner` と同じ流儀に揃える:

- `shell=False`、引数は配列渡し（インジェクション不可）
- タイムアウト・キャンセル時は**プロセスツリーごと**終了させる
  （`subprocess.run(timeout=)` は直接の子しか kill せず ffmpeg が孤児になる）
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from abist_kb.infrastructure.visualization.manim_runner import (
    ProcessResult,
    run_python_process,
    tail,
)

#: 結合1回あたりの既定タイムアウト（秒）。長尺でも十分な余裕を取る。
DEFAULT_CONCAT_TIMEOUT_SECONDS = 30 * 60.0


class FfmpegUnavailableError(RuntimeError):
    """ffmpeg / ffprobe が PATH に無い。"""


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def require_ffmpeg() -> tuple[str, str]:
    ffmpeg = ffmpeg_path()
    ffprobe = ffprobe_path()
    if ffmpeg is None or ffprobe is None:
        raise FfmpegUnavailableError(
            "ffmpeg / ffprobe が見つかりません。"
            "winget install Gyan.FFmpeg.Essentials などで導入してください"
        )
    return ffmpeg, ffprobe


def run_ffmpeg(
    args: list[str],
    *,
    timeout_seconds: float = DEFAULT_CONCAT_TIMEOUT_SECONDS,
    cwd: Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ProcessResult:
    """ffmpeg を実行する。

    `manim_runner.run_python_process` をそのまま流用する（実体は
    「任意の実行ファイル + 引数」を Popen + ポーリング + プロセスツリー kill で
    走らせるものなので、Python 以外にも使える）。これにより
    **キャンセル・タイムアウト時に ffmpeg の子孫を残さない**保証を共有できる。
    """
    ffmpeg, _ = require_ffmpeg()
    return run_python_process(
        python_path=ffmpeg,
        args=["-hide_banner", "-nostdin", "-y", *args],
        timeout_seconds=timeout_seconds,
        cwd=cwd,
        should_cancel=should_cancel,
    )


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration_sec: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool


def probe(path: Path, *, timeout_seconds: float = 60.0) -> MediaInfo:
    """`ffprobe` でメディア情報を読む（尺の**実測**に使う）。"""
    _, ffprobe = require_ffmpeg()
    result = run_python_process(
        python_path=ffprobe,
        args=[
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height,r_frame_rate,codec_type",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=timeout_seconds,
    )
    if result.exit_code != 0:
        return MediaInfo(None, None, None, None, False)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return MediaInfo(None, None, None, None, False)

    duration = payload.get("format", {}).get("duration")
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    fps: float | None = None
    if video and isinstance(video.get("r_frame_rate"), str):
        raw = video["r_frame_rate"]
        if "/" in raw:
            num, _, den = raw.partition("/")
            try:
                fps = float(num) / float(den) if float(den) else None
            except (ValueError, ZeroDivisionError):
                fps = None
    return MediaInfo(
        duration_sec=float(duration) if duration else None,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        fps=fps,
        has_audio=has_audio,
    )


def write_concat_list(entries: list[Path], list_path: Path) -> Path:
    """concat demuxer 用のリストを書く。

    パスは**リストからの相対**にせず絶対パスにし、シングルクォートを
    エスケープする（日本語パス・空白を含むパスで壊れないため）。
    """
    lines = []
    for entry in entries:
        escaped = str(entry.resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


@dataclass(frozen=True, slots=True)
class ConcatResult:
    ok: bool
    output: Path | None = None
    code: str | None = None
    message: str | None = None
    stderr_tail: str | None = None
    duration_sec: float | None = None


def concat_videos(
    scenes: list[Path],
    output: Path,
    *,
    width: int,
    height: int,
    fps: int,
    work_dir: Path,
    timeout_seconds: float = DEFAULT_CONCAT_TIMEOUT_SECONDS,
    should_cancel: Callable[[], bool] | None = None,
) -> ConcatResult:
    """複数のシーン動画を1本へ結合する。

    **再エンコードする。** Manim の出力はシーンごとに GOP 構造が違い、
    stream copy では境目で壊れる。解像度・fps をここで統一し、
    手動 YouTube 登録互換（H.264 / yuv420p）に揃える。
    """
    if not scenes:
        return ConcatResult(ok=False, code="NO_SCENES", message="結合するシーンがありません")
    missing = [s for s in scenes if not s.is_file()]
    if missing:
        return ConcatResult(
            ok=False,
            code="SCENE_MISSING",
            message="シーン動画が見つかりません: " + " / ".join(str(m) for m in missing),
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    list_path = write_concat_list(scenes, work_dir / "concat.txt")
    output.parent.mkdir(parents=True, exist_ok=True)

    result = run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-movflags",
            "+faststart",
            str(output),
        ],
        timeout_seconds=timeout_seconds,
        should_cancel=should_cancel,
    )
    if result.cancelled:
        return ConcatResult(
            ok=False,
            code="RENDER_CANCELLED",
            message="利用者の要求により結合を中止しました",
            stderr_tail=tail(result.stderr),
        )
    if result.timed_out:
        return ConcatResult(
            ok=False,
            code="CONCAT_TIMEOUT",
            message="結合がタイムアウトしました",
            stderr_tail=tail(result.stderr),
        )
    if result.exit_code != 0 or not output.exists():
        return ConcatResult(
            ok=False,
            code="CONCAT_FAILED",
            message=f"ffmpeg が失敗しました (exit={result.exit_code})",
            stderr_tail=tail(result.stderr),
        )
    return ConcatResult(ok=True, output=output, duration_sec=probe(output).duration_sec)


def mux_audio(
    video: Path,
    audio: Path | None,
    output: Path,
    *,
    timeout_seconds: float = DEFAULT_CONCAT_TIMEOUT_SECONDS,
    should_cancel: Callable[[], bool] | None = None,
) -> ConcatResult:
    """映像へ音声を合成する。`audio` が None なら**無音のまま**コピーする。

    無音でも完走できることが MVP の要件なので、音声が無い経路を特別扱いしない。
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    if audio is None:
        args = ["-i", str(video), "-c", "copy", str(output)]
    else:
        args = [
            "-i",
            str(video),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output),
        ]
    result = run_ffmpeg(args, timeout_seconds=timeout_seconds, should_cancel=should_cancel)
    if result.cancelled:
        return ConcatResult(ok=False, code="RENDER_CANCELLED", message="中止しました")
    if result.exit_code != 0 or not output.exists():
        return ConcatResult(
            ok=False,
            code="MUX_FAILED",
            message=f"音声合成に失敗しました (exit={result.exit_code})",
            stderr_tail=tail(result.stderr),
        )
    return ConcatResult(ok=True, output=output, duration_sec=probe(output).duration_sec)


__all__ = [
    "DEFAULT_CONCAT_TIMEOUT_SECONDS",
    "ConcatResult",
    "FfmpegUnavailableError",
    "MediaInfo",
    "concat_videos",
    "ffmpeg_path",
    "ffprobe_path",
    "mux_audio",
    "probe",
    "require_ffmpeg",
    "run_ffmpeg",
    "write_concat_list",
]
