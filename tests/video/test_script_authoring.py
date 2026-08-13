"""エージェントが書いた台本の受け入れ口（`author_script`）。

台本はエージェントが書くが、**そのまま描画はしない**。既存の検証チェーン
（validate_script_draft → build_scenes → validate_scene_spec → validate_story_content）
をそのまま受け入れゲートとして通す。ここで固定するのは、エージェントが自力で
直せるだけの情報がエラーに載ることと、CLI 経路と結果が食い違わないこと。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from abist_kb.application.video.input_resolver import ResolvedInput
from abist_kb.application.video.script_planner import author_script

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"


def _load_golden(name: str) -> dict[str, Any]:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _write_docs(docs_dir: Path, paths: list[str], *, lines: int = 1000) -> list[ResolvedInput]:
    resolved: list[ResolvedInput] = []
    for path in paths:
        target = docs_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(
            f"# 見出し{n}" if n % 40 == 1 else f"- 項目{n}の内容です" for n in range(1, lines + 1)
        )
        target.write_text(body, encoding="utf-8")
        resolved.append(
            ResolvedInput(
                path=path,
                selection="explicit_primary",
                require_usage=True,
                origin={"type": "kb_path"},
                content_hash="a" * 64,
            )
        )
    return resolved


@pytest.fixture
def golden_env(tmp_path: Path):
    golden = _load_golden("activity_story")
    docs_dir = tmp_path / "docs"
    resolved = _write_docs(docs_dir, golden["inputs"]["kb_paths"])
    return golden, docs_dir, resolved


# -- 受け入れ ---------------------------------------------------------------------


def test_golden_script_is_accepted(golden_env) -> None:
    golden, docs_dir, resolved = golden_env
    result = author_script(
        golden["script"],
        resolved,
        docs_dir=docs_dir,
        story_requirements=golden["story_requirements"],
    )
    assert result.ok, result.errors
    assert len(result.scenes) == len(golden["script"]["scenes"])
    assert result.errors == []


def test_authoring_is_deterministic(golden_env) -> None:
    golden, docs_dir, resolved = golden_env
    first = author_script(golden["script"], resolved, docs_dir=docs_dir)
    second = author_script(golden["script"], resolved, docs_dir=docs_dir)
    assert [s["scene_spec"] for s in first.scenes] == [s["scene_spec"] for s in second.scenes]


def test_content_hash_is_computed_not_trusted(golden_env) -> None:
    """出典ハッシュは実ファイルから計算する。作者が書いた値は使わない。"""
    golden, docs_dir, resolved = golden_env
    result = author_script(golden["script"], resolved, docs_dir=docs_dir)
    hashes = [
        source["content_hash"]
        for scene in result.scenes
        for source in (scene["scene_spec"].get("sources") or [])
    ]
    assert hashes
    assert all(len(h) == 64 for h in hashes)


# -- strict モードのエラー -----------------------------------------------------------


def test_broken_scene_reports_its_id_instead_of_being_dropped(golden_env) -> None:
    """壊れたシーンは黙って間引かれず、どのシーンかが分かる形で報告される。"""
    golden, docs_dir, resolved = golden_env
    script = json.loads(json.dumps(golden["script"]))
    broken = next(s for s in script["scenes"] if (s.get("diagram") or {}).get("kind") == "timeline")
    broken["diagram"]["points"] = []

    result = author_script(script, resolved, docs_dir=docs_dir)

    assert not result.ok
    assert any(error.get("sceneId") == broken["id"] for error in result.errors)


def test_errors_carry_a_fix_hint(golden_env) -> None:
    golden, docs_dir, resolved = golden_env
    script = json.loads(json.dumps(golden["script"]))
    broken = next(s for s in script["scenes"] if (s.get("diagram") or {}).get("kind") == "timeline")
    broken["diagram"]["points"] = []

    result = author_script(script, resolved, docs_dir=docs_dir)

    assert all(error.get("fixHint") for error in result.errors)


def test_lenient_mode_keeps_the_pipeline_forgiving(golden_env) -> None:
    """無人経路（CLI）は従来どおり、描けないシーンを落として続行する。"""
    golden, docs_dir, resolved = golden_env
    script = json.loads(json.dumps(golden["script"]))
    broken = next(s for s in script["scenes"] if (s.get("diagram") or {}).get("kind") == "timeline")
    broken["diagram"]["points"] = []

    result = author_script(script, resolved, docs_dir=docs_dir, strict=False)

    assert result.ok
    assert broken["id"] not in {scene["id"] for scene in result.scenes}
    assert any(broken["id"] in warning for warning in result.warnings)


def test_unknown_source_ref_is_reported(golden_env) -> None:
    golden, docs_dir, resolved = golden_env
    script = json.loads(json.dumps(golden["script"]))
    scene = script["scenes"][1]
    scene.setdefault("claims", []).append(
        {"text": "根拠のない主張です。", "kind": "fact", "source_refs": ["s99"]}
    )

    result = author_script(script, resolved, docs_dir=docs_dir)

    assert any("s99" in json.dumps(error, ensure_ascii=False) for error in result.errors) or any(
        "s99" in warning for warning in result.warnings
    )


def test_declared_requirements_are_enforced(golden_env) -> None:
    golden, docs_dir, resolved = golden_env
    result = author_script(
        golden["script"],
        resolved,
        docs_dir=docs_dir,
        story_requirements={"required_topics": ["決して現れない語句"]},
    )
    assert not result.ok
    assert any(error["code"] == "MISSING_STORY_TOPIC" for error in result.errors)


# -- 尺の見積り --------------------------------------------------------------------


def test_scene_durations_are_estimated_from_caption_length(golden_env) -> None:
    golden, docs_dir, resolved = golden_env
    result = author_script(golden["script"], resolved, docs_dir=docs_dir)
    assert result.estimated_duration_sec > 0
    assert len(result.scene_estimates) == len(result.scenes)
    longest = max(result.scene_estimates, key=lambda e: e["estimatedSec"])
    shortest = min(result.scene_estimates, key=lambda e: e["estimatedSec"])
    assert longest["captionChars"] >= shortest["captionChars"]
