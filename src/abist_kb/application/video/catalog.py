"""動画カタログ（`video_projects`）への記録とディスクからの再構築。

purring の `application/visualization/catalog.py` と同じ原則:

1. **`project-spec.json` / `manifest.json` が正本。** DB は索引
2. **DB 行を書くのはこのモジュールだけ**
3. **`spec_sha256` でドリフト検知**
4. **ディスクが真、DB は再構築可能**（`reconcile_from_disk`）
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.application.video.project_store import (
    STATE_DRAFT,
    read_state,
)
from abist_kb.infrastructure.db.video_projects_repo import VideoProjectRepository
from abist_kb.infrastructure.video.artifact_store import (
    MANIFEST_FILE,
    PROJECT_SPEC_FILE,
)

#: 自動整合をあきらめる件数（一覧参照のたびに全件走査しない）。
MAX_AUTO_RECONCILE_DIRS = 500


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _relative(path: Path | str | None, root: Path) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return candidate.as_posix()


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _inputs_from_spec(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in spec.get("sources") or []:
        origin = source.get("origin") or {}
        rows.append(
            {
                "path": source.get("path"),
                "content_hash": source.get("content_hash"),
                "selection": source.get("selection"),
                "require_usage": bool(source.get("require_usage")),
                "origin_type": origin.get("type"),
                "origin_selector": origin.get("selector"),
                "esa_url": origin.get("esa_url"),
                "esa_post_id": origin.get("esa_post_id"),
                "used": source.get("used"),
            }
        )
    return [r for r in rows if r["path"] and r["selection"] and r["origin_type"]]


def _record_from_disk(project_dir: Path, root_dir: Path, *, source: str) -> dict[str, Any] | None:
    spec_path = project_dir / PROJECT_SPEC_FILE
    spec = _read_json(spec_path)
    if not isinstance(spec, dict):
        return None
    state = read_state(project_dir) or {}
    manifest = _read_json(project_dir / MANIFEST_FILE) or {}
    outputs = manifest.get("outputs") or []
    first = outputs[0] if outputs else {}
    fmt = spec.get("format") or {}
    dist = spec.get("distribution") or {}
    return {
        "id": spec.get("video_id") or project_dir.name,
        "job_id": state.get("job_id"),
        "state": state.get("state") or STATE_DRAFT,
        "code": state.get("code"),
        "schema_version": spec.get("schema_version") or "1.0",
        "title": spec.get("title") or "",
        "purpose": spec.get("purpose"),
        "language": spec.get("language"),
        "aspect_ratio": fmt.get("aspect_ratio"),
        "classification": dist.get("classification"),
        "public_candidate": bool(dist.get("public_candidate")),
        "project_dir": _relative(project_dir, root_dir),
        "spec_path": _relative(spec_path, root_dir),
        "spec_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        "output_path": first.get("path"),
        "output_sha256": first.get("sha256"),
        "output_size_bytes": first.get("size_bytes"),
        "duration_sec": manifest.get("duration_sec"),
        "scene_count": len(spec.get("scenes") or []),
        "warnings": list(state.get("warnings") or []),
        "source": source,
        "created_at": state.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
    }


def record_project(
    conn: sqlite3.Connection,
    project_dir: Path,
    *,
    root_dir: Path,
    job_id: str | None = None,
) -> None:
    """ディスク上のプロジェクトをカタログへ記録する（作成・更新の両方で呼ぶ）。"""
    record = _record_from_disk(project_dir, root_dir, source="render")
    if record is None:
        return
    if job_id is not None:
        record["job_id"] = job_id
    spec = _read_json(project_dir / PROJECT_SPEC_FILE) or {}
    VideoProjectRepository(conn).upsert(record, _inputs_from_spec(spec))


def reconcile_from_disk(
    conn: sqlite3.Connection, videos_root: Path, *, root_dir: Path
) -> dict[str, int]:
    """`reports/videos/*/project-spec.json` から DB を再構築する（冪等）。

    ディスクに無い行は削除する（プロジェクトを手で消したら行も消える）。
    """
    repo = VideoProjectRepository(conn)
    stats = {"scanned": 0, "upserted": 0, "orphaned": 0, "skipped": 0}
    if not videos_root.exists():
        for stale in repo.all_ids():
            repo.delete(stale)
            stats["orphaned"] += 1
        return stats

    specs = sorted(videos_root.glob(f"*/{PROJECT_SPEC_FILE}"))
    if len(specs) > MAX_AUTO_RECONCILE_DIRS:
        stats["skipped"] = len(specs)
        return stats

    seen: set[str] = set()
    for spec_path in specs:
        stats["scanned"] += 1
        record = _record_from_disk(spec_path.parent, root_dir, source="disk")
        if record is None:
            continue
        spec = _read_json(spec_path) or {}
        repo.upsert(record, _inputs_from_spec(spec))
        seen.add(record["id"])
        stats["upserted"] += 1

    for stale in repo.all_ids() - seen:
        repo.delete(stale)
        stats["orphaned"] += 1
    return stats


def disk_ids(videos_root: Path) -> set[str]:
    if not videos_root.exists():
        return set()
    return {p.parent.name for p in videos_root.glob(f"*/{PROJECT_SPEC_FILE}")}


def spec_drifted(record: dict[str, Any], root_dir: Path) -> tuple[bool, dict | None]:
    """`project-spec.json` がディスク上で変化していないかを確認する。"""
    rel = record.get("spec_path")
    if not rel:
        return False, None
    path = root_dir / rel
    if not path.exists():
        return True, None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = _read_json(path)
    if spec is None:
        return True, None
    return digest != record.get("spec_sha256"), spec


__all__ = [
    "MAX_AUTO_RECONCILE_DIRS",
    "disk_ids",
    "reconcile_from_disk",
    "record_project",
    "spec_drifted",
]
