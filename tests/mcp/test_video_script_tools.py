"""エージェントが台本を書くための MCP 契約。

固定するのはこの4点:

- 検証だけなら**何も書かない**（reports/ に触れない）
- 検証エラーは「どのシーンの何が悪く、どう直すか」まで返す
- 台本つきで作ったプロジェクトはそのままレンダリングできる
  （以前は scenes が空のまま作られ、必ず NO_RENDERABLE_SCENE で落ちた）
- 差し替えは全置換で、変わったシーンが分かる
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.presentation.mcp import kb_video

GOLDEN = Path(__file__).parents[1] / "video" / "fixtures" / "golden" / "activity_story.json"


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.fixture
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture
def docs(tmp_path: Path, golden: dict[str, Any]) -> Path:
    root = tmp_path / "docs"
    body = "\n".join(
        f"# 見出し{n}" if n % 40 == 1 else f"- 項目{n}の内容です" for n in range(1, 1001)
    )
    for path in golden["inputs"]["kb_paths"]:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_app_db(tmp_path / "app.sqlite")


@pytest.fixture
def tools(conn: sqlite3.Connection, docs: Path, tmp_path: Path) -> kb_video.KbVideoTools:
    return kb_video.KbVideoTools(
        conn,
        docs_dir=docs,
        reports_dir=tmp_path / "reports",
        repo_root=tmp_path,
    )


def _validate_args(golden: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    args = {
        "title": golden["title"],
        "inputs": golden["inputs"],
        "script": golden["script"],
        "story_requirements": golden["story_requirements"],
    }
    args.update(overrides)
    return args


# -- ツール面 -----------------------------------------------------------------------


class TestToolSurface:
    def test_new_tools_are_exposed(self) -> None:
        names = {tool.name for tool in kb_video.list_tools()}
        assert {"validate_video_script", "update_video_script", "get_video_preview"} <= names

    def test_every_new_tool_has_a_handler(self, tools) -> None:
        handlers = kb_video.handlers_for(tools)
        assert set(handlers) == {tool.name for tool in kb_video.list_tools()}

    def test_render_no_longer_offers_tts(self) -> None:
        """ナレーション音声は作らない。選ばせる口も残さない。"""
        render = next(t for t in kb_video.list_tools() if t.name == "start_render_video")
        assert "tts" not in render.inputSchema["properties"]


# -- validate_video_script ----------------------------------------------------------


class TestValidate:
    def test_valid_script_passes(self, tools, golden) -> None:
        payload = _payload(tools.validate_video_script(_validate_args(golden)))
        assert payload["ok"], payload
        assert payload["sceneCount"] == len(golden["script"]["scenes"])
        assert payload["estimatedDurationSec"] > 0

    def test_validation_writes_nothing(self, tools, golden, tmp_path: Path) -> None:
        tools.validate_video_script(_validate_args(golden))
        assert not (tmp_path / "reports").exists()

    def test_errors_name_the_scene_and_how_to_fix_it(self, tools, golden) -> None:
        script = json.loads(json.dumps(golden["script"]))
        broken = next(
            s for s in script["scenes"] if (s.get("diagram") or {}).get("kind") == "timeline"
        )
        broken["diagram"]["points"] = []

        payload = _payload(
            _validate_args(golden, script=script)
            and tools.validate_video_script(_validate_args(golden, script=script))
        )

        assert not payload["ok"]
        assert any(e.get("sceneId") == broken["id"] for e in payload["errors"])
        assert all(e.get("fixHint") for e in payload["errors"])

    def test_scene_estimates_are_returned(self, tools, golden) -> None:
        payload = _payload(tools.validate_video_script(_validate_args(golden)))
        assert len(payload["scenes"]) == payload["sceneCount"]
        assert all("estimatedSec" in scene for scene in payload["scenes"])
        assert all("captionChars" in scene for scene in payload["scenes"])

    def test_declared_requirements_are_enforced(self, tools, golden) -> None:
        payload = _payload(
            tools.validate_video_script(
                _validate_args(golden, story_requirements={"required_topics": ["現れない語"]})
            )
        )
        assert not payload["ok"]
        assert any(e["code"] == "MISSING_STORY_TOPIC" for e in payload["errors"])


# -- create_video_project（台本つき） -------------------------------------------------


class TestCreateWithScript:
    def test_scenes_are_persisted(self, tools, golden) -> None:
        payload = _payload(tools.create_video_project(_validate_args(golden)))
        assert payload["ok"], payload
        spec = json.loads(
            (Path(payload["projectDir"]) / "project-spec.json").read_text(encoding="utf-8")
        )
        assert len(spec["scenes"]) == len(golden["script"]["scenes"])
        assert spec["story_requirements"] == golden["story_requirements"]

    def test_a_project_cannot_be_created_without_a_script(self, tools, golden) -> None:
        """台本を機械生成する経路は無い。**描けない空箱を作らせない。**"""
        payload = _payload(
            tools.create_video_project(
                {
                    "title": golden["title"],
                    "inputs": golden["inputs"],
                    "target_duration_sec": {"min": 120, "max": 240},
                }
            )
        )
        assert not payload["ok"]
        assert payload["code"] == "SCRIPT_REQUIRED"
        assert payload["errors"][0]["fixHint"]

    def test_storyboard_review_is_written_before_any_render(self, tools, golden) -> None:
        payload = _payload(tools.create_video_project(_validate_args(golden)))
        review = Path(payload["projectDir"]) / "preview" / "storyboard-review.md"
        assert review.is_file()

    def test_broken_script_is_rejected_without_creating_a_project(
        self, tools, golden, tmp_path: Path
    ) -> None:
        script = json.loads(json.dumps(golden["script"]))
        script["scenes"][1]["title"] = "凡例"

        payload = _payload(tools.create_video_project(_validate_args(golden, script=script)))

        assert not payload["ok"]
        videos = tmp_path / "reports" / "videos"
        assert not videos.exists() or not list(videos.iterdir())


# -- update_video_script -------------------------------------------------------------


class TestUpdate:
    def _create(self, tools, golden) -> str:
        return _payload(tools.create_video_project(_validate_args(golden)))["videoId"]

    def test_changed_scenes_are_reported(self, tools, golden) -> None:
        video_id = self._create(tools, golden)
        script = json.loads(json.dumps(golden["script"]))
        script["scenes"][2]["narration"]["text"] = "書き換えた本文です。"

        payload = _payload(tools.update_video_script({"video_id": video_id, "script": script}))

        assert payload["ok"], payload
        assert payload["changedSceneIds"] == [script["scenes"][2]["id"]]

    def test_unchanged_script_reports_no_change(self, tools, golden) -> None:
        video_id = self._create(tools, golden)
        payload = _payload(
            tools.update_video_script({"video_id": video_id, "script": golden["script"]})
        )
        assert payload["changedSceneIds"] == []

    def test_update_resets_the_project_to_draft(self, tools, golden, tmp_path: Path) -> None:
        video_id = self._create(tools, golden)
        tools.update_video_script({"video_id": video_id, "script": golden["script"]})
        state = json.loads(
            (tmp_path / "reports" / "videos" / video_id / "state.json").read_text(encoding="utf-8")
        )
        assert state["state"] == "draft"

    def test_unknown_video_is_reported(self, tools, golden) -> None:
        payload = _payload(
            tools.update_video_script({"video_id": "nope", "script": golden["script"]})
        )
        assert not payload["ok"]
        assert payload["code"] == "VIDEO_NOT_FOUND"


# -- get_video_preview ---------------------------------------------------------------


class TestPreview:
    def test_preview_returns_the_storyboard(self, tools, golden) -> None:
        video_id = _payload(tools.create_video_project(_validate_args(golden)))["videoId"]
        payload = _payload(tools.get_video_preview({"video_id": video_id}))
        assert payload["ok"], payload
        assert payload["storyboard"]["sceneCount"] == len(golden["script"]["scenes"])
        assert payload["state"]

    def test_preview_reports_missing_artifacts_without_failing(self, tools, golden) -> None:
        video_id = _payload(tools.create_video_project(_validate_args(golden)))["videoId"]
        payload = _payload(tools.get_video_preview({"video_id": video_id}))
        assert payload["contactSheet"] is None
        assert payload["qaSummary"] is None

    def test_unknown_video_is_reported(self, tools) -> None:
        payload = _payload(tools.get_video_preview({"video_id": "nope"}))
        assert not payload["ok"]
        assert payload["code"] == "VIDEO_NOT_FOUND"


# -- 持ち込み画像 ---------------------------------------------------------------------


class TestImageAssets:
    _PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082"
    )

    def test_supplied_image_is_copied_and_recorded(self, tools, golden, tmp_path: Path) -> None:
        image = tmp_path / "fig.png"
        image.write_bytes(self._PNG)

        payload = _payload(
            tools.create_video_project(
                _validate_args(
                    golden,
                    image_assets=[{"id": "fig1", "path": str(image), "license": "in-house"}],
                )
            )
        )

        assert payload["ok"], payload
        project = Path(payload["projectDir"])
        assert (project / "assets" / "images" / "fig1.png").is_file()
        spec = json.loads((project / "project-spec.json").read_text(encoding="utf-8"))
        assert spec["image_assets"][0]["sha256"]

    def test_unregistered_image_reference_is_rejected(self, tools, golden) -> None:
        script = json.loads(json.dumps(golden["script"]))
        script["scenes"][2]["diagram"] = {"kind": "image", "images": [{"asset_id": "ghost"}]}

        payload = _payload(tools.validate_video_script(_validate_args(golden, script=script)))

        assert not payload["ok"]

    def test_missing_image_file_is_reported(self, tools, golden, tmp_path: Path) -> None:
        payload = _payload(
            tools.create_video_project(
                _validate_args(
                    golden,
                    image_assets=[
                        {"id": "fig1", "path": str(tmp_path / "nope.png"), "license": "x"}
                    ],
                )
            )
        )
        assert not payload["ok"]
        assert payload["code"] == "INVALID_IMAGE_ASSET"
