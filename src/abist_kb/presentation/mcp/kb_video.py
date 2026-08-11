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

import sqlite3
from pathlib import Path
from typing import Any

import jsonschema
import mcp.types as types

from abist_kb.application.video import catalog
from abist_kb.application.video.approval import approve as run_approve
from abist_kb.application.video.approval import verify_approval
from abist_kb.application.video.capture_planner import list_profiles
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
from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.application.video.project_store import (
    create_project,
    load_project,
    read_state,
    save_project,
)
from abist_kb.application.video.qa import load_report as load_qa_report
from abist_kb.application.video.qa import run_qa, write_report
from abist_kb.application.video.script_planner import plan_video
from abist_kb.application.video.video_metadata import load_metadata
from abist_kb.domain.video_project_spec import (
    DEFAULT_MAX_DOCS_PER_DIRECTORY,
    DEFAULT_MAX_TOTAL_CANDIDATES,
    validate_video_project_spec,
)
from abist_kb.infrastructure.db.video_projects_repo import VideoProjectRepository
from abist_kb.infrastructure.video.artifact_store import videos_dir
from abist_kb.presentation.mcp.payloads import ok_result, tool_result

#: 同期 `render_video` を許すシーン数の上限。これを超えたら非同期へ誘導する。
MAX_SYNC_SCENES = 2

TOOL_DESCRIPTIONS: dict[str, str] = {
    "plan_video": (
        "docs/ 配下の Markdown から動画の台本案を作る（保存しない）。"
        "target_duration_sec を渡すと章数・シーン数・ナレーション文字量を逆算する。"
        "関連情報だけで目標尺に届かない場合は INSUFFICIENT_CONTENT_FOR_DURATION を返す"
        "（説明を水増しして尺を埋めることはしない）。"
    ),
    "create_video_project": (
        "検証済みの動画プロジェクトを reports/videos/<id> に作る。"
        "kb_paths は必ず本編で使う主入力、kb_directories は選抜対象の候補集合、"
        "kb_queries は補完。描画はしない（start_render_video を使う）。"
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


def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="plan_video",
            description=TOOL_DESCRIPTIONS["plan_video"],
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "purpose": {"type": "string"},
                    "inputs": _INPUTS_SCHEMA,
                    "target_duration_sec": _TARGET_DURATION_SCHEMA,
                },
                "required": ["title", "inputs"],
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
                    "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16"]},
                    "target_duration_sec": _TARGET_DURATION_SCHEMA,
                },
                "required": ["title", "inputs"],
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
                    "tts": {
                        "type": "string",
                        "enum": ["none", "silence", "test_tone", "manual"],
                    },
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

    def plan_video(self, arguments: dict[str, Any]) -> types.CallToolResult:
        resolved = self._resolve(arguments.get("inputs") or {})
        if not resolved.ok:
            return _error(
                "NO_RESOLVABLE_INPUT",
                "題材を解決できませんでした",
                errors=resolved.errors,
                warnings=resolved.warnings,
            )
        plan, _draft = plan_video(
            resolved.inputs,
            docs_dir=self._docs_dir,
            title=str(arguments["title"]),
            purpose=arguments.get("purpose"),
            target_duration_sec=arguments.get("target_duration_sec"),
        )
        if not plan.ok:
            code = (plan.errors[0].get("code") if plan.errors else None) or "INVALID_SCRIPT_DRAFT"
            return _error(
                code,
                (plan.errors[0].get("message") if plan.errors else "台本を作れませんでした"),
                errors=plan.errors,
                durationPlan=plan.duration_plan,
            )
        return ok_result(
            {
                "ok": True,
                "sceneCount": len(plan.scenes),
                "scenes": [
                    {
                        "id": s["id"],
                        "kind": s["kind"],
                        "role": s["role"],
                        "title": s.get("title"),
                        "narrationChars": len((s.get("narration") or {}).get("text") or ""),
                    }
                    for s in plan.scenes
                ],
                "durationPlan": plan.duration_plan,
                "resolvedInputs": len(resolved.inputs),
                "warnings": [*plan.warnings, *(w["message"] for w in resolved.warnings)],
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
        aspect = str(arguments.get("aspect_ratio") or "16:9")
        width, height = (1080, 1920) if aspect == "9:16" else (1920, 1080)
        fmt: dict[str, Any] = {
            "aspect_ratio": aspect,
            "width": width,
            "height": height,
            "fps": 30,
        }
        if arguments.get("target_duration_sec"):
            fmt["target_duration_sec"] = arguments["target_duration_sec"]

        created = create_project(
            {
                "title": str(arguments["title"]),
                "purpose": arguments.get("purpose"),
                "inputs": inputs,
                "format": fmt,
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
        catalog.record_project(self._conn, created.project.dir, root_dir=self._repo_root)
        return ok_result(
            {
                "ok": True,
                "videoId": created.project.video_id,
                "projectDir": str(created.project.dir),
                "sourceCount": len(created.project.spec.get("sources") or []),
                "warnings": [w["message"] for w in (created.warnings or [])],
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
                "tts": arguments.get("tts") or "none",
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
        "plan_video": tools.plan_video,
        "create_video_project": tools.create_video_project,
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
