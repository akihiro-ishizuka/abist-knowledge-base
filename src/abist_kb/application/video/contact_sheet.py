"""各シーンの代表フレームを並べたコンタクトシートを生成する。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from abist_kb.application.video.thumbnail import extract_frame
from abist_kb.infrastructure.video.ffmpeg_runner import FfmpegUnavailableError, run_ffmpeg

CONTACT_SHEET_FILE = Path("preview") / "contact-sheet.png"


@dataclass(frozen=True, slots=True)
class ContactSheetResult:
    ok: bool
    path: Path | None = None
    frame_count: int = 0
    warnings: list[str] = field(default_factory=list)


def build_contact_sheet(
    video: Path | None,
    project_dir: Path,
    *,
    scene_ids: list[str],
    offsets: dict[str, float],
    durations: dict[str, float],
    aspect_ratio: str = "16:9",
) -> ContactSheetResult:
    """各シーンの中央付近を抜き、1枚の一覧画像にまとめる。

    タイルの形は動画のアスペクト比に合わせる。縦型の動画を横長タイルへ詰めると
    左右が黒帯だらけになり、絵の確認という目的を果たさない。
    """
    if video is None or not video.is_file():
        return ContactSheetResult(ok=False, warnings=["動画が無いためコンタクトシートを作れません"])
    preview = project_dir / "preview"
    frames_dir = preview / "contact-frames"
    frames: list[Path] = []
    for index, scene_id in enumerate(scene_ids, start=1):
        start = float(offsets.get(scene_id, 0.0))
        duration = float(durations.get(scene_id, 0.0))
        # 切り替わりを避け、代表情報が出そろう中央寄りを採る。
        at = start + max(1.5, min(duration * 0.55, max(1.5, duration - 0.6)))
        target = frames_dir / f"{index:02d}-{scene_id}.png"
        if extract_frame(video, at, target):
            frames.append(target)
    if not frames:
        return ContactSheetResult(ok=False, warnings=["代表フレームを抽出できませんでした"])

    portrait = aspect_ratio == "9:16"
    tile_width, tile_height = (270, 480) if portrait else (480, 270)
    # 縦型はタイルが高いので列を減らす（横に4枚並べるとシート自体が横長になりすぎる）。
    columns = 3 if portrait else 4
    rows = math.ceil(len(frames) / columns)
    layout = "|".join(
        f"{(i % columns) * tile_width}_{(i // columns) * tile_height}" for i in range(len(frames))
    )
    filters = []
    labels = []
    for index in range(len(frames)):
        filters.append(
            f"[{index}:v]scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease,"
            f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2[v{index}]"
        )
        labels.append(f"[v{index}]")
    filters.append(f"{''.join(labels)}xstack=inputs={len(frames)}:layout={layout}:fill=black[out]")
    target = project_dir / CONTACT_SHEET_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = []
    for frame in frames:
        args += ["-i", str(frame)]
    args += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-frames:v",
        "1",
        "-y",
        str(target),
    ]
    try:
        result = run_ffmpeg(args, timeout_seconds=180.0)
    except FfmpegUnavailableError:
        return ContactSheetResult(
            ok=False, frame_count=len(frames), warnings=["ffmpegを利用できません"]
        )
    ok = result.exit_code == 0 and target.is_file()
    if not ok:
        return ContactSheetResult(
            ok=False,
            frame_count=len(frames),
            warnings=[f"コンタクトシートの結合に失敗しました（{columns}列×{rows}行）"],
        )
    return ContactSheetResult(ok=True, path=target, frame_count=len(frames))


__all__ = ["CONTACT_SHEET_FILE", "ContactSheetResult", "build_contact_sheet"]
