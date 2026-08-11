"""動画プロジェクトのディスク保存・読み出し・状態管理。

**`project-spec.json` が正本。** DB（`video_projects`）は索引であり、
`catalog.reconcile_from_disk()` でここから完全に再構築できる
（purring の `visualizations` と同じ二重管理回避の原則）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.application.video.input_resolver import ResolveResult
from abist_kb.domain.video_project_spec import (
    SELECTION_EXPLICIT_PRIMARY,
    VideoSpecError,
    default_inputs,
    validate_video_project_spec,
)
from abist_kb.infrastructure.video.artifact_store import (
    CITATIONS_FILE,
    INPUTS_MANIFEST_FILE,
    PROJECT_SPEC_FILE,
    STATE_FILE,
    CreatedVideoDir,
    create_video_dir,
    videos_dir,
)

#: 進行状態。再開（Phase 9）と一覧表示の両方で使う。
STATE_DRAFT = "draft"
STATE_RENDERING = "rendering"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATES: tuple[str, ...] = (
    STATE_DRAFT,
    STATE_RENDERING,
    STATE_SUCCEEDED,
    STATE_FAILED,
    STATE_CANCELLED,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@dataclass(frozen=True, slots=True)
class CreatedProject:
    video_id: str
    dir: Path
    spec: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CreateResult:
    ok: bool
    project: CreatedProject | None = None
    code: str | None = None
    errors: list[dict[str, str]] | None = None
    warnings: list[dict[str, str]] | None = None


def _sources_from_resolved(resolved: ResolveResult) -> list[dict[str, Any]]:
    """`ResolvedInput` を spec の `sources[]` 形式へ写す。"""
    return [
        {
            "id": f"src{index + 1}",
            "path": item.path,
            "content_hash": item.content_hash,
            "selection": item.selection,
            "require_usage": item.require_usage,
            "origin": item.origin,
            "sensitivity": "internal",
        }
        for index, item in enumerate(resolved.inputs)
    ]


def _inputs_for_record(requested: Any, resolved: ResolveResult) -> dict[str, Any]:
    """`project-spec.json` に残す `inputs`。

    利用者が渡した指定（ディレクトリ・検索条件を含む「何を頼んだか」）を残す。
    省略された場合は解決結果から復元し、**spec が常に自己記述的**になるようにする
    （後から「この動画は何を題材にしたのか」を spec 単体で追えるようにするため）。
    """
    if isinstance(requested, dict) and any(
        requested.get(k) for k in ("kb_paths", "kb_directories", "kb_queries", "esa_posts")
    ):
        return requested

    restored = default_inputs()
    for item in resolved.inputs:
        origin = item.origin or {}
        kind = origin.get("type")
        selector = origin.get("selector")
        if kind == "kb_path":
            restored["kb_paths"].append(item.path)
        elif kind == "kb_directory" and selector and selector not in restored["kb_directories"]:
            restored["kb_directories"].append(selector)
        elif kind == "kb_query" and selector and selector not in restored["kb_queries"]:
            restored["kb_queries"].append(selector)
        elif kind == "esa_post":
            entry: dict[str, Any] = {}
            if origin.get("esa_url"):
                entry["url"] = origin["esa_url"]
            if origin.get("esa_post_id") is not None:
                entry["post_id"] = origin["esa_post_id"]
            if entry:
                restored["esa_posts"].append(entry)
    return restored


def create_project(
    spec: dict[str, Any],
    resolved: ResolveResult,
    *,
    reports_dir: Path,
    slug: str | None = None,
) -> CreateResult:
    """解決済み入力から動画プロジェクトを作成し、ディスクへ保存する。

    `sources` は `ResolvedInput` から組み立てて上書きする（利用者が手で書いた
    `sources` は信用しない。出典は必ず解決経路から来る）。
    """
    if not resolved.ok:
        return CreateResult(
            ok=False,
            code="NO_RESOLVABLE_INPUT",
            errors=resolved.errors,
            warnings=resolved.warnings,
        )

    candidate = {
        **spec,
        "inputs": _inputs_for_record(spec.get("inputs"), resolved),
        "sources": _sources_from_resolved(resolved),
    }
    validated = validate_video_project_spec(candidate)
    if not validated.ok:
        return CreateResult(
            ok=False,
            code="INVALID_VIDEO_SPEC",
            errors=[e.to_dict() for e in validated.errors],
            warnings=resolved.warnings,
        )
    assert validated.spec is not None

    created: CreatedVideoDir = create_video_dir(
        videos_dir(reports_dir), validated.spec["title"], slug=slug
    )
    final_spec = {**validated.spec, "video_id": created.video_id}

    _write_json(created.dir / PROJECT_SPEC_FILE, final_spec)
    _write_json(created.dir / INPUTS_MANIFEST_FILE, resolved.to_manifest())
    _write_json(created.dir / CITATIONS_FILE, {"sources": final_spec["sources"]})
    write_state(
        created.dir,
        state=STATE_DRAFT,
        warnings=[w["code"] for w in resolved.warnings],
    )
    return CreateResult(
        ok=True,
        project=CreatedProject(video_id=created.video_id, dir=created.dir, spec=final_spec),
        warnings=resolved.warnings,
    )


def load_project(project_dir: Path) -> dict[str, Any] | None:
    return _read_json(project_dir / PROJECT_SPEC_FILE)


def save_project(project_dir: Path, spec: dict[str, Any]) -> None:
    _write_json(project_dir / PROJECT_SPEC_FILE, spec)


def load_inputs_manifest(project_dir: Path) -> dict[str, Any] | None:
    return _read_json(project_dir / INPUTS_MANIFEST_FILE)


def write_state(
    project_dir: Path,
    *,
    state: str,
    code: str | None = None,
    warnings: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """進行状態を書く（再開と一覧の情報源）。"""
    if state not in STATES:
        raise ValueError(f"未知の state: {state}")
    previous = read_state(project_dir) or {}
    payload: dict[str, Any] = {
        **previous,
        "state": state,
        "code": code,
        "warnings": warnings if warnings is not None else previous.get("warnings", []),
        "updated_at": _now_iso(),
    }
    payload.setdefault("created_at", payload["updated_at"])
    if extra:
        payload.update(extra)
    _write_json(project_dir / STATE_FILE, payload)
    return payload


def read_state(project_dir: Path) -> dict[str, Any] | None:
    return _read_json(project_dir / STATE_FILE)


def primary_paths(spec: dict[str, Any]) -> list[str]:
    """個別の使用が必須な path（`kb_paths` / 解決済み `esa_posts` 由来）。"""
    return [
        s["path"]
        for s in spec.get("sources") or []
        if s.get("require_usage") is True and s.get("selection") == SELECTION_EXPLICIT_PRIMARY
    ]


def collection_selectors(spec: dict[str, Any]) -> list[str]:
    """1件以上の採用が必要なディレクトリ（`kb_directories` 由来）。"""
    selectors: list[str] = []
    for source in spec.get("sources") or []:
        origin = source.get("origin") or {}
        if origin.get("type") != "kb_directory":
            continue
        selector = origin.get("selector")
        if isinstance(selector, str) and selector not in selectors:
            selectors.append(selector)
    return selectors


def check_input_usage(spec: dict[str, Any], used_paths: set[str]) -> list[VideoSpecError]:
    """主入力の使用を検証する（`PRIMARY_INPUT_UNUSED` / `PRIMARY_COLLECTION_UNUSED`）。

    **ディレクトリは全件ではなく「1件以上」で判定する。** `docs/` には約 85,000 件の
    Markdown があり、ディレクトリ配下の全件使用は原理的に成立しない。
    どちらも warning 相当（中断しない）で、呼び出し側が qa-report へ回す。
    """
    errors: list[VideoSpecError] = []

    unused = [p for p in primary_paths(spec) if p not in used_paths]
    if unused:
        errors.append(
            VideoSpecError(
                "inputs.kb_paths",
                "PRIMARY_INPUT_UNUSED",
                "明示指定した主入力が本編で使われていません: " + " / ".join(sorted(unused)),
            )
        )

    by_selector: dict[str, list[str]] = {}
    for source in spec.get("sources") or []:
        origin = source.get("origin") or {}
        if origin.get("type") == "kb_directory" and isinstance(origin.get("selector"), str):
            by_selector.setdefault(origin["selector"], []).append(source["path"])
    unused_selectors = [
        selector
        for selector, paths in by_selector.items()
        if not any(p in used_paths for p in paths)
    ]
    if unused_selectors:
        errors.append(
            VideoSpecError(
                "inputs.kb_directories",
                "PRIMARY_COLLECTION_UNUSED",
                "指定ディレクトリから1件も採用されていません: "
                + " / ".join(sorted(unused_selectors)),
            )
        )
    return errors


__all__ = [
    "STATES",
    "STATE_CANCELLED",
    "STATE_DRAFT",
    "STATE_FAILED",
    "STATE_RENDERING",
    "STATE_SUCCEEDED",
    "CreateResult",
    "CreatedProject",
    "check_input_usage",
    "collection_selectors",
    "create_project",
    "load_inputs_manifest",
    "load_project",
    "primary_paths",
    "read_state",
    "save_project",
    "write_state",
]
