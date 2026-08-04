"""MCP `tools/call` 応答の共通整形(M5 task-1-brief Step2)。

応答は常に `{content: [{type: "text", text: <JSON文字列>}], isError: <bool>}`
の形を取る。`text` 内の JSON 整形(インデント・`ensure_ascii`)は
`tests/fixtures/mcp/**` の `response_raw_line` を実測して決めた:
インデント2・`ensure_ascii=False`(日本語をそのまま出す)・キー順は
Python の dict 挿入順(旧 Node 実装のオブジェクトキー順をそのまま踏襲する)。
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types


def dumps_tool_json(payload: dict[str, Any]) -> str:
    """`content[0].text` に入れる JSON 文字列を fixture と同じ整形で作る。"""
    return json.dumps(payload, indent=2, ensure_ascii=False)


def tool_result(payload: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    """ツール応答を組み立てる。`payload` は `{"ok": ...}` を含む素の dict。"""
    text = dumps_tool_json(payload)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def ok_result(payload: dict[str, Any]) -> types.CallToolResult:
    """成功応答(`ok: true` を含む payload をそのまま包む)。"""
    return tool_result(payload, is_error=False)


def error_result(message: str, *, extra: dict[str, Any] | None = None) -> types.CallToolResult:
    """失敗応答(`{"ok": false, "error": message}`)。"""
    payload: dict[str, Any] = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    return tool_result(payload, is_error=True)


__all__ = ["dumps_tool_json", "error_result", "ok_result", "tool_result"]
