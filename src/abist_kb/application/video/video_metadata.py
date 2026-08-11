"""手動投稿パック用のメタデータ（`video-metadata.json`）とチャプター。

**API アップロードは行わない。** ここが作るのは、人が会社の YouTube へ
手動登録するときにコピーする材料一式。文字数の上限検査は「手動登録時に弾かれ
ないための参考」であって、投稿 API とは無関係。

決定的であること（時刻を除いて同じ入力から同じ出力）を守る。生成のたびに
説明文が揺れると、承認レコードのハッシュが毎回無効になる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 手動登録時の参考上限（YouTube 側の制限に合わせた互換チェック）。
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS = 15
MAX_TAG_CHARS = 30
#: チャプターとして成立する最低数（YouTube の自動チャプターは3つ以上・0秒開始）。
MIN_CHAPTERS_FOR_YOUTUBE = 3

#: `public_candidate=false` のときに説明文へ必ず入れる注意書き。
INTERNAL_ONLY_NOTICE = "※ この動画は社内限定です。社外へ公開・共有しないでください。"

METADATA_FILE = "video-metadata.json"


@dataclass(frozen=True, slots=True)
class Chapter:
    start_sec: float
    title: str

    @property
    def timestamp(self) -> str:
        total = int(max(0.0, self.start_sec))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def to_dict(self) -> dict[str, Any]:
        return {"start_sec": round(self.start_sec, 2), "title": self.title}


@dataclass(frozen=True, slots=True)
class MetadataResult:
    ok: bool
    metadata: dict[str, Any] | None = None
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    code: str | None = None


def build_chapters(
    scenes: list[dict[str, Any]], offsets: dict[str, float], *, title: str
) -> list[Chapter]:
    """章扉シーンの開始位置からチャプターを作る。

    **0 秒から始める。** 手動登録時に先頭が 0:00 でないとチャプターとして
    認識されないため、最初の章が 0 秒でなければ導入の章を足す。
    """
    chapters: list[Chapter] = []
    for scene in scenes:
        if scene.get("kind") != "chapter":
            continue
        start = offsets.get(str(scene.get("id")))
        if start is None:
            continue
        label = str(scene.get("title") or f"第 {scene.get('chapter_index', len(chapters) + 1)} 章")
        chapters.append(Chapter(start_sec=float(start), title=label))

    chapters.sort(key=lambda c: c.start_sec)
    if not chapters or chapters[0].start_sec > 0.5:
        chapters.insert(0, Chapter(start_sec=0.0, title=f"はじめに（{title}）"[:100]))
    else:
        chapters[0] = Chapter(start_sec=0.0, title=chapters[0].title)
    return chapters


def _source_summary(sources: list[dict[str, Any]], limit: int = 10) -> list[str]:
    """出典の要約行（ローカルの絶対パスは出さない）。"""
    seen: list[str] = []
    for source in sources:
        path = source.get("path")
        if isinstance(path, str) and path not in seen:
            seen.append(path)
    return seen[:limit]


def _tags_from(scenes: list[dict[str, Any]], sources: list[dict[str, Any]]) -> list[str]:
    """章タイトルと出典ディレクトリからタグ候補を決定的に作る。"""
    tags: list[str] = []

    def add(value: str) -> None:
        cleaned = value.strip()[:MAX_TAG_CHARS]
        if cleaned and cleaned not in tags:
            tags.append(cleaned)

    for scene in scenes:
        if scene.get("kind") == "chapter" and isinstance(scene.get("title"), str):
            add(scene["title"])
    for source in sources:
        path = source.get("path")
        if isinstance(path, str) and "/" in path:
            add(path.split("/")[0])
    return tags[:MAX_TAGS]


def build_description(
    *,
    purpose: str | None,
    chapters: list[Chapter],
    sources: list[dict[str, Any]],
    sound_attributions: list[dict[str, Any]],
    public_candidate: bool,
    capture_commit_sha: str | None = None,
) -> str:
    """説明文を組む。

    **ローカルパスも API キーも入れない。** 出典は `docs/` 相対パスのみ、
    効果音は「ライセンスと帰属」だけを転記する。
    """
    blocks: list[str] = []
    if purpose:
        blocks.append(purpose.strip())
    if not public_candidate:
        blocks.append(INTERNAL_ONLY_NOTICE)

    if len(chapters) >= MIN_CHAPTERS_FOR_YOUTUBE:
        lines = ["【チャプター】"]
        lines += [f"{c.timestamp} {c.title}" for c in chapters]
        blocks.append("\n".join(lines))

    summary = _source_summary(sources)
    if summary:
        blocks.append("\n".join(["【出典】", *(f"- {path}" for path in summary)]))

    if capture_commit_sha:
        blocks.append(f"【画面キャプチャ】対象コミット: {capture_commit_sha}")

    if sound_attributions:
        # **表記が同じものは畳む。** 同じパックから7音使うと同一行が7回並び、
        # 説明文としては読めなくなる（帰属としては1回で足りる）。
        seen: list[str] = []
        for entry in sound_attributions:
            attribution = entry.get("attribution") or entry.get("sound_id")
            line = f"- {attribution}（{entry.get('license')}）"
            if line not in seen:
                seen.append(line)
        blocks.append("\n".join(["【効果音】", *seen]))

    return "\n\n".join(blocks)


def build_metadata(
    spec: dict[str, Any],
    *,
    offsets: dict[str, float],
    duration_sec: float | None,
    sound_attributions: list[dict[str, Any]] | None = None,
    distribution: dict[str, Any] | None = None,
    capture_commit_sha: str | None = None,
) -> MetadataResult:
    """`video-metadata.json` の中身を組む（**投稿はしない**）。"""
    warnings: list[str] = []
    errors: list[dict[str, str]] = []

    title = str(spec.get("title") or "無題")
    scenes = [s for s in (spec.get("scenes") or []) if isinstance(s, dict)]
    sources = [s for s in (spec.get("sources") or []) if isinstance(s, dict)]
    dist = distribution or spec.get("distribution") or {}
    public_candidate = bool(dist.get("public_candidate"))

    chapters = build_chapters(scenes, offsets, title=title)
    if len(chapters) < MIN_CHAPTERS_FOR_YOUTUBE:
        warnings.append(
            f"チャプターが {len(chapters)} 件しかありません"
            f"（手動登録で認識されるには {MIN_CHAPTERS_FOR_YOUTUBE} 件以上必要です）"
        )

    description = build_description(
        purpose=spec.get("purpose"),
        chapters=chapters,
        sources=sources,
        sound_attributions=sound_attributions or [],
        public_candidate=public_candidate,
        capture_commit_sha=capture_commit_sha,
    )

    if len(title) > MAX_TITLE_CHARS:
        errors.append(
            {
                "path": "title",
                "code": "INVALID_METADATA",
                "message": f"タイトルが {MAX_TITLE_CHARS} 文字を超えています（{len(title)} 文字）",
            }
        )
    if len(description) > MAX_DESCRIPTION_CHARS:
        errors.append(
            {
                "path": "description",
                "code": "INVALID_METADATA",
                "message": f"説明文が {MAX_DESCRIPTION_CHARS} 文字を超えています"
                f"（{len(description)} 文字）",
            }
        )

    metadata: dict[str, Any] = {
        "schema_version": "1.0",
        "video_id": spec.get("video_id"),
        "title": title,
        "description": description,
        "tags": _tags_from(scenes, sources),
        "chapters": [c.to_dict() for c in chapters],
        "language": spec.get("language") or "ja",
        "duration_sec": round(duration_sec, 2) if duration_sec else None,
        # 「人が確認してから登録する」ことを機械可読にしておく
        "suggested_visibility_note": "manual_review_required",
        "distribution_summary": {
            "classification": dist.get("classification") or "internal",
            "public_candidate": public_candidate,
        },
        "upload": {
            "method": "manual",
            "note": "YouTube Studio から手動でアップロードしてください（API 投稿は行いません）",
        },
    }
    if capture_commit_sha:
        metadata["capture"] = {"resolved_commit_sha": capture_commit_sha}

    return MetadataResult(
        ok=not errors,
        metadata=metadata,
        warnings=warnings,
        errors=errors,
        code="INVALID_METADATA" if errors else None,
    )


def write_metadata(result: MetadataResult, project_dir: Path) -> Path:
    """`video-metadata.json` を書き出す（QA FAIL でも成果物は残す）。"""
    path = project_dir / METADATA_FILE
    path.write_text(
        json.dumps(result.metadata or {}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_metadata(project_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((project_dir / METADATA_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


__all__ = [
    "INTERNAL_ONLY_NOTICE",
    "MAX_DESCRIPTION_CHARS",
    "MAX_TAGS",
    "MAX_TAG_CHARS",
    "MAX_TITLE_CHARS",
    "METADATA_FILE",
    "MIN_CHAPTERS_FOR_YOUTUBE",
    "Chapter",
    "MetadataResult",
    "build_chapters",
    "build_description",
    "build_metadata",
    "load_metadata",
    "write_metadata",
]
