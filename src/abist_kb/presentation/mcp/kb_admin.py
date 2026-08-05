"""kb-admin MCP ツール(MCP-only UI cutover)。

管理操作を Application Service 直呼びで公開する(エージェント向け一次面)。
互換3サーバー(`kb-download`/`kb-search`/`kb-visualize`)のツール一覧・スキーマ・
応答形は一切変更しない。破壊的操作は preview → `confirmed` + 対象再指定。
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import mcp.types as types

from abist_kb.application.audit.backfill_metadata import BackfillMetadataService
from abist_kb.application.audit.check_contradictions import CheckContradictionsService
from abist_kb.application.audit.find_duplicates import FindDuplicatesService
from abist_kb.application.audit.verify_integrity import VerifyIntegrityService
from abist_kb.application.visualization.source_verifier import verify_sources
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import Job, JobState
from abist_kb.domain.scene_spec import validate_scene_spec
from abist_kb.infrastructure.db.connection import connect
from abist_kb.presentation.common.container import ServiceContainer
from abist_kb.presentation.mcp.payloads import app_error_result, error_result, ok_result

_DESTRUCTIVE = types.ToolAnnotations(destructiveHint=True)


def _job_payload(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "state": str(job.state),
        "params": job.params,
        "result": job.result,
        "error": job.error,
        "cancel_requested": job.cancel_requested,
        "retry_of": job.retry_of,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "job_id": event.job_id,
        "phase": event.phase,
        "current": event.current,
        "total": event.total,
        "message": event.message,
        "severity": str(event.severity),
        "item": event.item,
        "timestamp": event.timestamp.isoformat(),
    }


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    destructive: bool = False,
) -> types.Tool:
    schema: dict[str, Any] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "additionalProperties": False,
        "properties": properties,
        "type": "object",
    }
    if required:
        schema["required"] = required
    return types.Tool(
        name=name,
        description=description,
        inputSchema=schema,
        annotations=_DESTRUCTIVE if destructive else None,
    )


def list_tools() -> list[types.Tool]:
    """kb-admin の全ツール定義。"""
    return [
        _tool("source_list", "ソース一覧を返す。", {}),
        _tool(
            "source_add",
            "ソースを追加する。",
            {
                "type": {"type": "string", "minLength": 1},
                "display_name": {"type": "string", "minLength": 1},
                "output_dir": {"type": "string", "minLength": 1},
                "connection": {"type": "object", "additionalProperties": True},
                "enabled": {"type": "boolean"},
            },
            required=["type", "display_name", "output_dir"],
        ),
        _tool(
            "source_edit",
            "ソースを編集する。",
            {
                "source_id": {"type": "string", "minLength": 1},
                "fields": {"type": "object", "additionalProperties": True},
            },
            required=["source_id", "fields"],
        ),
        _tool(
            "source_remove",
            "ソースを削除する。confirmed 無しは preview のみ。"
            "削除時は confirmed=true と confirm_source_id(=source_id) が必要。",
            {
                "source_id": {"type": "string", "minLength": 1},
                "confirmed": {"type": "boolean"},
                "confirm_source_id": {"type": "string", "minLength": 1},
            },
            required=["source_id"],
            destructive=True,
        ),
        _tool(
            "source_test_connection",
            "ソース接続設定の健全性チェック(実ネットワークは呼ばない)。",
            {"source_id": {"type": "string", "minLength": 1}},
            required=["source_id"],
        ),
        _tool("batch_list", "バッチ一覧を返す。", {}),
        _tool(
            "batch_add",
            "バッチを追加する。",
            {
                "name": {"type": "string", "minLength": 1},
                "type": {"type": "string", "minLength": 1},
                "output_dir": {"type": "string"},
                "enabled": {"type": "boolean"},
                "items": {"type": "array", "items": {"type": "object"}},
            },
            required=["name", "type"],
        ),
        _tool(
            "batch_edit",
            "バッチを編集する。",
            {
                "batch_id": {"type": "string", "minLength": 1},
                "fields": {"type": "object", "additionalProperties": True},
            },
            required=["batch_id", "fields"],
        ),
        _tool(
            "batch_remove",
            "バッチを削除する。confirmed 無しは preview のみ。"
            "削除時は confirmed=true と confirm_batch_id(=batch_id) が必要。",
            {
                "batch_id": {"type": "string", "minLength": 1},
                "confirmed": {"type": "boolean"},
                "confirm_batch_id": {"type": "string", "minLength": 1},
            },
            required=["batch_id"],
            destructive=True,
        ),
        _tool(
            "batch_run",
            "バッチ実行ジョブをキューへ投入する(kind=batch)。生きた worker が必要。",
            {"batch_id": {"type": "string", "minLength": 1}},
            required=["batch_id"],
        ),
        _tool(
            "job_list",
            "ジョブ一覧を返す。",
            {"state": {"type": "string", "minLength": 1}},
        ),
        _tool(
            "job_show",
            "ジョブ詳細と履歴を返す。",
            {"job_id": {"type": "string", "minLength": 1}},
            required=["job_id"],
        ),
        _tool(
            "job_retry",
            "failed/interrupted ジョブを再投入する。",
            {"job_id": {"type": "string", "minLength": 1}},
            required=["job_id"],
        ),
        _tool(
            "document_list",
            "文書メタデータ一覧を返す。",
            {
                "source": {"type": "string"},
                "sync_status": {"type": "string"},
                "status": {"type": "string"},
                "path_prefix": {"type": "string"},
            },
        ),
        _tool(
            "document_detail",
            "文書メタデータと本文(あれば)を返す。",
            {"path": {"type": "string", "minLength": 1}},
            required=["path"],
        ),
        _tool(
            "document_update_metadata",
            "文書の status / document_type のみ更新する。",
            {
                "path": {"type": "string", "minLength": 1},
                "status": {"type": "string"},
                "document_type": {"type": "string"},
            },
            required=["path"],
        ),
        _tool(
            "document_delete",
            "文書を削除する。confirmed 無しは preview のみ。"
            "削除時は confirmed=true と confirm_path(=path) が必要。",
            {
                "path": {"type": "string", "minLength": 1},
                "confirmed": {"type": "boolean"},
                "confirm_path": {"type": "string", "minLength": 1},
            },
            required=["path"],
            destructive=True,
        ),
        _tool(
            "chat_start",
            "会話を開始する(openai_api_key 必須)。",
            {"title": {"type": "string"}},
        ),
        _tool(
            "chat_ask",
            "会話に質問する。",
            {
                "conversation_id": {"type": "string", "minLength": 1},
                "question": {"type": "string", "minLength": 1},
            },
            required=["conversation_id", "question"],
        ),
        _tool(
            "chat_history",
            "会話履歴を返す。",
            {"conversation_id": {"type": "string", "minLength": 1}},
            required=["conversation_id"],
        ),
        _tool(
            "quality_run_integrity",
            "整合性監査を実行する(既定は DB 非更新)。",
            {"update_db": {"type": "boolean"}},
        ),
        _tool(
            "quality_run_duplicates",
            "重複監査を実行する。",
            {"corpus": {"type": "string"}},
        ),
        _tool("quality_run_contradictions", "矛盾候補監査を実行する。", {}),
        _tool(
            "quality_run_backfill_metadata",
            "メタデータ補完監査。既定は dry-run。apply=true は confirmed=true が必要。",
            {
                "apply": {"type": "boolean"},
                "confirmed": {"type": "boolean"},
            },
        ),
        _tool(
            "visualization_validate",
            "SceneSpec を検証する(Manim は起動しない)。",
            {
                "scene_spec": {
                    "anyOf": [
                        {"type": "object", "additionalProperties": True},
                        {"type": "string"},
                    ]
                }
            },
            required=["scene_spec"],
        ),
    ]


_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {tool.name: tool.inputSchema for tool in list_tools()}


def validate_arguments(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult | None:
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
            content=[types.TextContent(type="text", text=text)],
            isError=True,
        )
    return None


def _require_confirm_match(
    *,
    confirmed: bool,
    target: str,
    confirm_value: str | None,
    confirm_field: str,
) -> types.CallToolResult | None:
    if not confirmed:
        return None
    if confirm_value is None or confirm_value != target:
        return app_error_result(
            AppError(
                code=ErrorCode.INVALID_INPUT,
                message=(
                    f"破壊的操作には confirmed=true と "
                    f"{confirm_field}(対象と同一値)の再指定が必要です。"
                ),
            )
        )
    return None


class KbAdminTools:
    """kb-admin ツール実装。Application Service を直接呼ぶ。"""

    def __init__(self, container: ServiceContainer) -> None:
        self._c = container

    # -- sources --------------------------------------------------------------

    def source_list(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        return ok_result({"ok": True, "sources": self._c.sources.list()})

    def source_add(self, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            source = self._c.sources.add(
                type=arguments["type"],
                display_name=arguments["display_name"],
                connection=arguments.get("connection"),
                output_dir=arguments["output_dir"],
                enabled=arguments.get("enabled", True),
            )
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "source": source})

    def source_edit(self, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            source = self._c.sources.edit(arguments["source_id"], **arguments["fields"])
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "source": source})

    def source_remove(self, arguments: dict[str, Any]) -> types.CallToolResult:
        source_id = arguments["source_id"]
        confirmed = bool(arguments.get("confirmed"))
        try:
            existing = self._c.sources.get(source_id)
        except AppError as exc:
            return app_error_result(exc)

        if not confirmed:
            return ok_result(
                {
                    "ok": True,
                    "preview": True,
                    "target": {
                        "id": existing["id"],
                        "display_name": existing.get("display_name"),
                        "type": existing.get("type"),
                    },
                    "message": (
                        "削除するには confirmed=true と confirm_source_id を再指定してください。"
                    ),
                }
            )

        mismatch = _require_confirm_match(
            confirmed=confirmed,
            target=source_id,
            confirm_value=arguments.get("confirm_source_id"),
            confirm_field="confirm_source_id",
        )
        if mismatch is not None:
            return mismatch

        try:
            removed = self._c.sources.remove(source_id, confirm=lambda _msg: True, actor="mcp")
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "deleted": bool(removed), "source_id": source_id})

    def source_test_connection(self, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            result = self._c.sources.test_connection(arguments["source_id"])
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, **result})

    # -- batches --------------------------------------------------------------

    def batch_list(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        return ok_result({"ok": True, "batches": self._c.batches.list()})

    def batch_add(self, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            batch = self._c.batches.add(
                name=arguments["name"],
                type=arguments["type"],
                output_dir=arguments.get("output_dir"),
                enabled=arguments.get("enabled", True),
                items=arguments.get("items"),
            )
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "batch": batch})

    def batch_edit(self, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            batch = self._c.batches.edit(arguments["batch_id"], **arguments["fields"])
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "batch": batch})

    def batch_remove(self, arguments: dict[str, Any]) -> types.CallToolResult:
        batch_id = arguments["batch_id"]
        confirmed = bool(arguments.get("confirmed"))
        try:
            existing = self._c.batches.show(batch_id)
        except AppError as exc:
            return app_error_result(exc)

        if not confirmed:
            return ok_result(
                {
                    "ok": True,
                    "preview": True,
                    "target": {"id": existing["id"], "name": existing.get("name")},
                    "message": (
                        "削除するには confirmed=true と confirm_batch_id を再指定してください。"
                    ),
                }
            )

        mismatch = _require_confirm_match(
            confirmed=confirmed,
            target=batch_id,
            confirm_value=arguments.get("confirm_batch_id"),
            confirm_field="confirm_batch_id",
        )
        if mismatch is not None:
            return mismatch

        try:
            removed = self._c.batches.remove(batch_id, confirm=lambda _msg: True, actor="mcp")
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "deleted": bool(removed), "batch_id": batch_id})

    def batch_run(self, arguments: dict[str, Any]) -> types.CallToolResult:
        batch_id = arguments["batch_id"]
        try:
            self._c.batches.show(batch_id)
            job = self._c.jobs.detach("batch", {"batch_id": batch_id})
        except AppError as exc:
            return app_error_result(exc)
        return ok_result(
            {
                "ok": True,
                "job_id": job.id,
                "kind": job.kind,
                "state": str(job.state),
            }
        )

    # -- jobs -----------------------------------------------------------------

    def job_list(self, arguments: dict[str, Any]) -> types.CallToolResult:
        state_raw = arguments.get("state")
        try:
            state_filter = JobState(state_raw) if state_raw else None
        except ValueError:
            return app_error_result(
                AppError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"未知のジョブ状態です: {state_raw}",
                )
            )
        jobs = self._c.jobs.list(state=state_filter)
        return ok_result({"ok": True, "jobs": [_job_payload(j) for j in jobs]})

    def job_show(self, arguments: dict[str, Any]) -> types.CallToolResult:
        job_id = arguments["job_id"]
        try:
            job = self._c.jobs.get(job_id)
            history = self._c.jobs.history(job_id)
        except AppError as exc:
            return app_error_result(exc)
        return ok_result(
            {
                "ok": True,
                "job": _job_payload(job),
                "history": [_event_payload(e) for e in history],
            }
        )

    def job_retry(self, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            job = self._c.jobs.retry(arguments["job_id"])
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "job": _job_payload(job)})

    # -- documents ------------------------------------------------------------

    def document_list(self, arguments: dict[str, Any]) -> types.CallToolResult:
        docs = self._c.documents.list(
            source=arguments.get("source"),
            sync_status=arguments.get("sync_status"),
            status=arguments.get("status"),
            path_prefix=arguments.get("path_prefix"),
        )
        return ok_result({"ok": True, "documents": docs})

    def document_detail(self, arguments: dict[str, Any]) -> types.CallToolResult:
        path = arguments["path"]
        try:
            record = self._c.documents.get(path)
        except AppError as exc:
            return app_error_result(exc)

        file_path = self._c.settings.docs_dir / path
        body: str | None = None
        body_missing = False
        if file_path.is_file():
            try:
                body = file_path.read_text(encoding="utf-8")
            except OSError:
                body_missing = True
        else:
            body_missing = True
        return ok_result(
            {
                "ok": True,
                "document": record,
                "body": body,
                "body_missing": body_missing,
            }
        )

    def document_update_metadata(self, arguments: dict[str, Any]) -> types.CallToolResult:
        path = arguments["path"]
        fields = {
            key: arguments[key]
            for key in ("status", "document_type")
            if key in arguments and arguments[key] is not None
        }
        if not fields:
            return app_error_result(
                AppError(
                    code=ErrorCode.INVALID_INPUT,
                    message="更新する文書メタデータ(status / document_type)を指定してください。",
                )
            )
        try:
            document = self._c.documents.update_metadata(path, fields)
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "document": document})

    def document_delete(self, arguments: dict[str, Any]) -> types.CallToolResult:
        path = arguments["path"]
        confirmed = bool(arguments.get("confirmed"))
        try:
            existing = self._c.documents.get(path)
        except AppError as exc:
            return app_error_result(exc)

        if not confirmed:
            return ok_result(
                {
                    "ok": True,
                    "preview": True,
                    "target": {"path": existing["path"], "source": existing.get("source")},
                    "message": "削除するには confirmed=true と confirm_path を再指定してください。",
                }
            )

        mismatch = _require_confirm_match(
            confirmed=confirmed,
            target=path,
            confirm_value=arguments.get("confirm_path"),
            confirm_field="confirm_path",
        )
        if mismatch is not None:
            return mismatch

        try:
            deleted = self._c.documents.delete(path, confirm=lambda _msg: True, actor="mcp")
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "deleted": bool(deleted), "path": path})

    # -- chat -----------------------------------------------------------------

    def _require_chat(self) -> Any:
        chat = self._c.chat
        if chat is None:
            raise AppError(
                code=ErrorCode.CONFIG_ERROR,
                message="ChatService が利用できません(openai_api_key 未設定)。",
            )
        return chat

    def chat_start(self, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            chat = self._require_chat()
            conversation_id = chat.start_conversation(title=arguments.get("title"))
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "conversation_id": conversation_id})

    def chat_ask(self, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            chat = self._require_chat()
            answer = chat.ask(arguments["conversation_id"], arguments["question"])
        except AppError as exc:
            return app_error_result(exc)
        return ok_result(
            {
                "ok": True,
                "conversation_id": answer.conversation_id,
                "message_id": answer.message_id,
                "text": answer.text,
                "citations": [
                    {
                        "path": c.path,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "valid": c.valid,
                        "reason": c.reason,
                    }
                    for c in answer.citations
                ],
                "citation_warnings": list(answer.citation_warnings),
            }
        )

    def chat_history(self, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            chat = self._require_chat()
            messages = chat.history(arguments["conversation_id"])
        except AppError as exc:
            return app_error_result(exc)
        return ok_result({"ok": True, "messages": messages})

    # -- quality --------------------------------------------------------------

    def quality_run_integrity(self, arguments: dict[str, Any]) -> types.CallToolResult:
        update_db = bool(arguments.get("update_db", False))
        result = VerifyIntegrityService(self._c.conn, docs_dir=self._c.settings.docs_dir).run(
            update_db=update_db
        )
        return ok_result(
            {
                "ok": True,
                "run_id": result.run_id,
                "totals": result.totals.as_dict(),
                "findings": result.findings,
            }
        )

    def quality_run_duplicates(self, arguments: dict[str, Any]) -> types.CallToolResult:
        corpus = arguments.get("corpus") or "work"
        index_path = (
            self._c.settings.work_index_path
            if corpus == "work"
            else self._c.settings.reference_index_path
        )
        index_conn = connect(index_path, read_only=True) if index_path.is_file() else None
        try:
            result = FindDuplicatesService(self._c.conn, index_conn=index_conn).run()
        finally:
            if index_conn is not None:
                index_conn.close()
        return ok_result(
            {
                "ok": True,
                "run_id": result.run_id,
                "totals": result.totals(),
                "same_article": result.same_article,
                "identical": result.identical,
                "near": result.near,
            }
        )

    def quality_run_contradictions(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        result = CheckContradictionsService(self._c.conn, docs_dir=self._c.settings.docs_dir).run()
        return ok_result(
            {
                "ok": True,
                "run_id": result.run_id,
                "candidate_pair_count": result.candidate_pair_count,
                "candidates": [
                    {
                        "path_a": c.path_a,
                        "path_b": c.path_b,
                        "sources": c.sources,
                        "conflicts": c.conflicts,
                    }
                    for c in result.candidates
                ],
            }
        )

    def quality_run_backfill_metadata(self, arguments: dict[str, Any]) -> types.CallToolResult:
        apply = bool(arguments.get("apply", False))
        confirmed = bool(arguments.get("confirmed", False))
        if apply and not confirmed:
            return app_error_result(
                AppError(
                    code=ErrorCode.INVALID_INPUT,
                    message="apply=true には confirmed=true が必要です(破壊的なメタデータ書込)。",
                )
            )
        result = BackfillMetadataService(self._c.conn, docs_dir=self._c.settings.docs_dir).run(
            apply=apply
        )
        return ok_result(
            {
                "ok": True,
                "run_id": result.run_id,
                "mode": result.mode,
                "totals": result.totals.as_dict(),
            }
        )

    # -- visualization --------------------------------------------------------

    def visualization_validate(self, arguments: dict[str, Any]) -> types.CallToolResult:
        raw = arguments.get("scene_spec")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                return error_result(
                    f"scene_spec の JSON パースに失敗しました: {exc}",
                    extra={"code": "INVALID_SCENE_SPEC"},
                )
        if not isinstance(raw, dict):
            return error_result(
                "scene_spec は JSON オブジェクトで指定してください",
                extra={"code": "INVALID_SCENE_SPEC"},
            )

        validated = validate_scene_spec(raw)
        if not validated.ok:
            return ok_result(
                {
                    "ok": False,
                    "code": "INVALID_SCENE_SPEC",
                    "errors": [e.to_dict() for e in validated.errors],
                    "warnings": [],
                }
            )
        assert validated.spec is not None
        verified = verify_sources(validated.spec, self._c.settings.docs_dir)
        if not verified.ok:
            return ok_result(
                {
                    "ok": False,
                    "code": verified.code,
                    "errors": [e.to_dict() for e in verified.errors],
                    "warnings": verified.warnings,
                }
            )
        return ok_result({"ok": True, "spec": verified.spec, "warnings": verified.warnings})


def handlers_for(tools: KbAdminTools) -> dict[str, Any]:
    return {
        "source_list": tools.source_list,
        "source_add": tools.source_add,
        "source_edit": tools.source_edit,
        "source_remove": tools.source_remove,
        "source_test_connection": tools.source_test_connection,
        "batch_list": tools.batch_list,
        "batch_add": tools.batch_add,
        "batch_edit": tools.batch_edit,
        "batch_remove": tools.batch_remove,
        "batch_run": tools.batch_run,
        "job_list": tools.job_list,
        "job_show": tools.job_show,
        "job_retry": tools.job_retry,
        "document_list": tools.document_list,
        "document_detail": tools.document_detail,
        "document_update_metadata": tools.document_update_metadata,
        "document_delete": tools.document_delete,
        "chat_start": tools.chat_start,
        "chat_ask": tools.chat_ask,
        "chat_history": tools.chat_history,
        "quality_run_integrity": tools.quality_run_integrity,
        "quality_run_duplicates": tools.quality_run_duplicates,
        "quality_run_contradictions": tools.quality_run_contradictions,
        "quality_run_backfill_metadata": tools.quality_run_backfill_metadata,
        "visualization_validate": tools.visualization_validate,
    }


__all__ = ["KbAdminTools", "handlers_for", "list_tools", "validate_arguments"]
