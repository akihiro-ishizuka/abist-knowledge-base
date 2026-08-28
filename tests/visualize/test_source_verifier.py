"""`application.visualization.source_verifier` の検証(M7 task-3):

metric の不良出典は即中断、statement/flow_step は警告して除外・カスケードする
非対称ポリシーを確認する。旧実装 `tools/lib/source-verifier.js` の移植先。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.visualization.source_verifier import verify_sources
from abist_kb.domain.line_range import range_hash

_DOC_TEXT = "行1\n行2\n行3\n行4\n行5\n"


@pytest.fixture
def docs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    (d / "doc.md").write_text(_DOC_TEXT, encoding="utf-8")
    return d


def _valid_hash(start: int, end: int) -> str:
    result = range_hash(_DOC_TEXT, start, end)
    assert result.ok
    assert result.hash is not None
    return result.hash


def _source(**overrides):
    src = {
        "id": "s1",
        "path": "doc.md",
        "start_line": 1,
        "end_line": 2,
        "content_hash": _valid_hash(1, 2),
    }
    src.update(overrides)
    return src


def test_all_sources_ok_passes_through_unchanged(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source()],
        "beats": [{"type": "statement", "text": "文", "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is True
    assert result.spec["beats"] == spec["beats"]
    assert result.warnings == []


def test_metric_with_missing_source_aborts_with_source_not_found(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", path="missing.md")],
        "beats": [{"type": "metric", "label": "L", "value": 1, "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "SOURCE_NOT_FOUND"


def test_metric_with_hash_mismatch_aborts_with_source_hash_mismatch(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", content_hash="f" * 64)],
        "beats": [{"type": "metric", "label": "L", "value": 1, "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "SOURCE_HASH_MISMATCH"


def test_statement_with_bad_source_is_dropped_with_warning(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", content_hash="f" * 64), _source(id="s2")],
        "beats": [
            {"type": "statement", "text": "壊れた文", "source_refs": ["s1"]},
            {"type": "statement", "text": "生きてる文", "source_refs": ["s2"]},
        ],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is True
    assert len(result.spec["beats"]) == 1
    assert result.spec["beats"][0]["text"] == "生きてる文"
    assert len(result.warnings) == 1


def test_dropped_flow_step_cascades_to_referencing_transition(docs_dir: Path):
    spec = {
        "scene_kind": "flow",
        "sources": [_source(id="s1", content_hash="f" * 64), _source(id="s2")],
        "beats": [
            {"type": "flow_step", "label": "A", "description": "壊れた", "source_refs": ["s1"]},
            {"type": "flow_step", "label": "B", "description": "生きてる", "source_refs": ["s2"]},
            {"type": "flow_step", "label": "C", "description": "生きてる2", "source_refs": ["s2"]},
            {"type": "transition", "from": "A", "to": "B"},
            {"type": "transition", "from": "B", "to": "C"},
        ],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is True
    kept_types_labels = [
        (b["type"], b.get("label"), b.get("from"), b.get("to")) for b in result.spec["beats"]
    ]
    assert ("flow_step", "A", None, None) not in kept_types_labels
    assert ("transition", None, "A", "B") not in kept_types_labels
    assert ("transition", None, "B", "C") in kept_types_labels
    assert len(result.warnings) == 2  # flow_step除外 + transition連鎖除外


def test_pruning_below_explain_minimum_is_invalid_scene_spec(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", content_hash="f" * 64)],
        "beats": [{"type": "statement", "text": "唯一の文", "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "INVALID_SCENE_SPEC"


def test_pruning_below_flow_minimum_is_invalid_scene_spec(docs_dir: Path):
    spec = {
        "scene_kind": "flow",
        "sources": [_source(id="s1", content_hash="f" * 64), _source(id="s2")],
        "beats": [
            {"type": "flow_step", "label": "A", "description": "壊れた", "source_refs": ["s1"]},
            {"type": "flow_step", "label": "B", "description": "生きてる", "source_refs": ["s2"]},
        ],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "INVALID_SCENE_SPEC"


def test_decorative_beat_bypasses_verification_entirely(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", path="missing.md")],
        "beats": [
            {"type": "statement", "text": "装飾", "decorative": True, "source_refs": ["s1"]},
            {"type": "statement", "text": "本文", "source_refs": []},
        ],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is True
    assert len(result.spec["beats"]) == 2


def test_path_traversal_is_treated_as_not_found(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", path="../outside.md")],
        "beats": [{"type": "metric", "label": "L", "value": 1, "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "SOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# 出典必須ポリシーの網羅テスト
#
# 新しく出典を要求するフィールドを足したのに source_verifier の剪定対象へ
# 入れ忘れると、構文検証は通るのに実ファイル照合が素通りし、ポリシーが
# レンダリング時に迂回される。判定は beat type 単位では足りない
# (transition は label の有無で扱いが変わる)ため、
# 「出典を要求するフィールドの組み合わせ」で網羅する。
# ---------------------------------------------------------------------------


def _spec_with(scene_kind: str, beats: list[dict], *, hash_ok: bool) -> dict:
    template = "step_explanation" if scene_kind == "explain" else "data_flow_v1"
    return {
        "schema_version": "1.0",
        "scene_kind": scene_kind,
        "output_format": "png",
        "template": template,
        "title": "出典剪定の網羅",
        "sources": [
            {
                "id": "s1",
                "path": "doc.md",
                "start_line": 1,
                "end_line": 1,
                "content_hash": ("a" * 64) if hash_ok else ("b" * 64),
            }
        ],
        "beats": beats,
    }


def _docs_with_doc(tmp_path: Path) -> tuple[Path, str]:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_text("本文\n", encoding="utf-8")
    return docs, range_hash("本文\n", 1, 1).hash


def test_labeled_transition_with_bad_source_keeps_arrow_and_drops_label(
    tmp_path: Path,
) -> None:
    """ラベル付き transition の不良出典は、ラベルだけを落として矢印を残す。

    矢印ごと消すと、出典と無関係なグラフ構造が黙って壊れる。
    """
    docs, good = _docs_with_doc(tmp_path)
    spec = _spec_with(
        "flow",
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {
                "type": "transition",
                "from": "A",
                "to": "B",
                "label": "スコア80以上",
                "source_refs": ["s1"],
            },
        ],
        hash_ok=False,
    )
    result = verify_sources(spec, docs)
    assert result.ok, result.errors
    transitions = [b for b in result.spec["beats"] if b["type"] == "transition"]
    assert len(transitions) == 1, "矢印は残さなければならない"
    assert "label" not in transitions[0], "出典の無いラベルは落とさなければならない"
    assert transitions[0]["from"] == "A" and transitions[0]["to"] == "B"
    assert len(result.warnings) == 1
    assert "ラベルのみ" in result.warnings[0]
    assert good  # 未使用警告回避


def test_unlabeled_transition_is_untouched(tmp_path: Path) -> None:
    """ラベルなし transition は従来どおり出典不要(既存挙動の維持)。"""
    docs, _ = _docs_with_doc(tmp_path)
    spec = _spec_with(
        "flow",
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "transition", "from": "A", "to": "B"},
        ],
        hash_ok=False,
    )
    result = verify_sources(spec, docs)
    assert result.ok
    assert result.warnings == []
    assert len([b for b in result.spec["beats"] if b["type"] == "transition"]) == 1


def test_decorative_labeled_transition_bypasses_pruning(tmp_path: Path) -> None:
    docs, _ = _docs_with_doc(tmp_path)
    spec = _spec_with(
        "flow",
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "transition", "from": "A", "to": "B", "label": "装飾", "decorative": True},
        ],
        hash_ok=False,
    )
    result = verify_sources(spec, docs)
    assert result.ok
    assert result.warnings == []
    kept = [b for b in result.spec["beats"] if b["type"] == "transition"][0]
    assert kept.get("label") == "装飾"


@pytest.mark.parametrize(
    ("case", "scene_kind", "beat", "expect"),
    [
        (
            "statement",
            "explain",
            {"type": "statement", "text": "事実", "source_refs": ["s1"]},
            "dropped",
        ),
        (
            "metric",
            "explain",
            {"type": "metric", "label": "件数", "value": 3, "source_refs": ["s1"]},
            "aborted",
        ),
        (
            "flow_step_with_description",
            "flow",
            {"type": "flow_step", "label": "X", "description": "事実", "source_refs": ["s1"]},
            "dropped",
        ),
        (
            "labeled_transition",
            "flow",
            {"type": "transition", "from": "A", "to": "B", "label": "条件", "source_refs": ["s1"]},
            "label_stripped",
        ),
    ],
)
def test_every_fact_bearing_field_is_pruned_on_bad_source(
    tmp_path: Path, case: str, scene_kind: str, beat: dict, expect: str
) -> None:
    """出典を要求する全フィールドが、不良出典で必ず何らかの形で扱われること。

    素通し(warnings が空で beat がそのまま残る)は、この網の失敗を意味する。
    新しい beat type / フィールドを足したらここへ1行足すこと。
    """
    docs, _ = _docs_with_doc(tmp_path)
    # 剪定後も kind の構成制約(explain: statement/metric>=1, flow: flow_step>=2)を
    # 満たすだけの土台を置く。ここが割れると INVALID_SCENE_SPEC になり、
    # 「剪定されたか」ではなく「構成が崩れたか」を見てしまう。
    if scene_kind == "flow":
        padding: list[dict] = [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
        ]
    else:
        padding = [{"type": "statement", "text": "土台", "decorative": True}]
    spec = _spec_with(scene_kind, [*padding, beat], hash_ok=False)

    result = verify_sources(spec, docs)

    if expect == "aborted":
        assert not result.ok, f"{case}: metric は即中断でなければならない"
        assert result.code in ("SOURCE_NOT_FOUND", "SOURCE_HASH_MISMATCH")
        return

    assert result.ok, f"{case}: {result.errors}"
    assert result.warnings, f"{case}: 不良出典が素通りしている(剪定対象の登録漏れ)"
    if expect == "dropped":
        kinds = [b["type"] for b in result.spec["beats"]]
        expected = len([b for b in padding if b["type"] == beat["type"]])
        assert kinds.count(beat["type"]) == expected, f"{case}: beat が除外されていない"
    elif expect == "label_stripped":
        kept = [b for b in result.spec["beats"] if b["type"] == "transition"]
        assert kept and "label" not in kept[0], f"{case}: label が落ちていない"


def test_timeline_point_with_bad_source_is_dropped_with_warning(tmp_path: Path) -> None:
    """timeline_point の剪定対象への登録漏れを検出する。"""
    docs, _ = _docs_with_doc(tmp_path)
    spec = {
        "schema_version": "1.0",
        "scene_kind": "timeline",
        "output_format": "png",
        "template": "timeline_v1",
        "title": "経緯",
        "sources": [
            {"id": "s1", "path": "doc.md", "start_line": 1, "end_line": 1, "content_hash": "b" * 64}
        ],
        "beats": [
            {"type": "timeline_point", "at": "a", "label": "落ちる", "source_refs": ["s1"]},
            {"type": "timeline_point", "at": "b", "label": "残る1", "decorative": True},
            {"type": "timeline_point", "at": "c", "label": "残る2", "decorative": True},
        ],
    }
    result = verify_sources(spec, docs)
    assert result.ok, result.errors
    labels = [b["label"] for b in result.spec["beats"]]
    assert "落ちる" not in labels
    assert result.warnings and "timeline_point" in result.warnings[0]


def test_comparison_item_with_bad_source_is_dropped_with_warning(tmp_path: Path) -> None:
    """comparison_item の剪定対象への登録漏れを検出する。"""
    docs, _ = _docs_with_doc(tmp_path)

    def cell(side, aspect, **kw):
        return {"type": "comparison_item", "side": side, "aspect": aspect, "text": "内容", **kw}

    spec = {
        "schema_version": "1.0",
        "scene_kind": "comparison",
        "output_format": "png",
        "template": "comparison_v1",
        "title": "比較",
        "sources": [
            {"id": "s1", "path": "doc.md", "start_line": 1, "end_line": 1, "content_hash": "b" * 64}
        ],
        "beats": [
            cell("A", "落ちる観点", source_refs=["s1"]),
            cell("A", "残る", decorative=True),
            cell("B", "残る", decorative=True),
        ],
    }
    result = verify_sources(spec, docs)
    assert result.ok, result.errors
    aspects = [b["aspect"] for b in result.spec["beats"]]
    assert "落ちる観点" not in aspects
    assert result.warnings and "comparison_item" in result.warnings[0]


def test_domain_entity_dropped_cascades_to_its_relations(tmp_path: Path) -> None:
    """⚠ domain は3 kind の中で唯一、連鎖除外が必須。

    entity が出典不良で除外されたのに relation が残ると、`boxes.get(name)` が
    None になって矢印が黙って消え、警告なしに図が崩れる。
    """
    docs, good = _docs_with_doc(tmp_path)
    spec = {
        "schema_version": "1.0",
        "scene_kind": "domain",
        "output_format": "png",
        "template": "domain_map_v1",
        "title": "構成",
        "sources": [
            {
                "id": "bad",
                "path": "doc.md",
                "start_line": 1,
                "end_line": 1,
                "content_hash": "b" * 64,
            },
            {"id": "ok", "path": "doc.md", "start_line": 1, "end_line": 1, "content_hash": good},
        ],
        "beats": [
            {"type": "domain_entity", "name": "落ちる", "source_refs": ["bad"]},
            {"type": "domain_entity", "name": "残るA", "source_refs": ["ok"]},
            {"type": "domain_entity", "name": "残るB", "source_refs": ["ok"]},
            {
                "type": "domain_relation",
                "from": "落ちる",
                "to": "残るA",
                "label": "連鎖で消える",
                "source_refs": ["ok"],
            },
            {
                "type": "domain_relation",
                "from": "残るA",
                "to": "残るB",
                "label": "残る",
                "source_refs": ["ok"],
            },
        ],
    }
    result = verify_sources(spec, docs)
    assert result.ok, result.errors

    names = [b.get("name") for b in result.spec["beats"] if b["type"] == "domain_entity"]
    assert "落ちる" not in names

    relations = [b for b in result.spec["beats"] if b["type"] == "domain_relation"]
    assert len(relations) == 1, f"除外済み entity を指す relation が残っている: {relations}"
    assert relations[0]["from"] == "残るA"

    joined = " / ".join(result.warnings)
    assert "domain_entity" in joined
    assert "domain_relation" in joined, "連鎖除外の警告が出ていない"


def test_domain_relation_with_bad_source_is_dropped_but_entities_remain(
    tmp_path: Path,
) -> None:
    docs, good = _docs_with_doc(tmp_path)
    spec = {
        "schema_version": "1.0",
        "scene_kind": "domain",
        "output_format": "png",
        "template": "domain_map_v1",
        "title": "構成",
        "sources": [
            {
                "id": "bad",
                "path": "doc.md",
                "start_line": 1,
                "end_line": 1,
                "content_hash": "b" * 64,
            },
            {"id": "ok", "path": "doc.md", "start_line": 1, "end_line": 1, "content_hash": good},
        ],
        "beats": [
            {"type": "domain_entity", "name": "A", "source_refs": ["ok"]},
            {"type": "domain_entity", "name": "B", "source_refs": ["ok"]},
            {"type": "domain_relation", "from": "A", "to": "B", "source_refs": ["bad"]},
        ],
    }
    result = verify_sources(spec, docs)
    assert result.ok, result.errors
    names = [b.get("name") for b in result.spec["beats"] if b["type"] == "domain_entity"]
    assert names == ["A", "B"], "entity は残ること"
    assert not [b for b in result.spec["beats"] if b["type"] == "domain_relation"]
    assert result.warnings
