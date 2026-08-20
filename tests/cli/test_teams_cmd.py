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


def test_inbox_skip_prevents_reselection_and_frees_the_slot(tmp_root: Path) -> None:
    """設計 §5.2, §6.1: `pending` から `inbox skip` しない限り再選択され続ける。

    finding 1 の回帰ガード。`skip` を呼ばないと、質問でないと判定した1件が
    `processing` のまま居座り続け、新着メッセージが繰り上がらなくなる。
    """
    runner.invoke(
        app,
        ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(_inbox(tmp_root, []))],
    )
    runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "ingest",
            "--from",
            str(_inbox(tmp_root, ["a"])),
        ],
    )

    pending = runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "ingest",
            "--from",
            str(_inbox(tmp_root, ["a"])),
        ],
    )
    assert [p["message_id"] for p in json.loads(pending.output)["pending"]] == ["a"]

    skip = runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "skip",
            "--message-id",
            "a",
            "--reason",
            "相槌・了解のため未回答",
        ],
    )
    assert skip.exit_code == 0, skip.output
    assert json.loads(skip.output)["status"] == "skipped"

    next_pending = runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "ingest",
            "--from",
            str(_inbox(tmp_root, ["a", "b"])),
        ],
    )
    assert [p["message_id"] for p in json.loads(next_pending.output)["pending"]] == ["b"]


def test_inbox_skip_rejects_unknown_message(tmp_root: Path) -> None:
    runner.invoke(
        app,
        ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(_inbox(tmp_root, []))],
    )

    result = runner.invoke(
        app, ["--root", str(tmp_root), "teams", "inbox", "skip", "--message-id", "nope"]
    )

    assert result.exit_code != 0
    assert "nope" in result.output


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
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "ingest",
            "--from",
            str(_inbox(tmp_root, ["a"])),
        ],
    )
    runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "ingest",
            "--from",
            str(_inbox(tmp_root, ["a", "b"])),
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
            "--root",
            str(tmp_root),
            "teams",
            "questions",
            "mark",
            "--message-id",
            "b",
            "--status",
            "reminded",
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


def test_backoff_hit_then_status_blocks_then_clear_releases(tmp_root: Path) -> None:
    """設計 §3.2: 429 を受けたら次ティックまで待つ。

    ここが無いと next_allowed_at は永久に null で、429 を受けても次ティックが
    何事もなかったように検索を再開する。
    """
    runner.invoke(
        app,
        ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(_inbox(tmp_root, []))],
    )

    before = runner.invoke(app, ["--root", str(tmp_root), "teams", "gate", "status"])
    assert before.exit_code == 0, before.output
    assert json.loads(before.output)["backoff_allows"] is True

    hit = runner.invoke(
        app, ["--root", str(tmp_root), "teams", "backoff", "hit", "--retry-after", "600"]
    )
    assert hit.exit_code == 0, hit.output
    hit_payload = json.loads(hit.output)
    assert hit_payload["next_allowed_at"] is not None
    # Retry-After に従ったときは間隔を据え置く(設計 §3.2)
    assert hit_payload["interval_minutes"] == 20

    blocked = runner.invoke(app, ["--root", str(tmp_root), "teams", "gate", "status"])
    assert json.loads(blocked.output)["backoff_allows"] is False

    released = runner.invoke(app, ["--root", str(tmp_root), "teams", "backoff", "clear"])
    assert released.exit_code == 0, released.output
    assert json.loads(released.output)["next_allowed_at"] is None

    after = runner.invoke(app, ["--root", str(tmp_root), "teams", "gate", "status"])
    assert json.loads(after.output)["backoff_allows"] is True


def test_backoff_hit_without_retry_after_doubles_the_interval(tmp_root: Path) -> None:
    runner.invoke(
        app,
        ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(_inbox(tmp_root, []))],
    )

    first = runner.invoke(app, ["--root", str(tmp_root), "teams", "backoff", "hit"])
    assert json.loads(first.output)["interval_minutes"] == 40

    second = runner.invoke(app, ["--root", str(tmp_root), "teams", "backoff", "hit"])
    assert json.loads(second.output)["interval_minutes"] == 80


