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


# -- chart（台本からの変換経路） -------------------------------------------------------


def test_a_chart_scene_can_be_written_in_a_script(golden_env) -> None:
    """`chart` は kind・テンプレート・検証を入れただけでは書き手から使えない。

    ScriptDraft の `diagram.kind` から SceneSpec へ落とす分岐が無いと、
    黙って explain になって数値がただの文字列で並ぶ。
    """
    golden, docs_dir, resolved = golden_env
    path = golden["inputs"]["kb_paths"][0]
    script = {
        "title": "工数の計画と実績",
        "scenes": [
            {
                "id": "s01",
                "role": "body",
                "title": "案件別の工数",
                "narration": {"text": "計画と実績を比べます。", "source_refs": ["s1"]},
                "diagram": {
                    "kind": "chart",
                    "variant": "grouped_bar",
                    "value_label": "単位: 時間",
                    "source": {"path": path, "start": 1, "end": 8},
                    "series": [
                        {
                            "label": "案件A",
                            "unit": "h",
                            "values": [{"name": "計画", "value": 40}, {"name": "実績", "value": 36}],
                        },
                        {
                            "label": "案件B",
                            "unit": "h",
                            "values": [{"name": "計画", "value": 32}, {"name": "実績", "value": 35}],
                        },
                    ],
                },
            }
        ],
        "sound_events": [],
    }

    result = author_script(script, resolved, docs_dir=docs_dir)

    assert result.ok, result.errors
    spec = result.scenes[0]["scene_spec"]
    assert spec["scene_kind"] == "chart", "chart を書いたのに別の種別になっている"
    assert spec["chart"]["variant"] == "grouped_bar"
    assert [b["label"] for b in spec["beats"]] == ["案件A", "案件B"]
    assert all(b["source_refs"] for b in spec["beats"]), "数値に出典が付いていない"


def test_a_chart_without_series_is_not_rendered_as_something_else(golden_env) -> None:
    """中身の無い chart は黙って別種別に化けさせず、エラーにする。"""
    golden, docs_dir, resolved = golden_env
    script = {
        "title": "t",
        "scenes": [
            {
                "id": "s01",
                "role": "body",
                "title": "空のグラフ",
                "diagram": {
                    "kind": "chart",
                    "series": [],
                    "source": {"path": golden["inputs"]["kb_paths"][0], "start": 1, "end": 8},
                },
            }
        ],
        "sound_events": [],
    }
    assert not author_script(script, resolved, docs_dir=docs_dir).ok


# -- role からカード種別を推測する ------------------------------------------------------


@pytest.mark.parametrize(
    "role,expected",
    # 表紙・章扉・エンドカードは題字だけで成立する（本文が無くてもよい面）。
    # summary / key_points は中身が要るので、別のテストで内容つきで確かめる。
    [("intro", "title"), ("chapter", "chapter"), ("ending", "ending")],
)
def test_a_card_role_without_a_diagram_becomes_that_card(golden_env, role, expected) -> None:
    """`role` だけ書いたシーンが explain に化けないこと。

    Skill の「構成の型」は表紙(title)・章扉(chapter)・エンドカード(ending)を
    role で書くように読める。`diagram` を書かないと全部 explain になるのでは、
    型どおりに書いた台本が章扉の無い動画になる。
    """
    golden, docs_dir, resolved = golden_env
    script = {
        "title": "t",
        "scenes": [
            {
                "id": "s01",
                "role": role,
                "title": "章のタイトル",
                "narration": {"text": "説明です。"},
            }
        ],
        "sound_events": [],
    }

    result = author_script(script, resolved, docs_dir=docs_dir)

    assert result.ok, result.errors
    assert result.scenes[0]["scene_spec"]["scene_kind"] == expected


def test_an_explicit_diagram_still_wins(golden_env) -> None:
    """role の推測は、書き手が明示した diagram を上書きしない。"""
    golden, docs_dir, resolved = golden_env
    script = {
        "title": "t",
        "scenes": [
            {
                "id": "s01",
                "role": "chapter",
                "title": "章のタイトル",
                "narration": {"text": "説明です。", "source_refs": ["s1"]},
                "claims": [{"text": "要点です", "kind": "fact", "source_refs": ["s1"]}],
                "diagram": {
                    "kind": "key_points",
                    "source": {"path": golden["inputs"]["kb_paths"][0], "start": 1, "end": 8},
                },
            }
        ],
        "sound_events": [],
    }

    result = author_script(script, resolved, docs_dir=docs_dir)

    assert result.scenes[0]["scene_spec"]["scene_kind"] == "key_points"


def test_a_summary_role_with_content_becomes_a_summary_card(golden_env) -> None:
    """まとめは中身があって初めてまとめになる（題字だけの要約は無い）。"""
    golden, docs_dir, resolved = golden_env
    script = {
        "title": "t",
        "scenes": [
            {
                "id": "s01",
                "role": "summary",
                "title": "まとめ",
                "narration": {"text": "要点を確認します。", "source_refs": ["s1"]},
                "claims": [{"text": "分けて渡す", "kind": "fact", "source_refs": ["s1"]}],
                "diagram": {
                    "source": {"path": golden["inputs"]["kb_paths"][0], "start": 1, "end": 8}
                },
            }
        ],
        "sound_events": [],
    }
    result = author_script(script, resolved, docs_dir=docs_dir)
    assert result.ok, result.errors
    assert result.scenes[0]["scene_spec"]["scene_kind"] == "summary"


def test_a_body_role_without_a_diagram_still_falls_back_to_explain(golden_env) -> None:
    """本編は従来どおり。推測するのは対応するカードがある role だけ。"""
    golden, docs_dir, resolved = golden_env
    script = {
        "title": "t",
        "scenes": [
            {"id": "s01", "role": "body", "title": "本編", "narration": {"text": "説明です。"}}
        ],
        "sound_events": [],
    }
    result = author_script(script, resolved, docs_dir=docs_dir)
    assert result.scenes[0]["scene_spec"]["scene_kind"] == "explain"
