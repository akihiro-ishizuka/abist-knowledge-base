"""可視化カタログ（`visualizations` テーブル）への記録とディスクからの再構築。

設計原則（実装を変えるときはこれを守ること）:

1. **`manifest.json` が全文の正本。** DB は索引であり、`spec.beats` /
   `render.stdout_tail` / `generator` の verbatim は持たない
2. **DB 行を書くのはこのモジュールだけ。** `renderer` は
   `RenderOutcome` を返すだけで DB に触れない（renderer.py の docstring 参照）
3. **`manifest_sha256` でドリフト検知。** 読み出し時にディスクを読み直し、
   一致しなければ `manifest_drift: true` を返す
4. **ディスクが真、DB は再構築可能。** `reconcile_from_disk()` が
   `reports/visualizations/*/manifest.json` から完全に再生成できる
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.infrastructure.db.visualizations_repo import VisualizationRepository

#: `reconcile_from_disk` が自動整合をあきらめる成果物数。
#: これを超えたら明示的な整合コマンドに委ねる（一覧参照のたびに全件走査しない）。
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


def normalize_created_at(raw: Any) -> str:
    """manifest の `created_at` を ISO 文字列へ揃える。

    旧 Node 実装は `"...Z"`、現行 Python 実装は `datetime.isoformat()` の
    `"+00:00"` を書く。読み側で明示的に吸収して意図を可視化する
    （Python 3.12 の `fromisoformat` は `Z` も受けるが、暗黙に頼らない）。
    """
    if not isinstance(raw, str) or not raw:
        return _now_iso()
    return raw.replace("Z", "+00:00")


def _sources_from(spec_sources: list[dict[str, Any]], status: dict[str, str]) -> list[dict]:
    return [
        {
            "source_id": src.get("id"),
            "path": src.get("path"),
            "start_line": src.get("start_line"),
            "end_line": src.get("end_line"),
            "content_hash": src.get("content_hash"),
            # 取り込み(disk)では当時の判定が残っていないので unknown になる。
            "status": status.get(str(src.get("id")), "unknown"),
        }
        for src in spec_sources
        if src.get("id")
    ]


def record_render(
    conn: sqlite3.Connection,
    outcome: Any,
    *,
    root_dir: Path,
    job_id: str | None = None,
) -> None:
    """レンダリング結果をカタログへ記録する。

    `outcome.visualization_id is None`（= 出力ディレクトリを作る前に失敗した
    INVALID_SCENE_SPEC / SOURCE_* / PYTHON_NOT_FOUND）は「可視化」として成立して
    いないため記録しない。これは manifest.json が存在するかどうかと一致する。
    """
    if getattr(outcome, "visualization_id", None) is None:
        return
    manifest = outcome.manifest or {}
    spec = manifest.get("spec") or {}
    outputs = manifest.get("outputs") or []
    first = outputs[0] if outputs else {}
    now = _now_iso()
    record = {
        "id": outcome.visualization_id,
        "job_id": job_id,
        "state": "succeeded" if outcome.ok else "failed",
        "code": outcome.code,
        "schema_version": spec.get("schema_version") or manifest.get("schema_version") or "1.0",
        "scene_kind": spec.get("scene_kind") or manifest.get("scene_kind") or "",
        "template": spec.get("template") or manifest.get("template") or "",
        "output_format": spec.get("output_format") or manifest.get("output_format") or "",
        "title": spec.get("title") or "",
        "query": spec.get("query") or manifest.get("query"),
        "output_dir": _relative(outcome.output_dir, root_dir),
        "manifest_path": _relative(outcome.manifest_path, root_dir),
        "manifest_sha256": outcome.manifest_sha256,
        "output_path": first.get("path"),
        "output_sha256": first.get("sha256"),
        "output_size_bytes": first.get("size_bytes"),
        "duration_ms": outcome.duration_ms,
        "warnings": list(outcome.warnings or []),
        "source": "render",
        "created_at": normalize_created_at(manifest.get("created_at")),
        "updated_at": now,
    }
    VisualizationRepository(conn).upsert(
        record, _sources_from(outcome.sources or [], outcome.source_status or {})
    )


def _record_from_manifest(
    manifest: dict[str, Any], manifest_path: Path, root_dir: Path
) -> dict[str, Any]:
    spec = manifest.get("spec") or {}
    outputs = manifest.get("outputs") or []
    first = outputs[0] if outputs else {}
    out_dir = manifest_path.parent
    render = manifest.get("render") or {}
    # manifest には state / code が無い。exit_code と出力ファイルの実在から導く。
    produced = out_dir / first["path"] if first.get("path") else None
    succeeded = render.get("exit_code") == 0 and produced is not None and produced.exists()
    now = _now_iso()
    return {
        "id": manifest.get("visualization_id") or out_dir.name,
        "job_id": None,
        "state": "succeeded" if succeeded else "failed",
        "code": None,
        "schema_version": spec.get("schema_version") or manifest.get("schema_version") or "1.0",
        "scene_kind": spec.get("scene_kind") or manifest.get("scene_kind") or "",
        "template": spec.get("template") or manifest.get("template") or "",
        "output_format": spec.get("output_format") or manifest.get("output_format") or "",
        "title": spec.get("title") or "",
        "query": spec.get("query") or manifest.get("query"),
        "output_dir": _relative(out_dir, root_dir),
        "manifest_path": _relative(manifest_path, root_dir),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "output_path": first.get("path"),
        "output_sha256": first.get("sha256"),
        "output_size_bytes": first.get("size_bytes"),
        "duration_ms": render.get("duration_ms"),
        "warnings": list(manifest.get("warnings") or []),
        "source": "disk",
        "created_at": normalize_created_at(manifest.get("created_at")),
        "updated_at": now,
    }


def reconcile_from_disk(
    conn: sqlite3.Connection, visualizations_dir: Path, *, root_dir: Path
) -> dict[str, int]:
    """`reports/visualizations/*/manifest.json` から DB を再構築する（冪等）。

    戻り値: `{"scanned", "upserted", "orphaned", "skipped"}`。
    ディスクに無い行は削除する（成果物ディレクトリを手で消したら行も消える）。
    """
    repo = VisualizationRepository(conn)
    stats = {"scanned": 0, "upserted": 0, "orphaned": 0, "skipped": 0}
    if not visualizations_dir.exists():
        for stale in repo.all_ids():
            repo.delete(stale)
            stats["orphaned"] += 1
        return stats

    manifests = sorted(visualizations_dir.glob("*/manifest.json"))
    if len(manifests) > MAX_AUTO_RECONCILE_DIRS:
        stats["skipped"] = len(manifests)
        return stats

    seen: set[str] = set()
    for manifest_path in manifests:
        stats["scanned"] += 1
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        record = _record_from_manifest(manifest, manifest_path, root_dir)
        # 取り込み時に出典を再検証しない。記録すべきは「レンダリング当時の判定」で
        # あって、現在の docs/ に対する判定ではない。
        sources = _sources_from((manifest.get("sources") or []), {})
        repo.upsert(record, sources)
        seen.add(record["id"])
        stats["upserted"] += 1

    for stale in repo.all_ids() - seen:
        repo.delete(stale)
        stats["orphaned"] += 1
    return stats


def disk_ids(visualizations_dir: Path) -> set[str]:
    """ディスク上の成果物ディレクトリ id 集合（manifest.json を持つものだけ）。"""
    if not visualizations_dir.exists():
        return set()
    return {p.parent.name for p in visualizations_dir.glob("*/manifest.json")}


def manifest_drifted(record: dict[str, Any], root_dir: Path) -> tuple[bool, dict | None]:
    """manifest がディスク上で変化していないかを確認し、内容も返す。"""
    rel = record.get("manifest_path")
    if not rel:
        return False, None
    path = root_dir / rel
    if not path.exists():
        return True, None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, None
    return digest != record.get("manifest_sha256"), manifest


__all__ = [
    "MAX_AUTO_RECONCILE_DIRS",
    "disk_ids",
    "manifest_drifted",
    "normalize_created_at",
    "reconcile_from_disk",
    "record_render",
]
