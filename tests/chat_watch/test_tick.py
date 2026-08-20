"""ingest と reply のユースケース(設計 §5, §6.1.1)。

state の読み書きを含めた通しの振る舞いを固める。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from abist_kb.application.chat_watch.source import JsonFileMessageSource
from abist_kb.application.chat_watch.state import load_state
from abist_kb.application.chat_watch.tick import run_ingest, run_reply
from abist_kb.config import Settings, load_settings
from abist_kb.domain.chat_watch import MessageStatus
from abist_kb.infrastructure.notify.teams import DeliveryOutcome

NOW = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
CHAT_ID = "19:meeting_Yzc0Yjc4OWUtOGE3Ny00ODYxLWJiZjMtZDI2YzgyMDBlNzBh@thread.v2"


def _inbox(tmp_path: Path, messages: list[dict]) -> Path:
    path = tmp_path / "inbox.json"
    path.write_text(
        json.dumps({"probes": {"い": messages, "の": [], "す": []}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _raw(message_id: str, sender: str = "t_isaka@abist.co.jp", minute: int = 0) -> dict:
    return {
        "message_id": message_id,
        "chat_id": CHAT_ID,
        "sender_email": sender,
        "sender_name": "井坂 孝",
        "body": "#527 の状態を教えてください",
        "created_at": f"2026-08-20T08:{minute:02d}:00Z",
    }


def _settings(tmp_root: Path) -> Settings:
    settings = load_settings(root=tmp_root)
    settings.ensure_directories()
    return settings


def test_first_run_is_cold_start_and_returns_nothing(tmp_root: Path) -> None:
    settings = _settings(tmp_root)
    source = JsonFileMessageSource(_inbox(tmp_root, [_raw("a"), _raw("b")]))

    result = run_ingest(settings, source, now=NOW)

    assert result.cold_start is True
    assert result.pending == []

    state = load_state(settings.teams_state_path)
    assert state.messages["a"].status is MessageStatus.CLOSED_COLD_START


def test_empty_first_run_still_consumes_cold_start(tmp_root: Path) -> None:
    """初回に1件も取れなくても cold start は消化される。

    `not state.messages` で判定すると、ここで永遠に cold start のままになり、
    以後どのメッセージにも応答しなくなる。
    """
    settings = _settings(tmp_root)
    first = run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [])), now=NOW)
    assert first.cold_start is True

    second = run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [_raw("a")])), now=NOW)

    assert second.cold_start is False
    assert [r.message_id for r in second.pending] == ["a"]


def test_second_run_returns_pending(tmp_root: Path) -> None:
    settings = _settings(tmp_root)
    run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [_raw("a")])), now=NOW)

    result = run_ingest(
        settings,
        JsonFileMessageSource(_inbox(tmp_root, [_raw("a"), _raw("b", minute=10)])),
        now=NOW,
    )

    assert result.cold_start is False
    assert [r.message_id for r in result.pending] == ["b"]


def test_pending_is_capped_at_settings_limit(tmp_root: Path) -> None:
    settings = _settings(tmp_root)
    run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [])), now=NOW)
    many = [_raw(str(i), minute=i) for i in range(8)]

    result = run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, many)), now=NOW)

    assert len(result.pending) == settings.teams_max_posts_per_tick == 3
    state = load_state(settings.teams_state_path)
    remaining = [r for r in state.messages.values() if r.status is MessageStatus.DISCOVERED]
    assert len(remaining) == 5


def test_probe_counts_are_reported(tmp_root: Path) -> None:
    settings = _settings(tmp_root)

    result = run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [_raw("a")])), now=NOW)

    assert result.per_probe == {"い": 1, "の": 0, "す": 0}
    assert result.merged == 1


def test_reply_accepted_records_fingerprint(tmp_root: Path) -> None:
    settings = _settings(tmp_root)
    settings.teams_webhook_url = "https://example.com/hook"
    run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [])), now=NOW)
    run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [_raw("a")])), now=NOW)

    transport = httpx.MockTransport(lambda request: httpx.Response(202))
    with httpx.Client(transport=transport) as client:
        result = run_reply(
            settings,
            message_id="a",
            title="#527 の状態",
            body="対応中です",
            sources=["https://abist.esa.io/posts/5346"],
            client=client,
            now=NOW,
        )

    assert result.outcome is DeliveryOutcome.ACCEPTED
    state = load_state(settings.teams_state_path)
    assert state.messages["a"].status is MessageStatus.ACCEPTED
    assert state.messages["a"].outbound_fingerprint is not None


def test_reply_unknown_is_not_retried(tmp_root: Path) -> None:
    """応答が無い場合は unknown。次の ingest で再処理対象にならない。"""
    settings = _settings(tmp_root)
    settings.teams_webhook_url = "https://example.com/hook"
    run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [])), now=NOW)
    run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [_raw("a")])), now=NOW)

    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(_raise)) as client:
        result = run_reply(
            settings,
            message_id="a",
            title="t",
            body="b",
            sources=None,
            client=client,
            now=NOW,
        )

    assert result.outcome is DeliveryOutcome.UNKNOWN
    again = run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [_raw("a")])), now=NOW)
    assert again.pending == []


def test_reply_failed_is_retried(tmp_root: Path) -> None:
    settings = _settings(tmp_root)
    settings.teams_webhook_url = "https://example.com/hook"
    run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [])), now=NOW)
    run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [_raw("a")])), now=NOW)

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        run_reply(
            settings, message_id="a", title="t", body="b", sources=None, client=client, now=NOW
        )

    again = run_ingest(settings, JsonFileMessageSource(_inbox(tmp_root, [_raw("a")])), now=NOW)
    assert [r.message_id for r in again.pending] == ["a"]
