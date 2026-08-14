"""動画ツール（MCP）。`all` サーバーへ**純増分**として追加する。

責務の切り分け:

- 長時間の描画は**非同期ジョブ**（`start_render_video`）。同期でやらせない
- 汎用のジョブ照会は既存 `job_status`。`video_status` はプロジェクト視点の薄い付加情報
- **`upload_youtube` は作らない**（将来バックログ）

**画面キャプチャの起動コマンドは受け取らない。** `capture_profile`（登録済みの
プロファイル名）だけを受け付ける。生の `command` / `url` / `repo` を渡す口は
どのツールにも無い。

fixture は1バイトも触らない。追加分は
`tests/mcp/test_all_server_tools_list_diff.py` の `_NEW_VIDEO_TOOL_NAMES` に
純増分として宣言する（`test_contract_visualize.py` と同じ方式）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import jsonschema
import mcp.types as types

from abist_kb.application.video import catalog
from abist_kb.application.video.approval import approve as run_approve
from abist_kb.application.video.approval import verify_approval
from abist_kb.application.video.capture_planner import list_profiles
from abist_kb.application.video.composition import score_composition
from abist_kb.application.video.contact_sheet import CONTACT_SHEET_FILE
from abist_kb.application.video.content_quality import write_storyboard_review
from abist_kb.application.video.distribution import (
    evaluate as evaluate_distribution,
)
from abist_kb.application.video.distribution import (
    load_report as load_distribution_report,
)
from abist_kb.application.video.distribution import (
    request_public_review,
)
from abist_kb.application.video.distribution import (
    write_report as write_distribution_report,
)
from abist_kb.application.video.image_assets import (
    attach_image_assets,
    ingest_image_assets,
    validate_image_asset_refs,
)
from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.application.video.pipeline import _format_for
from abist_kb.application.video.project_store import (
    STATE_DRAFT,
    create_project,
    load_project,
    read_state,
    save_project,
    write_state,
)
from abist_kb.application.video.qa import load_report as load_qa_report
from abist_kb.application.video.qa import run_qa, write_report
from abist_kb.application.video.resume import scene_content_digest
from abist_kb.application.video.script_planner import author_script, fix_hint_for
from abist_kb.application.video.video_metadata import load_metadata
from abist_kb.domain.video_project_spec import (
    DEFAULT_MAX_DOCS_PER_DIRECTORY,
    DEFAULT_MAX_TOTAL_CANDIDATES,
    DEFAULT_VIDEO_QUALITY,
    VIDEO_QUALITIES,
    validate_video_project_spec,
)
from abist_kb.infrastructure.db.video_projects_repo import VideoProjectRepository
from abist_kb.infrastructure.video.artifact_store import videos_dir
from abist_kb.presentation.mcp.payloads import ok_result, tool_result

#: 同期 `render_video` を許すシーン数の上限。これを超えたら非同期へ誘導する。
MAX_SYNC_SCENES = 2

TOOL_DESCRIPTIONS: dict[str, str] = {
    "validate_video_script": (
        "自分で書いた台本を、保存もレンダリングもせずに検証する。"
        "エラーは {sceneId, path, code, message, fixHint} で返るので、"
        "直してから create_video_project / update_video_script を呼ぶこと。"
        "シーンごとの推定尺（テロップの読速から算出）も返す。"
    ),
    "create_video_project": (
        "検証済みの動画プロジェクトを reports/videos/<id> に作る。script は必須"
        "（台本はエージェントが書く。機械生成する経路は無い）。"
        "kb_paths は必ず本編で使う主入力、kb_directories は選抜対象の候補集合、"
        "kb_queries は補完。描画はしない（start_render_video を使う）。"
    ),
    "update_video_script": (
        "既存プロジェクトの台本を差し替える（全置換）。変わったシーンの id を返し、"
        "state を draft へ戻す。題材（inputs）は変えられない（変えたいなら新しい"
        "プロジェクトを作ること）。"
    ),
    "get_video_preview": (
        "レンダリング前後の確認材料を返す：絵コンテ（全表示文）・コンタクトシート・"
        "シーン別成果物・QA 要約。人間が観る前の自己点検に使う。"
    ),
    "start_render_video": (
        "動画レンダリングを非同期ジョブとして投入する。進捗は job_status、"
        "完成物は get_video で参照する。長時間かかるため同期実行はしない。"
    ),
    "video_status": (
        "動画プロジェクトの進行状態（state.json）と QA の要約を返す。"
        "ジョブ自体の状態は job_status を使うこと。"
    ),
    "get_video": (
        "動画プロジェクトのメタデータ・成果物パス・QA・配布判定を返す。"
        "動画そのものは外部へ送信しない（手動アップロード用のパス一覧を返すだけ）。"
    ),
    "list_videos": "生成済み動画プロジェクトの一覧をカタログから返す。",
    "run_video_qa": (
        "qa-report.json を再生成する。FAIL でも成果物は消さない（承認だけがブロックされる）。"
    ),
    "approve_video": (
        "社内プレビュー承認を記録する。成果物・メタデータ・QA・配布判定の SHA-256 に"
        "バインドするので、後から再生成すると自動的に無効になる。外部送信は行わない。"
    ),
    "set_distribution": (
        "情報区分と公開候補を更新する。機密ソースや秘密スキャンのヒットがある場合、"
        "public_candidate は強制的に false へ降格される。"
    ),
    "request_public_review": (
        "公開審査への提出状態へ進める（ステータス記録のみ。外部へは何も送らない）。"
        "public_candidate が機械判定で true のときだけ受け付ける。"
    ),
    "list_capture_profiles": (
        "登録済みの画面キャプチャプロファイル名を返す。起動コマンドは返さないし、"
        "MCP から新しいコマンドを登録することもできない（運用者が設定ファイルで管理する）。"
    ),
}

_INPUTS_SCHEMA = {
    "type": "object",
    "properties": {
        "kb_paths": {"type": "array", "items": {"type": "string"}},
        "kb_directories": {"type": "array", "items": {"type": "string"}},
        "kb_queries": {"type": "array"},
        "esa_posts": {"type": "array"},
    },
    "additionalProperties": False,
}

_TARGET_DURATION_SCHEMA = {
    "type": "object",
    "properties": {"min": {"type": "number"}, "max": {"type": "number"}},
    "required": ["min", "max"],
    "additionalProperties": False,
}

#: 作者が書く台本（ScriptDraft）。中身の検証は `validate_script_draft` が行うので、
#: ここでは「scenes を持つオブジェクト」までしか縛らない（スキーマを二重管理しない）。
_SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "scenes": {"type": "array"},
        "sound_events": {"type": "array"},
    },
    "required": ["scenes"],
}

#: 作者が自己申告する構成要件。宣言したものだけが検査される。
_STORY_REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "required_topics": {"type": "array", "items": {"type": "string"}},
        "required_scene_kinds": {"type": "array", "items": {"type": "string"}},
        "scene_count": {
            "type": "object",
            "properties": {"min": {"type": "integer"}, "max": {"type": "integer"}},
            "additionalProperties": False,
        },
        "source_path_pattern": {"type": "string"},
        "sound_events": {
            "type": "object",
            "properties": {"min": {"type": "integer"}, "max": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

#: 台本作成時に用意した画像。`path` はローカルの原本（リポジトリ外でもよい）。
_IMAGE_ASSETS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "path": {"type": "string"},
            "license": {"type": "string"},
            "attribution": {"type": "string"},
            "caption": {"type": "string"},
        },
        "required": ["id", "path", "license"],
        "additionalProperties": False,
    },
}


def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="validate_video_script",
            description=TOOL_DESCRIPTIONS["validate_video_script"],
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "purpose": {"type": "string"},
                    "inputs": _INPUTS_SCHEMA,
                    "script": _SCRIPT_SCHEMA,
                    "story_requirements": _STORY_REQUIREMENTS_SCHEMA,
                    "image_assets": _IMAGE_ASSETS_SCHEMA,
                    "target_duration_sec": _TARGET_DURATION_SCHEMA,
                },
                "required": ["title", "inputs", "script"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="create_video_project",
            description=TOOL_DESCRIPTIONS["create_video_project"],
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "purpose": {"type": "string"},
                    "inputs": _INPUTS_SCHEMA,
                    "script": _SCRIPT_SCHEMA,
                    "story_requirements": _STORY_REQUIREMENTS_SCHEMA,
                    "image_assets": _IMAGE_ASSETS_SCHEMA,
                    "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16"]},
                    "quality": {"type": "string", "enum": list(VIDEO_QUALITIES)},
                    "target_duration_sec": _TARGET_DURATION_SCHEMA,
                },
                "required": ["title", "inputs", "script"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="update_video_script",
            description=TOOL_DESCRIPTIONS["update_video_script"],
            inputSchema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "script": _SCRIPT_SCHEMA,
                    "story_requirements": _STORY_REQUIREMENTS_SCHEMA,
                    "image_assets": _IMAGE_ASSETS_SCHEMA,
                },
                "required": ["video_id", "script"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_video_preview",
            description=TOOL_DESCRIPTIONS["get_video_preview"],
            inputSchema={
                "type": "object",
                "properties": {"video_id": {"type": "string"}},
                "required": ["video_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="start_render_video",
            description=TOOL_DESCRIPTIONS["start_render_video"],
            inputSchema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "sound_intensity": {
                        "type": "string",
                        "enum": ["off", "subtle", "normal"],
                    },
                    # **登録済みプロファイル名のみ。** 起動コマンドは受け取らない。
                    "capture_profile": {"type": "string"},
                },
                "required": ["video_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="video_status",
            description=TOOL_DESCRIPTIONS["video_status"],
            inputSchema={
                "type": "object",
                "properties": {"video_id": {"type": "string"}},
                "required": ["video_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_video",
            description=TOOL_DESCRIPTIONS["get_video"],
            inputSchema={
                "type": "object",
                "properties": {"video_id": {"type": "string"}},
                "required": ["video_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="list_videos",
            description=TOOL_DESCRIPTIONS["list_videos"],
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "offset": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="run_video_qa",
            description=TOOL_DESCRIPTIONS["run_video_qa"],
            inputSchema={
                "type": "object",
                "properties": {"video_id": {"type": "string"}},
                "required": ["video_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="approve_video",
            description=TOOL_DESCRIPTIONS["approve_video"],
            inputSchema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "approver": {"type": "string"},
                    "confirmed": {"type": "boolean"},
                },
                "required": ["video_id", "approver", "confirmed"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="set_distribution",
            description=TOOL_DESCRIPTIONS["set_distribution"],
            inputSchema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "classification": {
                        "type": "string",
                        "enum": ["internal", "confidential", "public_candidate_pending"],
                    },
                    "audience": {"type": "array", "items": {"type": "string"}},
                    "allowed_groups": {"type": "array", "items": {"type": "string"}},
                    "public_candidate": {"type": "boolean"},
                },
                "required": ["video_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="request_public_review",
            description=TOOL_DESCRIPTIONS["request_public_review"],
            inputSchema={
                "type": "object",
                "properties": {"video_id": {"type": "string"}},
                "required": ["video_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="list_capture_profiles",
            description=TOOL_DESCRIPTIONS["list_capture_profiles"],
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
    ]


_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {tool.name: tool.inputSchema for tool in list_tools()}


def validate_arguments(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult | None:
    """`inputSchema` に対する検証（kb-visualize と同じ規約）。"""
    schema = _INPUT_SCHEMAS.get(tool_name)
    if schema is None:
        return None
    try:
        jsonschema.validate(instance=arguments, schema=schema)
    except jsonschema.ValidationError as exc:
        text = (
            f"MCP error -32602: Input validation error: "
            f"Invalid arguments for tool {tool_name}: {exc.message}"
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], isError=True
        )
    return None


def _error(code: str, message: str, **extra: Any) -> types.CallToolResult:
    return tool_result({"ok": False, "code": code, "message": message, **extra})


class KbVideoTools:
    """動画ツールのアダプタ。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        docs_dir: Path,
        reports_dir: Path,
        repo_root: Path,
        job_tools: Any | None = None,
    ) -> None:
        self._conn = conn
        self._docs_dir = docs_dir
        self._reports_dir = reports_dir
        self._repo_root = repo_root
        self._job_tools = job_tools

    # --- 内部ヘルパー -------------------------------------------------

    def _project_dir(self, video_id: str) -> Path | None:
        """`video_id` からプロジェクトディレクトリを引く（存在しなければ None）。

        id はディレクトリ名そのもの。`..` を含む値は `videos_dir` の外を
        指しうるので、解決後に必ず封じ込めを確認する。
        """
        root = videos_dir(self._reports_dir).resolve()
        candidate = (root / video_id).resolve()
        if root not in candidate.parents or not candidate.is_dir():
            return None
        return candidate

    def _resolve(self, inputs: dict[str, Any]):
        return resolve_inputs(
            inputs,
            docs_dir=self._docs_dir,
            max_docs_per_directory=DEFAULT_MAX_DOCS_PER_DIRECTORY,
            max_total_candidates=DEFAULT_MAX_TOTAL_CANDIDATES,
        )

    # --- ツール -------------------------------------------------------

    def validate_video_script(self, arguments: dict[str, Any]) -> types.CallToolResult:
        resolved = self._resolve(arguments.get("inputs") or {})
        if not resolved.ok:
            return _error(
                "NO_RESOLVABLE_INPUT",
                "題材を解決できませんでした",
                errors=resolved.errors,
                warnings=resolved.warnings,
            )
        authored = author_script(
            arguments.get("script") or {},
            resolved.inputs,
            docs_dir=self._docs_dir,
            target_duration_sec=arguments.get("target_duration_sec"),
            story_requirements=arguments.get("story_requirements"),
            strict=True,
        )
        errors = list(authored.errors)
        if authored.ok:
            declared = {str(asset.get("id")) for asset in (arguments.get("image_assets") or [])}
            errors.extend(
                {**error, "fixHint": fix_hint_for(error["code"])}
                for error in validate_image_asset_refs(authored.scenes, registered_ids=declared)
            )
        # 構成の単調さは**描く前**に返す（描いてから気付くのでは数分無駄になる）。
        # 既定は警告どまりだが、書き手が story_requirements で約束したぶんは通さない。
        composition = score_composition(
            authored.scenes, requirements=arguments.get("story_requirements")
        )
        errors.extend(
            {
                "path": "scenes",
                "code": finding["code"],
                "message": finding["message"],
                "fixHint": finding["hint"],
            }
            for finding in composition.findings
            if finding["declared"]
        )
        if errors:
            return tool_result(
                {
                    "ok": False,
                    "code": errors[0].get("code") or "INVALID_SCRIPT_DRAFT",
                    "message": "台本を検証できませんでした",
                    "errors": errors,
                    "warnings": authored.warnings,
                    "sceneCount": len(authored.scenes),
                    "composition": composition.to_dict(),
                }
            )
        return ok_result(
            {
                "ok": True,
                "sceneCount": len(authored.scenes),
                "scenes": authored.scene_estimates,
                "estimatedDurationSec": authored.estimated_duration_sec,
                "durationPlan": authored.duration_plan,
                "composition": composition.to_dict(),
                "errors": [],
                "warnings": [
                    *authored.warnings,
                    *(w["message"] for w in resolved.warnings),
                ],
            }
        )

    def create_video_project(self, arguments: dict[str, Any]) -> types.CallToolResult:
        inputs = arguments.get("inputs") or {}
        resolved = self._resolve(inputs)
        if not resolved.ok:
            return _error(
                "NO_RESOLVABLE_INPUT",
                "題材を解決できませんでした",
                errors=resolved.errors,
                warnings=resolved.warnings,
            )
        target_duration = arguments.get("target_duration_sec")
        fmt = _format_for(
            str(arguments.get("aspect_ratio") or "16:9"),
            target_duration,
            quality=str(arguments.get("quality") or DEFAULT_VIDEO_QUALITY),
        )

        # **台本は必須。** 空のシーンでプロジェクトを作ると start_render_video が
        # NO_RENDERABLE_SCENE で必ず失敗する（作れたのに描けない箱ができる）。
        script = arguments.get("script")
        if not isinstance(script, dict):
            return _error(
                "SCRIPT_REQUIRED",
                "script を指定してください（台本はエージェントが書きます）",
                errors=[
                    {
                        "path": "script",
                        "code": "SCRIPT_REQUIRED",
                        "message": "台本を機械生成する経路はありません",
                        "fixHint": "validate_video_script で台本を通してから渡してください",
                    }
                ],
            )
        declared_images = {str(a.get("id")) for a in (arguments.get("image_assets") or [])}
        warnings: list[str] = []
        authored = author_script(
            script,
            resolved.inputs,
            docs_dir=self._docs_dir,
            target_duration_sec=target_duration,
            story_requirements=arguments.get("story_requirements"),
            strict=True,
        )
        errors = list(authored.errors)
        if authored.ok:
            errors.extend(
                {**error, "fixHint": fix_hint_for(error["code"])}
                for error in validate_image_asset_refs(
                    authored.scenes, registered_ids=declared_images
                )
            )
        if errors:
            return tool_result(
                {
                    "ok": False,
                    "code": errors[0].get("code") or "INVALID_SCRIPT_DRAFT",
                    "message": "台本を検証できませんでした",
                    "errors": errors,
                    "warnings": authored.warnings,
                }
            )
        scenes, sound_events = authored.scenes, authored.sound_events
        warnings.extend(authored.warnings)

        created = create_project(
            {
                "title": str(arguments["title"]),
                "purpose": arguments.get("purpose"),
                "inputs": inputs,
                "format": fmt,
                "scenes": scenes,
                "sound_events": sound_events,
                "story_requirements": arguments.get("story_requirements"),
            },
            resolved,
            reports_dir=self._reports_dir,
        )
        if not created.ok or created.project is None:
            return _error(
                created.code or "INVALID_VIDEO_SPEC",
                "プロジェクトを作成できませんでした",
                errors=created.errors or [],
            )
        project = created.project

        image_error = self._store_image_assets(project.dir, arguments.get("image_assets") or [])
        if image_error is not None:
            return image_error

        # レンダリング前に全表示文を残す（人が観る前の自己点検材料）。
        write_storyboard_review(project.dir, scenes, sound_events)
        catalog.record_project(self._conn, project.dir, root_dir=self._repo_root)
        return ok_result(
            {
                "ok": True,
                "videoId": project.video_id,
                "projectDir": str(project.dir),
                "sceneCount": len(scenes),
                "sourceCount": len(project.spec.get("sources") or []),
                "storyboardReview": str(project.dir / "preview" / "storyboard-review.md"),
                "warnings": [*warnings, *(w["message"] for w in (created.warnings or []))],
            }
        )

    def _store_image_assets(
        self, project_dir: Path, raw_assets: list[dict[str, Any]]
    ) -> types.CallToolResult | None:
        """持ち込み画像を取り込み、spec とシーンへ結び付ける（失敗なら結果を返す）。"""
        if not raw_assets:
            return None
        ingested = ingest_image_assets(raw_assets, project_dir)
        if not ingested.ok:
            return tool_result(
                {
                    "ok": False,
                    "code": "INVALID_IMAGE_ASSET",
                    "message": "画像を取り込めませんでした",
                    "errors": ingested.errors,
                }
            )
        spec = load_project(project_dir) or {}
        spec["image_assets"] = ingested.assets
        attach_image_assets(spec.get("scenes") or [], ingested.assets)
        save_project(project_dir, spec)
        return None

    def update_video_script(self, arguments: dict[str, Any]) -> types.CallToolResult:
        video_id = str(arguments["video_id"])
        project_dir = self._project_dir(video_id)
        if project_dir is None:
            return _error("VIDEO_NOT_FOUND", f"動画 {video_id} が見つかりません")
        spec = load_project(project_dir)
        if spec is None:
            return _error("VIDEO_NOT_FOUND", f"動画 {video_id} の spec を読めません")

        # 題材は作成時に固定。差し替えたいなら別プロジェクトにする（出典の履歴が濁る）。
        resolved = self._resolve(spec.get("inputs") or {})
        if not resolved.ok:
            return _error(
                "NO_RESOLVABLE_INPUT",
                "題材を解決できませんでした",
                errors=resolved.errors,
            )
        requirements = (
            arguments.get("story_requirements")
            if "story_requirements" in arguments
            else spec.get("story_requirements")
        )
        authored = author_script(
            arguments.get("script") or {},
            resolved.inputs,
            docs_dir=self._docs_dir,
            target_duration_sec=(spec.get("format") or {}).get("target_duration_sec"),
            story_requirements=requirements,
            strict=True,
        )
        declared_images = {
            str(a.get("id"))
            for a in (arguments.get("image_assets") or spec.get("image_assets") or [])
        }
        errors = list(authored.errors)
        if authored.ok:
            errors.extend(
                {**error, "fixHint": fix_hint_for(error["code"])}
                for error in validate_image_asset_refs(
                    authored.scenes, registered_ids=declared_images
                )
            )
        if errors:
            return tool_result(
                {
                    "ok": False,
                    "code": errors[0].get("code") or "INVALID_SCRIPT_DRAFT",
                    "message": "台本を検証できませんでした",
                    "errors": errors,
                    "warnings": authored.warnings,
                }
            )

        previous = {
            str(scene.get("id")): scene_content_digest(scene) for scene in spec.get("scenes") or []
        }
        changed = [
            scene["id"]
            for scene in authored.scenes
            if previous.get(scene["id"]) != scene_content_digest(scene)
        ]
        spec["scenes"] = authored.scenes
        spec["sound_events"] = authored.sound_events
        spec["story_requirements"] = requirements
        save_project(project_dir, spec)

        if arguments.get("image_assets"):
            image_error = self._store_image_assets(project_dir, arguments["image_assets"])
            if image_error is not None:
                return image_error

        write_storyboard_review(project_dir, authored.scenes, authored.sound_events)
        # 台本が変われば成果物は古い。承認も QA もやり直しなので draft へ戻す。
        write_state(project_dir, state=STATE_DRAFT)
        return ok_result(
            {
                "ok": True,
                "videoId": video_id,
                "changedSceneIds": changed,
                "unchangedSceneIds": [
                    scene["id"] for scene in authored.scenes if scene["id"] not in changed
                ],
                "sceneCount": len(authored.scenes),
                "estimatedDurationSec": authored.estimated_duration_sec,
                "storyboardReview": str(project_dir / "preview" / "storyboard-review.md"),
                "warnings": authored.warnings,
            }
        )

    def get_video_preview(self, arguments: dict[str, Any]) -> types.CallToolResult:
        video_id = str(arguments["video_id"])
        project_dir = self._project_dir(video_id)
        if project_dir is None:
            return _error("VIDEO_NOT_FOUND", f"動画 {video_id} が見つかりません")

        storyboard: dict[str, Any] | None = None
        review = project_dir / "preview" / "storyboard-review.json"
        if review.is_file():
            try:
                raw = json.loads(review.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = None
            if isinstance(raw, dict):
                # ディスク上は snake_case、MCP の応答は camelCase で揃える。
                storyboard = {
                    "sceneCount": raw.get("scene_count"),
                    "soundEventCount": raw.get("sound_event_count"),
                    "scenes": raw.get("scenes") or [],
                    "path": str(project_dir / "preview" / "storyboard-review.md"),
                }

        spec = load_project(project_dir) or {}
        scene_artifacts = []
        for scene in spec.get("scenes") or []:
            scene_id = str(scene.get("id"))
            output = project_dir / "scenes" / scene_id / "output.mp4"
            scene_artifacts.append(
                {
                    "id": scene_id,
                    "kind": scene.get("kind"),
                    "title": scene.get("title"),
                    "video": str(output) if output.is_file() else None,
                }
            )

        sheet = project_dir / CONTACT_SHEET_FILE
        qa = load_qa_report(project_dir)
        state = read_state(project_dir)
        return ok_result(
            {
                "ok": True,
                "videoId": video_id,
                "state": (state or {}).get("state") or "draft",
                "storyboard": storyboard,
                "contactSheet": str(sheet) if sheet.is_file() else None,
                "sceneArtifacts": scene_artifacts,
                "qaSummary": (
                    {
                        "ok": qa.get("ok"),
                        "failed": [
                            c["id"] for c in (qa.get("checks") or []) if c.get("status") == "fail"
                        ],
                        "humanRequired": qa.get("human_required") or [],
                    }
                    if qa
                    else None
                ),
                "imageAssets": spec.get("image_assets") or [],
            }
        )

    def start_render_video(self, arguments: dict[str, Any]) -> types.CallToolResult:
        if self._job_tools is None:
            return _error("JOBS_UNAVAILABLE", "ジョブ基盤が利用できません")
        video_id = str(arguments["video_id"])
        project_dir = self._project_dir(video_id)
        if project_dir is None:
            return _error("VIDEO_NOT_FOUND", f"動画 {video_id} が見つかりません")
        return self._job_tools.start_render_video(
            {
                "video_id": video_id,
                "project_dir": str(project_dir),
                "sound_intensity": arguments.get("sound_intensity") or "subtle",
                # 起動コマンドではなく**プロファイル名**だけをジョブへ渡す
                "capture_profile": arguments.get("capture_profile"),
            }
        )

    def video_status(self, arguments: dict[str, Any]) -> types.CallToolResult:
        project_dir = self._project_dir(str(arguments["video_id"]))
        if project_dir is None:
            return _error("VIDEO_NOT_FOUND", "動画が見つかりません")
        state = read_state(project_dir) or {}
        qa = load_qa_report(project_dir) or {}
        return ok_result(
            {
                "ok": True,
                "videoId": arguments["video_id"],
                "state": state.get("state"),
                "code": state.get("code"),
                "phase": state.get("phase"),
                "durationSec": state.get("duration_sec"),
                "sceneCount": state.get("scene_count"),
                "qaOk": qa.get("ok"),
                "qaFailed": [
                    c["id"] for c in (qa.get("checks") or []) if c.get("status") == "fail"
                ],
                "warnings": state.get("warnings") or [],
            }
        )

    def get_video(self, arguments: dict[str, Any]) -> types.CallToolResult:
        project_dir = self._project_dir(str(arguments["video_id"]))
        if project_dir is None:
            return _error("VIDEO_NOT_FOUND", "動画が見つかりません")
        spec = load_project(project_dir) or {}
        state = read_state(project_dir) or {}
        approval = verify_approval(project_dir)
        outputs = {
            name: str(project_dir / name)
            for name in (
                "output.mp4",
                "output-with-audio.mp4",
                "thumbnail.png",
                "video-metadata.json",
                "citations.json",
                "qa-report.json",
                "distribution-report.json",
                "subtitles/narration.srt",
            )
            if (project_dir / name).is_file()
        }
        return ok_result(
            {
                "ok": True,
                "videoId": spec.get("video_id"),
                "title": spec.get("title"),
                "state": state.get("state"),
                "durationSec": state.get("duration_sec"),
                "sceneCount": len(spec.get("scenes") or []),
                "sources": [
                    {"path": s.get("path"), "selection": s.get("selection")}
                    for s in (spec.get("sources") or [])
                ],
                "metadata": load_metadata(project_dir),
                "qa": load_qa_report(project_dir),
                "distribution": load_distribution_report(project_dir),
                "approval": {"valid": approval.ok, "code": approval.code},
                "outputs": outputs,
                # 外部送信はしない。人が手で登録するためのパス一覧を返すだけ
                "uploadNote": "外部への送信は行いません。手動アップロード用のパスです",
            }
        )

    def list_videos(self, arguments: dict[str, Any]) -> types.CallToolResult:
        warnings = catalog.reconcile_if_needed(
            self._conn, self._reports_dir, root_dir=self._repo_root
        )
        repo = VideoProjectRepository(self._conn)
        rows, total = repo.list(
            state=arguments.get("state"),
            limit=int(arguments.get("limit") or 20),
            offset=int(arguments.get("offset") or 0),
        )
        return ok_result(
            {
                "ok": True,
                "total": total,
                "items": [
                    {
                        "videoId": row["id"],
                        "title": row["title"],
                        "state": row["state"],
                        "durationSec": row["duration_sec"],
                        "sceneCount": row["scene_count"],
                        "createdAt": row["created_at"],
                    }
                    for row in rows
                ],
                "warnings": warnings,
            }
        )

    def run_video_qa(self, arguments: dict[str, Any]) -> types.CallToolResult:
        project_dir = self._project_dir(str(arguments["video_id"]))
        if project_dir is None:
            return _error("VIDEO_NOT_FOUND", "動画が見つかりません")
        spec = load_project(project_dir) or {}
        report = run_qa(project_dir, spec=spec)
        write_report(report, project_dir)
        return ok_result(
            {
                "ok": True,
                "videoId": arguments["video_id"],
                "qaOk": report.ok,
                "checks": [c.to_dict() for c in report.checks],
                "humanRequired": report.human_required,
                # FAIL でも成果物は残す（承認だけがブロックされる）
                "artifactsKept": True,
            }
        )

    def approve_video(self, arguments: dict[str, Any]) -> types.CallToolResult:
        if arguments.get("confirmed") is not True:
            return _error("CONFIRMATION_REQUIRED", "confirmed: true を指定してください")
        project_dir = self._project_dir(str(arguments["video_id"]))
        if project_dir is None:
            return _error("VIDEO_NOT_FOUND", "動画が見つかりません")
        result = run_approve(project_dir, approver=str(arguments["approver"]))
        if not result.ok:
            return _error(
                result.code or "APPROVAL_FAILED", result.message or "承認できませんでした"
            )
        return ok_result(
            {
                "ok": True,
                "videoId": arguments["video_id"],
                "approvalId": (result.record or {}).get("approval_id"),
                "expiresAt": (result.record or {}).get("expires_at"),
                "note": "社内配布確認用の記録です。外部への送信は行いません",
            }
        )

    def set_distribution(self, arguments: dict[str, Any]) -> types.CallToolResult:
        project_dir = self._project_dir(str(arguments["video_id"]))
        if project_dir is None:
            return _error("VIDEO_NOT_FOUND", "動画が見つかりません")
        spec = load_project(project_dir) or {}
        dist = dict(spec.get("distribution") or {})
        for key in ("classification", "audience", "allowed_groups", "public_candidate"):
            if key in arguments:
                dist[key] = arguments[key]
        spec["distribution"] = dist

        # 降格ルールは validate 側で必ず再適用される（true のまま残らない）
        validated = validate_video_project_spec(spec)
        if not validated.ok or validated.spec is None:
            return _error(
                "INVALID_VIDEO_SPEC",
                "distribution の指定が不正です",
                errors=[e.to_dict() for e in validated.errors],
            )
        save_project(project_dir, validated.spec)
        decision = evaluate_distribution(
            validated.spec,
            project_dir,
            qa=load_qa_report(project_dir),
            requested_public=bool(dist.get("public_candidate")),
        )
        write_distribution_report(decision, project_dir)
        return ok_result(
            {
                "ok": True,
                "videoId": arguments["video_id"],
                "distribution": decision.to_dict(),
            }
        )

    def request_public_review(self, arguments: dict[str, Any]) -> types.CallToolResult:
        project_dir = self._project_dir(str(arguments["video_id"]))
        if project_dir is None:
            return _error("VIDEO_NOT_FOUND", "動画が見つかりません")
        spec = load_project(project_dir) or {}
        decision = request_public_review(spec, project_dir, qa=load_qa_report(project_dir))
        if decision.code:
            write_distribution_report(decision, project_dir)
            return _error(
                decision.code,
                "公開審査へ提出できません",
                distribution=decision.to_dict(),
            )
        dist = dict(spec.get("distribution") or {})
        dist["public_review_status"] = decision.public_review_status
        spec["distribution"] = dist
        save_project(project_dir, spec)
        write_distribution_report(decision, project_dir)
        return ok_result(
            {
                "ok": True,
                "videoId": arguments["video_id"],
                "publicReviewStatus": decision.public_review_status,
                "note": "ステータスを記録しただけです。外部へは何も送信していません",
            }
        )

    def list_capture_profiles(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        return ok_result(
            {
                "ok": True,
                "profiles": list_profiles(self._repo_root),
                "note": "起動コマンドは返しません。登録は運用者が設定ファイルで行います",
            }
        )


def handlers_for(tools: KbVideoTools) -> dict[str, Any]:
    return {
        "validate_video_script": tools.validate_video_script,
        "create_video_project": tools.create_video_project,
        "update_video_script": tools.update_video_script,
        "get_video_preview": tools.get_video_preview,
        "start_render_video": tools.start_render_video,
        "video_status": tools.video_status,
        "get_video": tools.get_video,
        "list_videos": tools.list_videos,
        "run_video_qa": tools.run_video_qa,
        "approve_video": tools.approve_video,
        "set_distribution": tools.set_distribution,
        "request_public_review": tools.request_public_review,
        "list_capture_profiles": tools.list_capture_profiles,
    }


__all__ = [
    "MAX_SYNC_SCENES",
    "TOOL_DESCRIPTIONS",
    "KbVideoTools",
    "handlers_for",
    "list_tools",
    "validate_arguments",
]
