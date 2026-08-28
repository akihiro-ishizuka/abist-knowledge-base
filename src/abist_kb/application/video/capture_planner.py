"""キャプチャの解決と実行（プロファイルレジストリ + git + 撮影）。

**MCP / API / CLI が渡せるのはプロファイル名だけ。** ここでレジストリを引き、
運用者が登録した内容だけを実行する。レジストリの実体は
`config/capture-profiles.json`（リポジトリに同梱しない運用ファイル）。

キャプチャは既定で無効。無効・未登録・失敗のいずれでも
`CapturePlan(ok=False, ...)` を返すだけで、**動画生成は止めない**。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.domain.capture_spec import (
    CaptureProfile,
    is_capture_enabled,
    validate_capture_profile,
)
from abist_kb.infrastructure.video import git_workspace, screen_capture

#: レジストリの既定位置（リポジトリルートからの相対）。
PROFILES_FILE = Path("config") / "capture-profiles.json"
#: プロジェクト内でのキャプチャ成果物の置き場。
CAPTURES_DIR = "captures"
CAPTURES_MANIFEST = "manifest.json"
#: clone 先（成果物ツリー内。外へは出さない）。
WORKSPACE_DIR = "workspace"


@dataclass(frozen=True, slots=True)
class CapturePlan:
    ok: bool
    profile: CaptureProfile | None = None
    code: str | None = None
    message: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    ok: bool
    manifest_path: Path | None = None
    resolved_commit_sha: str | None = None
    #: `scene_id -> captures/ からの相対パス`。image beat の path になる。
    images_by_scene: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    code: str | None = None


def load_registry(repo_root: Path, *, profiles_file: Path | None = None) -> dict[str, Any]:
    """プロファイルレジストリを読む（無ければ空）。"""
    path = profiles_file or (repo_root / PROFILES_FILE)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    profiles = payload.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def list_profiles(repo_root: Path, *, profiles_file: Path | None = None) -> list[str]:
    """登録済みプロファイル名の一覧（**コマンドは返さない**）。"""
    return sorted(load_registry(repo_root, profiles_file=profiles_file))


def resolve_profile(
    name: Any,
    *,
    repo_root: Path,
    env: dict[str, str] | None = None,
    profiles_file: Path | None = None,
) -> CapturePlan:
    """プロファイル名から実行可能な `CaptureProfile` を得る。

    ここが**唯一の入口**。生の `command` / `url` / `repo` を受け取る経路は無い。
    """
    environ = env if env is not None else dict(os.environ)
    if not is_capture_enabled(environ):
        return CapturePlan(
            ok=False,
            code="CAPTURE_DISABLED",
            message="画面キャプチャは既定で無効です。"
            "ABIST_KB_VIDEO_CAPTURE_ENABLED=1 を設定すると有効になります",
        )
    if not isinstance(name, str) or not name:
        return CapturePlan(
            ok=False,
            code="CAPTURE_PROFILE_NOT_FOUND",
            message="capture_profile にはプロファイル名を指定してください",
        )

    registry = load_registry(repo_root, profiles_file=profiles_file)
    if name not in registry:
        known = ", ".join(sorted(registry)) or "（登録なし）"
        return CapturePlan(
            ok=False,
            code="CAPTURE_PROFILE_NOT_FOUND",
            message=f'capture_profile "{name}" は登録されていません。登録済み: {known}',
        )

    validated = validate_capture_profile(name, registry[name])
    if not validated.ok or validated.profile is None:
        return CapturePlan(
            ok=False,
            code="INVALID_CAPTURE_PROFILE",
            message=f'capture_profile "{name}" の登録内容が不正です',
            errors=[e.to_dict() for e in validated.errors],
        )
    return CapturePlan(ok=True, profile=validated.profile)


def run_capture(plan: CapturePlan, project_dir: Path) -> CaptureOutcome:
    """git 取得 → 起動 → 撮影 → マスク → manifest 書き出し。

    **どの失敗でも動画生成は止めない。** `ok=False` と warnings を返し、
    呼び出し側は該当シーンをプレースホルダで描く。
    """
    if not plan.ok or plan.profile is None:
        return CaptureOutcome(
            ok=False, code=plan.code, warnings=[plan.message] if plan.message else []
        )

    profile = plan.profile
    workspace = project_dir / WORKSPACE_DIR
    prepared = git_workspace.prepare(profile.repo_url, profile.ref, workspace)
    if not prepared.ok:
        return CaptureOutcome(
            ok=False,
            code=prepared.code,
            warnings=[f"キャプチャを省略しました: {prepared.message}"],
        )

    out_dir = project_dir / CAPTURES_DIR
    run_result = screen_capture.run(profile, workspace=workspace, out_dir=out_dir)
    warnings = list(run_result.warnings)
    if run_result.code:
        warnings.append(f"キャプチャを省略しました: {run_result.message}")

    shots_payload: list[dict[str, Any]] = []
    images_by_scene: dict[str, str] = {}
    for shot in run_result.shots:
        entry: dict[str, Any] = {
            "id": shot.id,
            "scene_id": shot.scene_id,
            "ok": shot.ok,
            "masked_regions": shot.masked_regions,
        }
        if shot.ok and shot.path is not None:
            entry["path"] = f"{CAPTURES_DIR}/{shot.path.name}"
            entry["sha256"] = shot.sha256
            if shot.scene_id:
                images_by_scene[shot.scene_id] = f"{CAPTURES_DIR}/{shot.path.name}"
        else:
            entry["code"] = shot.code
            entry["message"] = shot.message
        shots_payload.append(entry)

    manifest = {
        "schema_version": "1.0",
        "profile": profile.name,
        "repo_url": profile.repo_url,
        "requested_ref": profile.ref,
        # 「どの時点のアプリ画面か」の唯一の証跡。citations と説明文へも転記する。
        "resolved_commit_sha": prepared.resolved_commit_sha,
        "captured_at": datetime.now(UTC).isoformat(),
        "shots": shots_payload,
        "warnings": warnings,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / CAPTURES_MANIFEST
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return CaptureOutcome(
        ok=run_result.ok,
        manifest_path=manifest_path,
        resolved_commit_sha=prepared.resolved_commit_sha,
        images_by_scene=images_by_scene,
        warnings=warnings,
        code=run_result.code,
    )


def read_manifest(project_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads(
            (project_dir / CAPTURES_DIR / CAPTURES_MANIFEST).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None


__all__ = [
    "CAPTURES_DIR",
    "CAPTURES_MANIFEST",
    "PROFILES_FILE",
    "WORKSPACE_DIR",
    "CaptureOutcome",
    "CapturePlan",
    "list_profiles",
    "load_registry",
    "read_manifest",
    "resolve_profile",
    "run_capture",
]
