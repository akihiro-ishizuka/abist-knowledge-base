"""`abist-kb teams` コマンド群(設計 §10.0)。

Track A では判断が Claude 側にあるため tick を3つに分解している。すべて JSON を
標準出力へ返す。
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from abist_kb.presentation.cli.app import app

runner = CliRunner()

CHAT_ID = "19:meeting_Yzc0Yjc4OWUtOGE3Ny00ODYxLWJiZjMtZDI2YzgyMDBlNzBh@thread.v2"


def _inbox(tmp_root: Path, message_ids: list[str]) -> Path:
    path = tmp_root / "inbox.json"
    path.write_text(
        json.dumps(
            {
                "probes": {
                    "い": [
                        {
                            "message_id": mid,
                            "chat_id": CHAT_ID,
                            "sender_email": "t_isaka@abist.co.jp",
                            "sender_name": "井坂 孝",
                            "body": "質問です",
                            "created_at": "2026-08-20T08:00:00Z",
                        }
                        for mid in message_ids
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_ingest_reports_cold_start(tmp_root: Path) -> None:
    inbox = _inbox(tmp_root, ["a"])

    result = runner.invoke(
        app, ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(inbox)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["cold_start"] is True
    assert payload["pending"] == []


def test_ingest_returns_pending_on_second_run(tmp_root: Path) -> None:
    inbox = _inbox(tmp_root, ["a"])
    runner.invoke(app, ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(inbox)])
    inbox2 = _inbox(tmp_root, ["a", "b"])

    result = runner.invoke(
        app, ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(inbox2)]
    )

    payload = json.loads(result.output)
    assert [p["message_id"] for p in payload["pending"]] == ["b"]


def test_state_show_redacts_nothing_secret(tmp_root: Path) -> None:
    runner.invoke(
        app,
        ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(_inbox(tmp_root, []))],
    )

    result = runner.invoke(app, ["--root", str(tmp_root), "teams", "state", "show"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "schema_version" in payload
    assert "sig=" not in result.output


def test_questions_track_then_mark_reminded_stamps_reminded_at(tmp_root: Path) -> None:
    """設計 §7: 質問追跡が CLI から配線されていること。

    ここが無いと `reminders due` は永遠に空を返し、リマインド機能全体が死ぬ。
    """
    runner.invoke(
        app,
        [
            "--root", str(tmp_root), "teams", "inbox", "ingest",
            "--from", str(_inbox(tmp_root, ["a"])),
        ],
    )
    runner.invoke(
        app,
        [
            "--root", str(tmp_root), "teams", "inbox", "ingest",
            "--from", str(_inbox(tmp_root, ["a", "b"])),
        ],
    )

    tracked = runner.invoke(
        app, ["--root", str(tmp_root), "teams", "questions", "track", "--message-id", "b"]
    )
    assert tracked.exit_code == 0, tracked.output
    assert json.loads(tracked.output)["status"] == "open"

    marked = runner.invoke(
        app,
        [
            "--root", str(tmp_root), "teams", "questions", "mark",
            "--message-id", "b", "--status", "reminded",
        ],
    )
    assert marked.exit_code == 0, marked.output
    payload = json.loads(marked.output)
    assert payload["status"] == "reminded"
    assert payload["reminded_at"] is not None


def test_questions_track_rejects_unknown_message(tmp_root: Path) -> None:
    runner.invoke(
        app,
        ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(_inbox(tmp_root, []))],
    )

    result = runner.invoke(
        app, ["--root", str(tmp_root), "teams", "questions", "track", "--message-id", "nope"]
    )

    assert result.exit_code != 0
    assert "nope" in result.output


def test_reply_without_webhook_url_fails_clearly(tmp_root: Path) -> None:
    runner.invoke(
        app,
        ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(_inbox(tmp_root, []))],
    )
    body = tmp_root / "body.md"
    body.write_text("回答です", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "reply",
            "--message-id",
            "a",
            "--title",
            "t",
            "--body",
            str(body),
        ],
    )

    assert result.exit_code != 0
    assert "ABIST_KB_TEAMS_WEBHOOK_URL" in result.output
