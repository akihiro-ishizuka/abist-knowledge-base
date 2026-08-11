"""動画 MCP ツールの契約（Phase 10）。

一番大事なのは「**MCP から任意コードを実行できないこと**」。
`capture_profile`（登録済みの名前）以外の経路で起動コマンドを渡せない、を
スキーマとハンドラの両面で固定する。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.mcp import kb_video


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_app_db(tmp_path / "app.sqlite")


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "notes").mkdir(parents=True)
    for index in range(4):
        (root / "notes" / f"n{index}.md").write_text(
            f"# 会議 {index}\n\n## 決まったこと\n\n"
            "- 編集画面はビューオンリー方針を確認した\n"
            "- 履歴は50件まで保持する\n"
            "- 単一アプリへ統合する\n\n"
            "## 次にやること\n\n- 起動遅延の要件化\n- 手順書の同梱\n",
            encoding="utf-8",
        )
    return root


@pytest.fixture
def tools(conn: sqlite3.Connection, docs: Path, tmp_path: Path) -> kb_video.KbVideoTools:
    return kb_video.KbVideoTools(
        conn,
        docs_dir=docs,
        reports_dir=tmp_path / "reports",
        repo_root=tmp_path,
    )


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


class TestToolSurface:
    def test_every_tool_has_a_description(self) -> None:
        for tool in kb_video.list_tools():
            assert tool.description
            assert tool.name in kb_video.TOOL_DESCRIPTIONS

    def test_handlers_cover_every_tool(self, tools) -> None:
        handlers = kb_video.handlers_for(tools)
        assert set(handlers) == {tool.name for tool in kb_video.list_tools()}

    def test_no_youtube_tool_is_exposed(self) -> None:
        """将来バックログ（自動投稿）が実装されていないこと。"""
        names = {tool.name for tool in kb_video.list_tools()}
        assert not any("youtube" in n or "upload" in n or "publish" in n for n in names)

    def test_schemas_reject_unknown_arguments(self) -> None:
        error = kb_video.validate_arguments("get_video", {"video_id": "x", "extra": 1})
        assert error is not None and error.isError

    def test_capture_profile_is_a_name_not_a_command(self) -> None:
        tool = next(t for t in kb_video.list_tools() if t.name == "start_render_video")
        schema = tool.inputSchema["properties"]["capture_profile"]
        assert schema == {"type": "string"}, "文字列（プロファイル名）以外を受け付けている"

    def test_no_tool_accepts_a_launch_command(self) -> None:
        for tool in kb_video.list_tools():
            properties = (tool.inputSchema or {}).get("properties") or {}
            for name, schema in properties.items():
                assert name not in ("command", "launch", "argv", "shell", "repo")
                # 配列で任意の文字列を受け取る口も塞ぐ（コマンド列の抜け道になる）
                if name == "capture_profile":
                    assert schema.get("type") == "string"


class TestPlanAndCreate:
    def test_plan_returns_a_duration_plan(self, tools, docs) -> None:
        payload = _payload(
            tools.plan_video(
                {
                    "title": "テスト動画",
                    "inputs": {"kb_directories": ["notes"]},
                    "target_duration_sec": {"min": 120, "max": 180},
                }
            )
        )
        assert payload["ok"] is True
        assert payload["durationPlan"]["ok"] is True
        assert payload["sceneCount"] == payload["durationPlan"]["scene_count"]

    def test_plan_reports_insufficient_content(self, tools, tmp_path, docs) -> None:
        payload = _payload(
            tools.plan_video(
                {
                    "title": "長すぎる",
                    "inputs": {"kb_paths": ["notes/n0.md"]},
                    "target_duration_sec": {"min": 900, "max": 1200},
                }
            )
        )
        assert payload["ok"] is False
        assert payload["code"] == "INSUFFICIENT_CONTENT_FOR_DURATION"

    def test_create_project_writes_to_disk(self, tools, tmp_path) -> None:
        payload = _payload(
            tools.create_video_project(
                {"title": "テスト動画", "inputs": {"kb_directories": ["notes"]}}
            )
        )
        assert payload["ok"] is True
        project_dir = Path(payload["projectDir"])
        assert (project_dir / "project-spec.json").is_file()
        assert (project_dir / "inputs-manifest.json").is_file()

    def test_create_rejects_paths_outside_docs(self, tools) -> None:
        payload = _payload(
            tools.create_video_project(
                {"title": "外を見る", "inputs": {"kb_paths": ["../../secret.md"]}}
            )
        )
        assert payload["ok"] is False
        assert payload["code"] == "NO_RESOLVABLE_INPUT"


class TestLookupsAreConfined:
    @pytest.mark.parametrize("video_id", ["../..", "..\\..", "../other", "/etc"])
    def test_video_id_cannot_escape_the_reports_dir(self, tools, video_id) -> None:
        payload = _payload(tools.get_video({"video_id": video_id}))
        assert payload["ok"] is False
        assert payload["code"] == "VIDEO_NOT_FOUND"

    def test_unknown_video_is_reported(self, tools) -> None:
        payload = _payload(tools.video_status({"video_id": "20990101T000000Z-video-0000"}))
        assert payload["code"] == "VIDEO_NOT_FOUND"


class TestApprovalAndDistribution:
    def _project(self, tools) -> Path:
        payload = _payload(
            tools.create_video_project(
                {"title": "テスト動画", "inputs": {"kb_directories": ["notes"]}}
            )
        )
        return Path(payload["projectDir"])

    def test_approve_requires_confirmation(self, tools) -> None:
        project = self._project(tools)
        payload = _payload(
            tools.approve_video(
                {"video_id": project.name, "approver": "me@example.invalid", "confirmed": False}
            )
        )
        assert payload["ok"] is False
        assert payload["code"] == "CONFIRMATION_REQUIRED"

    def test_approve_fails_without_artifacts(self, tools) -> None:
        project = self._project(tools)
        payload = _payload(
            tools.approve_video(
                {"video_id": project.name, "approver": "me@example.invalid", "confirmed": True}
            )
        )
        assert payload["ok"] is False
        assert payload["code"] == "MANUAL_PACK_INCOMPLETE"

    def test_set_distribution_downgrades_public_candidate(self, tools) -> None:
        project = self._project(tools)
        # 機密ソースにしてから public_candidate を要求する
        spec = json.loads((project / "project-spec.json").read_text(encoding="utf-8"))
        spec["sources"][0]["sensitivity"] = "confidential"
        (project / "project-spec.json").write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8"
        )
        payload = _payload(
            tools.set_distribution({"video_id": project.name, "public_candidate": True})
        )
        assert payload["ok"] is True
        assert payload["distribution"]["public_candidate"] is False

    def test_public_review_is_denied_for_confidential(self, tools) -> None:
        project = self._project(tools)
        spec = json.loads((project / "project-spec.json").read_text(encoding="utf-8"))
        spec["sources"][0]["sensitivity"] = "confidential"
        (project / "project-spec.json").write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8"
        )
        payload = _payload(tools.request_public_review({"video_id": project.name}))
        assert payload["ok"] is False
        assert payload["code"] == "PUBLIC_CANDIDATE_DENIED"

    def test_get_video_states_that_nothing_is_sent(self, tools) -> None:
        project = self._project(tools)
        payload = _payload(tools.get_video({"video_id": project.name}))
        assert payload["ok"] is True
        assert "外部への送信は行いません" in payload["uploadNote"]


class TestCaptureProfiles:
    def test_listing_does_not_leak_commands(self, tools, tmp_path) -> None:
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "capture-profiles.json").write_text(
            json.dumps(
                {
                    "profiles": {
                        "app": {
                            "repo": {"url": "https://e.invalid/a.git", "ref": "main"},
                            "launch": {
                                "type": "web",
                                "command": ["python", "-m", "http.server"],
                                "url": "http://127.0.0.1:1/",
                            },
                            "shots": [{"id": "home"}],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        payload = _payload(tools.list_capture_profiles({}))
        assert payload["profiles"] == ["app"]
        assert "python" not in json.dumps(payload)
