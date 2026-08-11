"""サムネイル生成（`thumbnail.png` と候補フレーム）。

手動投稿時のカスタムサムネイルに使う。**必ず何かを出す** —— 失敗しても
黒背景 + タイトルのフォールバックへ落とし、動画生成そのものは止めない。

第一候補は**タイトルカードのフレーム**。動画の先頭に必ずあり、文字が大きく、
1280x720 以上の要件を素直に満たすため。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from abist_kb.infrastructure.video.ffmpeg_runner import (
    FfmpegUnavailableError,
    probe,
    run_ffmpeg,
)

THUMBNAIL_FILE = "thumbnail.png"
#: 候補フレームの置き場（人が選び直せるように残す）。
CANDIDATES_DIR = Path("preview") / "candidate-thumbs"
#: 手動登録時に求められる最小寸法。
MIN_WIDTH = 1280
MIN_HEIGHT = 720
#: 候補フレームの最大数。
MAX_CANDIDATES = 6


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    ok: bool
    path: Path | None = None
    width: int = 0
    height: int = 0
    #: フォールバック（単色背景）で作ったか。
    fallback: bool = False
    candidates: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _write_solid_png(path: Path, width: int, height: int, rgb: tuple[int, int, int]) -> Path:
    """単色 PNG を外部依存なしで書く（フォールバック用）。

    Pillow に依存しないのは、**サムネイル生成の失敗経路が新しい依存で
    さらに壊れるのを避ける**ため。ここは「最後に必ず成功する」道でなければ
    意味がない。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    return path


def extract_frame(video: Path, at_sec: float, target: Path) -> bool:
    """指定秒のフレームを1枚抜く（成功したら True）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run_ffmpeg(
            [
                "-ss",
                f"{max(0.0, at_sec):.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(target),
            ],
            timeout_seconds=120.0,
        )
    except FfmpegUnavailableError:
        return False
    return result.exit_code == 0 and target.is_file()


def build_thumbnail(
    video: Path | None,
    project_dir: Path,
    *,
    chapters: list[dict] | None = None,
    duration_sec: float | None = None,
) -> ThumbnailResult:
    """`thumbnail.png` と候補フレームを作る。

    1. 各章の先頭から候補フレームを抜く（人が選び直せるように残す）
    2. タイトルカード付近（2 秒地点）を本採用にする
    3. どれも失敗したら黒背景の PNG を書く（**必ず成果物を残す**）
    """
    warnings: list[str] = []
    target = project_dir / THUMBNAIL_FILE

    if video is None or not video.is_file():
        _write_solid_png(target, MIN_WIDTH, MIN_HEIGHT, (16, 16, 16))
        return ThumbnailResult(
            ok=True,
            path=target,
            width=MIN_WIDTH,
            height=MIN_HEIGHT,
            fallback=True,
            warnings=["動画が無いため単色のサムネイルを生成しました"],
        )

    info = probe(video)
    total = duration_sec or info.duration_sec or 0.0

    candidates: list[Path] = []
    candidate_dir = project_dir / CANDIDATES_DIR
    points = [c.get("start_sec", 0.0) for c in (chapters or [])][:MAX_CANDIDATES]
    if not points:
        points = [t for t in (2.0, total * 0.25, total * 0.5, total * 0.75) if t < total]
    for index, at in enumerate(points):
        # 章の切り替わりちょうどはトランジション中なので少し後ろへずらす
        shot = candidate_dir / f"{index + 1:02d}.png"
        if extract_frame(video, float(at) + 1.5, shot):
            candidates.append(shot)

    if extract_frame(video, min(2.0, max(0.0, total - 0.5)), target):
        size = probe(target)
        width = size.width or info.width or 0
        height = size.height or info.height or 0
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            warnings.append(
                f"サムネイルが {width}x{height} で手動登録の推奨（{MIN_WIDTH}x{MIN_HEIGHT}）"
                "を下回ります"
            )
        return ThumbnailResult(
            ok=True,
            path=target,
            width=width,
            height=height,
            candidates=candidates,
            warnings=warnings,
        )

    _write_solid_png(target, MIN_WIDTH, MIN_HEIGHT, (16, 16, 16))
    warnings.append("フレームを抽出できなかったため単色のサムネイルを生成しました")
    return ThumbnailResult(
        ok=True,
        path=target,
        width=MIN_WIDTH,
        height=MIN_HEIGHT,
        fallback=True,
        candidates=candidates,
        warnings=warnings,
    )


__all__ = [
    "CANDIDATES_DIR",
    "MAX_CANDIDATES",
    "MIN_HEIGHT",
    "MIN_WIDTH",
    "THUMBNAIL_FILE",
    "ThumbnailResult",
    "build_thumbnail",
    "extract_frame",
]