def test_questions_defer_stops_the_reminder_recurring(tmp_root: Path) -> None:
    """「催促しないと決めた」を記録できる(残課題1)。

    これが無いと、確証が持てない質問が `reminders due` に毎ティック出続ける。
    """
    runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "ingest",
            "--from",
            str(_inbox(tmp_root, ["a"])),
        ],
    )
    runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "ingest",
            "--from",
            str(_inbox(tmp_root, ["a", "b"])),
        ],
    )
    runner.invoke(
        app, ["--root", str(tmp_root), "teams", "questions", "track", "--message-id", "b"]
    )

    result = runner.invoke(
        app, ["--root", str(tmp_root), "teams", "questions", "defer", "--message-id", "b"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["deferred_at"] is not None
    # 見送りは「解決した」でも「催促した」でもない
    assert payload["status"] == "open"


def test_questions_defer_rejects_untracked_question(tmp_root: Path) -> None:
    runner.invoke(
        app,
        ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(_inbox(tmp_root, []))],
    )

    result = runner.invoke(
        app, ["--root", str(tmp_root), "teams", "questions", "defer", "--message-id", "nope"]
    )

    assert result.exit_code != 0
    assert "nope" in result.output


def _ingested(tmp_root: Path) -> None:
    runner.invoke(
        app,
        ["--root", str(tmp_root), "teams", "inbox", "ingest", "--from", str(_inbox(tmp_root, []))],
    )


def test_gate_status_reports_operating_hours_and_backoff(tmp_root: Path) -> None:
    """ティックの最初に「今動いてよいか」を1コマンドで答える。

    営業時間(8:30-17:30)の外では応答も催促もしない。`/loop` は24時間回るので、
    ここで止めないと深夜に投稿してしまう。
    """
    _ingested(tmp_root)

    result = runner.invoke(app, ["--root", str(tmp_root), "teams", "gate", "status"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) >= {
        "allowed",
        "within_operating_hours",
        "backoff_allows",
        "search_since",
        "reasons",
    }
    assert payload["backoff_allows"] is True


def test_gate_status_blocks_while_backed_off(tmp_root: Path) -> None:
    _ingested(tmp_root)
    runner.invoke(app, ["--root", str(tmp_root), "teams", "backoff", "hit", "--retry-after", "600"])

    payload = json.loads(
        runner.invoke(app, ["--root", str(tmp_root), "teams", "gate", "status"]).output
    )

    assert payload["backoff_allows"] is False
    assert payload["allowed"] is False
    assert any("バックオフ" in r for r in payload["reasons"])


def test_briefing_is_posted_at_most_once_per_day(tmp_root: Path) -> None:
    """`/loop` は20分間隔なので、記録が無いと朝の提示を何度も投稿する。"""
    _ingested(tmp_root)

    before = json.loads(
        runner.invoke(app, ["--root", str(tmp_root), "teams", "briefing", "status"]).output
    )
    assert before["posted_today"] is False
    assert "todos" in before

    done = runner.invoke(app, ["--root", str(tmp_root), "teams", "briefing", "done"])
    assert done.exit_code == 0, done.output

    after = json.loads(
        runner.invoke(app, ["--root", str(tmp_root), "teams", "briefing", "status"]).output
    )
    assert after["posted_today"] is True


def test_briefing_status_splits_owned_and_team_todos(tmp_root: Path) -> None:
    runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "ingest",
            "--from",
            str(_inbox(tmp_root, ["a"])),
        ],
    )
    runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "inbox",
            "ingest",
            "--from",
            str(_inbox(tmp_root, ["a", "b"])),
        ],
    )
    runner.invoke(
        app, ["--root", str(tmp_root), "teams", "questions", "track", "--message-id", "b"]
    )
    runner.invoke(
        app,
        [
            "--root",
            str(tmp_root),
            "teams",
            "questions",
            "assign",
            "--message-id",
            "b",
            "--owner",
            "t_isaka@abist.co.jp",
        ],
    )

    payload = json.loads(
        runner.invoke(app, ["--root", str(tmp_root), "teams", "briefing", "status"]).output
    )

    assert list(payload["todos"]["by_owner"]) == ["t_isaka@abist.co.jp"]
    assert payload["todos"]["team"] == []
