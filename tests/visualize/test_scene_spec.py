"""`domain.scene_spec` の検証(M7 task-3): 旧実装 `tools/lib/scene-spec.js` の移植先。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.domain.scene_spec import (
    MAX_BEATS,
    RESERVED_KINDS,
    SCENE_KINDS,
    validate_scene_spec,
)

_HASH = "a" * 64


def _base_source(**overrides):
    src = {
        "id": "s1",
        "path": "foo/bar.md",
        "start_line": 1,
        "end_line": 3,
        "content_hash": _HASH,
    }
    src.update(overrides)
    return src


def _explain_spec(**overrides):
    spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "mp4",
        "template": "step_explanation",
        "title": "テストタイトル",
        "sources": [_base_source()],
        "beats": [
            {"type": "statement", "text": "テスト文", "source_refs": ["s1"]},
        ],
    }
    spec.update(overrides)
    return spec


def test_valid_explain_spec_is_ok():
    result = validate_scene_spec(_explain_spec())
    assert result.ok is True
    assert result.spec is not None


def test_valid_flow_spec_is_ok():
    spec = {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "mp4",
        "template": "data_flow_v1",
        "title": "フロー",
        "sources": [_base_source()],
        "beats": [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "transition", "from": "A", "to": "B"},
        ],
    }
    result = validate_scene_spec(spec)
    assert result.ok is True


def test_empty_object_reports_multiple_errors():
    result = validate_scene_spec({"foo": "bar"})
    assert result.ok is False
    codes = {(e.path, e.code) for e in result.errors}
    assert ("schema_version", "invalid") in codes
    assert ("scene_kind", "unknown_kind") in codes
    assert ("output_format", "invalid") in codes
    assert ("title", "invalid") in codes
    assert ("sources", "invalid") in codes


def test_reserved_kind_reports_reserved_kind_error():
    """未実装の予約 kind は `reserved_kind`(タイポの `unknown_kind` と区別)。

    timeline は実装済みになったので、まだ予約中の comparison を使う。
    RESERVED_KINDS が空になったらこのテストは削除し、
    `test_reserved_and_implemented_kinds_are_disjoint` に役割を移すこと。
    """
    result = validate_scene_spec(_explain_spec(scene_kind="comparison", template="comparison"))
    assert result.ok is False
    assert any(e.code == "reserved_kind" for e in result.errors)


def test_non_object_is_invalid():
    result = validate_scene_spec("not-an-object")
    assert result.ok is False
    assert result.errors[0].path == ""


def test_too_many_beats_is_invalid():
    spec = _explain_spec(
        beats=[
            {"type": "statement", "text": f"文{i}", "source_refs": ["s1"]}
            for i in range(MAX_BEATS + 1)
        ]
    )
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.path == "beats" for e in result.errors)


def test_metric_without_source_refs_is_missing_source_refs():
    spec = _explain_spec(beats=[{"type": "metric", "label": "ラベル", "value": 1}])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "missing_source_refs" for e in result.errors)


def test_statement_decorative_bypasses_source_refs_requirement():
    spec = _explain_spec(beats=[{"type": "statement", "text": "装飾", "decorative": True}])
    result = validate_scene_spec(spec)
    assert result.ok is True


def test_flow_step_description_requires_source_refs_unless_decorative():
    spec = {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "mp4",
        "template": "data_flow_v1",
        "title": "フロー",
        "sources": [_base_source()],
        "beats": [
            {"type": "flow_step", "label": "A", "description": "説明"},
            {"type": "flow_step", "label": "B"},
        ],
    }
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "missing_source_refs" for e in result.errors)


def test_transition_referencing_unknown_flow_label_is_invalid():
    spec = {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "mp4",
        "template": "data_flow_v1",
        "title": "フロー",
        "sources": [_base_source()],
        "beats": [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "transition", "from": "A", "to": "C"},
        ],
    }
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "unknown_label" for e in result.errors)


def test_explain_requires_at_least_one_substantive_beat():
    # schema_version 段階の検証(source_verifier.py の剪定後チェックとは別物)では
    # decorative でも type が statement/metric なら「実質 beat」として数える
    # (旧実装 `scene-spec.js` の substantive フィルタも decorative を見ない)。
    # ここでは transition のみで statement/metric が0件のケースを使う。
    spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "mp4",
        "template": "step_explanation",
        "title": "テスト",
        "sources": [_base_source()],
        "beats": [{"type": "transition", "from": "A", "to": "B"}],
    }
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.path == "beats" and e.code == "invalid" for e in result.errors)


def test_flow_requires_at_least_two_flow_steps():
    spec = {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "mp4",
        "template": "data_flow_v1",
        "title": "フロー",
        "sources": [_base_source()],
        "beats": [{"type": "flow_step", "label": "A"}],
    }
    result = validate_scene_spec(spec)
    assert result.ok is False


def test_duplicate_source_id_is_invalid():
    spec = _explain_spec(sources=[_base_source(id="s1"), _base_source(id="s1")])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "duplicate_id" for e in result.errors)


def test_source_path_traversal_is_invalid():
    spec = _explain_spec(sources=[_base_source(path="../secret.md")])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.path == "sources[0].path" for e in result.errors)


def test_source_content_hash_wrong_length_is_invalid():
    spec = _explain_spec(sources=[_base_source(content_hash="deadbeef")])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.path == "sources[0].content_hash" for e in result.errors)


def test_unknown_source_ref_is_invalid():
    spec = _explain_spec(beats=[{"type": "statement", "text": "文", "source_refs": ["missing"]}])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "unknown_source_ref" for e in result.errors)


def test_scene_kinds_match_list_scene_kinds_contract():
    """`list_scene_kinds` 応答の形を固定する。

    **explain を先頭に保つこと。** `tests/mcp/replay.py::_structural_mismatch` は
    list を先頭要素だけ構造比較するため、先頭が変わらない限り kind を追加しても
    fixture は無風でいられる(値は比較されないので count も自由)。
    """
    assert [k["kind"] for k in SCENE_KINDS] == ["explain", "flow", "timeline"]
    for info in SCENE_KINDS:
        assert set(info.keys()) == {"kind", "description", "template", "required", "beat_types"}


def test_reserved_and_implemented_kinds_are_disjoint():
    """SCENE_KINDS へ追加したのに RESERVED_KINDS から消し忘れる事故を防ぐ。"""
    assert not (set(RESERVED_KINDS) & {k["kind"] for k in SCENE_KINDS})


def test_every_scene_kind_has_a_registered_template():
    """`render_scene.py` の TEMPLATES 辞書への登録漏れを静的に検出する。

    manim を import せずに ast でリテラルだけ読む(主 venv から実行できる)。
    ドメイン層(SCENE_KINDS)とツール層(TEMPLATES)の橋渡しを固定する唯一のテスト。
    """
    import ast

    source = (
        Path(__file__).resolve().parents[2] / "tools" / "visualize" / "render_scene.py"
    ).read_text(encoding="utf-8")
    registered: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "TEMPLATES" for t in node.targets
        ):
            registered = {k.value for k in node.value.keys}  # type: ignore[union-attr]
    assert registered, "TEMPLATES 辞書を読み取れなかった"
    for info in SCENE_KINDS:
        assert info["template"] in registered, (
            f'scene_kind "{info["kind"]}" の template "{info["template"]}" が '
            "render_scene.py の TEMPLATES に登録されていません"
        )


def test_every_beat_type_is_validated():
    """beat_types に書いたのに検証分岐が無い(=素通しする)ことを防ぐ。"""
    known = {
        "statement",
        "metric",
        "transition",
        "flow_step",
        "decision",
        "timeline_point",
    }
    for info in SCENE_KINDS:
        for beat_type in info["beat_types"]:
            assert beat_type in known, (
                f'{info["kind"]} の beat type "{beat_type}" に検証分岐がありません'
            )


def _flow_spec_with_beats(beats: list[dict]) -> dict:
    """duplicate_label 系テスト用の最小 flow spec。"""
    return {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "png",
        "template": "data_flow_v1",
        "title": "重複ラベルの検証",
        "sources": [
            {
                "id": "s1",
                "path": "a/b.md",
                "start_line": 1,
                "end_line": 2,
                "content_hash": "0" * 64,
            }
        ],
        "beats": beats,
    }


def test_duplicate_flow_step_label_is_invalid() -> None:
    """同名 label は data_flow_v1 の boxes[label] を後勝ちで上書きし矢印を誤接続する。"""
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "受信"},
            {"type": "flow_step", "label": "受信"},
            {"type": "flow_step", "label": "送信"},
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    dup = [e for e in result.errors if e.code == "duplicate_label"]
    assert len(dup) == 1
    assert dup[0].path == "beats[1].label"
    assert "beats[0]" in dup[0].message


def test_duplicate_label_is_reported_even_when_decorative() -> None:
    """decorative は出典要件の免除であって、ラベル一意性の免除ではない。"""
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "受信"},
            {"type": "flow_step", "label": "受信", "decorative": True},
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.code == "duplicate_label" for e in result.errors)


def test_triplicate_label_reports_each_later_occurrence() -> None:
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "A"},
        ]
    )
    result = validate_scene_spec(spec)
    dup = sorted(e.path for e in result.errors if e.code == "duplicate_label")
    assert dup == ["beats[1].label", "beats[2].label"]


def test_unique_labels_remain_valid() -> None:
    """既存の正常系が壊れていないこと(後方互換の確認)。"""
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "受信"},
            {"type": "flow_step", "label": "送信"},
            {"type": "transition", "from": "受信", "to": "送信"},
        ]
    )
    assert validate_scene_spec(spec).ok


def test_transition_still_resolves_labels_after_duplicate_check() -> None:
    """flow_labels を集合から dict に変えても unknown_label 検査が効くこと。"""
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "受信"},
            {"type": "flow_step", "label": "送信"},
            {"type": "transition", "from": "受信", "to": "存在しない"},
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.code == "unknown_label" for e in result.errors)


# ---------------------------------------------------------------------------
# Phase 3: transition.label / decision / emphasis / quality / unit
# ---------------------------------------------------------------------------


def test_transition_label_requires_source_refs() -> None:
    """label は独立した事実主張なので出典が要る(from/to だけなら不要のまま)。"""
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "transition", "from": "A", "to": "B", "label": "スコア80以上"},
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.code == "missing_source_refs" for e in result.errors)


def test_transition_without_label_still_needs_no_source_refs() -> None:
    """既存 spec の後方互換(label を持たない transition は従来どおり出典不要)。"""
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "transition", "from": "A", "to": "B"},
        ]
    )
    assert validate_scene_spec(spec).ok


def test_transition_label_over_40_chars_is_invalid() -> None:
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {
                "type": "transition",
                "from": "A",
                "to": "B",
                "label": "あ" * 41,
                "source_refs": ["s1"],
            },
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.path == "beats[2].label" for e in result.errors)


def test_decision_is_accepted_in_flow_and_referenced_by_transition() -> None:
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "受付"},
            {"type": "decision", "label": "承認済み?", "source_refs": ["s1"]},
            {"type": "flow_step", "label": "発行"},
            {"type": "transition", "from": "受付", "to": "承認済み?"},
            {
                "type": "transition",
                "from": "承認済み?",
                "to": "発行",
                "label": "はい",
                "source_refs": ["s1"],
            },
        ]
    )
    assert validate_scene_spec(spec).ok, validate_scene_spec(spec).errors


def test_decision_requires_source_refs() -> None:
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "decision", "label": "条件?"},
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.code == "missing_source_refs" for e in result.errors)


def test_decision_label_over_16_chars_is_invalid() -> None:
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "decision", "label": "あ" * 17, "source_refs": ["s1"]},
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.path == "beats[2].label" for e in result.errors)


def test_decision_rejects_description() -> None:
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "decision", "label": "条件?", "description": "だめ", "source_refs": ["s1"]},
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.path == "beats[2].description" for e in result.errors)


def test_decision_shares_label_namespace_with_flow_step() -> None:
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "審査"},
            {"type": "flow_step", "label": "B"},
            {"type": "decision", "label": "審査", "source_refs": ["s1"]},
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.code == "duplicate_label" for e in result.errors)


def test_decision_is_rejected_in_explain() -> None:
    spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "png",
        "template": "step_explanation",
        "title": "t",
        "sources": [
            {"id": "s1", "path": "a.md", "start_line": 1, "end_line": 1, "content_hash": "0" * 64}
        ],
        "beats": [
            {"type": "statement", "text": "x", "source_refs": ["s1"]},
            {"type": "decision", "label": "条件?", "source_refs": ["s1"]},
        ],
    }
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.code == "unknown_beat_type" for e in result.errors)


@pytest.mark.parametrize("value", ["normal", "key", "warn"])
def test_emphasis_accepts_enumerated_values(value: str) -> None:
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "A", "emphasis": value},
            {"type": "flow_step", "label": "B"},
        ]
    )
    assert validate_scene_spec(spec).ok


def test_emphasis_rejects_free_form_color() -> None:
    spec = _flow_spec_with_beats(
        [
            {"type": "flow_step", "label": "A", "emphasis": "#ff0000"},
            {"type": "flow_step", "label": "B"},
        ]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.path == "beats[0].emphasis" for e in result.errors)


@pytest.mark.parametrize("value", ["draft", "standard", "high"])
def test_quality_accepts_enumerated_values(value: str) -> None:
    spec = _flow_spec_with_beats(
        [{"type": "flow_step", "label": "A"}, {"type": "flow_step", "label": "B"}]
    )
    spec["quality"] = value
    assert validate_scene_spec(spec).ok


def test_quality_rejects_unknown_value() -> None:
    spec = _flow_spec_with_beats(
        [{"type": "flow_step", "label": "A"}, {"type": "flow_step", "label": "B"}]
    )
    spec["quality"] = "ultra"
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.path == "quality" for e in result.errors)


def test_metric_unit_over_8_chars_is_invalid() -> None:
    """unit はテンプレートが値へ直に連結する。1.0 当初から無検証だった穴を塞ぐ。"""
    spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "png",
        "template": "step_explanation",
        "title": "t",
        "sources": [
            {"id": "s1", "path": "a.md", "start_line": 1, "end_line": 1, "content_hash": "0" * 64}
        ],
        "beats": [
            {"type": "metric", "label": "件数", "value": 3, "unit": "あ" * 9, "source_refs": ["s1"]}
        ],
    }
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.path == "beats[0].unit" for e in result.errors)


# ---------------------------------------------------------------------------
# Phase 4: timeline
# ---------------------------------------------------------------------------


def _timeline_spec(beats: list[dict]) -> dict:
    return {
        "schema_version": "1.0",
        "scene_kind": "timeline",
        "output_format": "png",
        "template": "timeline_v1",
        "title": "経緯",
        "sources": [
            {"id": "s1", "path": "a.md", "start_line": 1, "end_line": 1, "content_hash": "0" * 64}
        ],
        "beats": beats,
    }


def _point(at: str = "2026-05-14", label: str = "決定", **extra) -> dict:
    beat = {"type": "timeline_point", "at": at, "label": label, "source_refs": ["s1"]}
    beat.update(extra)
    return beat


def test_valid_timeline_spec_is_ok() -> None:
    result = validate_scene_spec(_timeline_spec([_point(), _point(at="2026-06-11")]))
    assert result.ok, result.errors


def test_timeline_point_requires_source_refs() -> None:
    """時点付きの事実主張なので、description の有無に関わらず出典が要る。"""
    spec = _timeline_spec(
        [{"type": "timeline_point", "at": "2026-05-14", "label": "決定"}, _point()]
    )
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.code == "missing_source_refs" for e in result.errors)


def test_timeline_point_decorative_bypasses_source_refs() -> None:
    spec = _timeline_spec(
        [
            {"type": "timeline_point", "at": "?", "label": "装飾", "decorative": True},
            _point(),
            _point(at="2026-07-01"),
        ]
    )
    assert validate_scene_spec(spec).ok


def test_timeline_requires_at_least_two_points() -> None:
    result = validate_scene_spec(_timeline_spec([_point()]))
    assert not result.ok
    assert any(e.path == "beats" for e in result.errors)


def test_timeline_rejects_more_than_ten_points() -> None:
    result = validate_scene_spec(_timeline_spec([_point(at=f"d{i}") for i in range(11)]))
    assert not result.ok
    assert any(e.path == "beats" for e in result.errors)


def test_timeline_at_is_not_parsed_as_a_date() -> None:
    """原文の粒度(「6/11 定例」「2025年度」)を捨てないため日付解釈しない。"""
    spec = _timeline_spec([_point(at="6/11 定例"), _point(at="2025年度")])
    assert validate_scene_spec(spec).ok


def test_timeline_at_over_24_chars_is_invalid() -> None:
    result = validate_scene_spec(_timeline_spec([_point(at="あ" * 25), _point()]))
    assert not result.ok
    assert any(e.path == "beats[0].at" for e in result.errors)


def test_timeline_rejects_flow_step_beat_type() -> None:
    spec = _timeline_spec([_point(), _point(at="x"), {"type": "flow_step", "label": "A"}])
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.code == "unknown_beat_type" for e in result.errors)


def test_timeline_allows_statement_and_metric() -> None:
    spec = _timeline_spec(
        [
            _point(),
            _point(at="2026-06-11"),
            {"type": "statement", "text": "結論", "source_refs": ["s1"]},
            {"type": "metric", "label": "件数", "value": 4, "source_refs": ["s1"]},
        ]
    )
    assert validate_scene_spec(spec).ok
