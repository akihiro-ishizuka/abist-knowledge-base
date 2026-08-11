"""Phase 7: フレームプリセット（16:9 / 9:16）と動画専用テンプレートの契約。

manim を import せずに検査できる範囲をここで固定する:

- `layout` のプリセットが純関数として正しいこと
- SceneSpec が `frame` を受け付け、既存 spec（frame なし）が 16:9 のままであること
- 動画専用 kind の必須フィールドと出典ポリシー
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from abist_kb.domain.scene_spec import SCENE_KINDS, validate_scene_spec

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RENDER_SCENE = _REPO_ROOT / "tools" / "visualize" / "render_scene.py"

#: 動画専用テンプレートの一覧（`render_scene.TEMPLATES` へ登録済みであること）。
VIDEO_TEMPLATES = (
    "title_card",
    "chapter_card",
    "key_points",
    "quote_card",
    "summary_card",
    "cta_card",
    "ending_card",
    "code_block",
    "formula_block",
    "image_still",
)


@pytest.fixture(scope="module")
def layout_module():
    from templates import layout  # conftest が sys.path を通している

    return layout


def _card_spec(kind: str, template: str, beats: list[dict], **extra) -> dict:
    return {
        "schema_version": "1.0",
        "scene_kind": kind,
        "output_format": "mp4",
        "template": template,
        "title": "テストカード",
        "sources": [
            {
                "id": "s1",
                "path": "a/b.md",
                "start_line": 1,
                "end_line": 5,
                "content_hash": "0" * 64,
            }
        ],
        "beats": beats,
        **extra,
    }


class TestFramePresets:
    def test_landscape_is_the_default_geometry(self, layout_module) -> None:
        width, height = layout_module.frame_size("16:9")
        assert height == 8.0
        assert width == pytest.approx(14.2222, abs=0.001)

    def test_portrait_is_narrow_not_short(self, layout_module) -> None:
        """縦型は高さを保ったまま幅を狭める（本文の折り返しが自動で効く）。"""
        width, height = layout_module.frame_size("9:16")
        assert height == 8.0
        assert width == pytest.approx(4.5, abs=0.001)

    def test_unknown_aspect_falls_back_to_landscape(self, layout_module) -> None:
        assert layout_module.frame_size("4:3") == layout_module.frame_size("16:9")

    @pytest.mark.parametrize(
        ("aspect", "quality", "expected"),
        [
            ("16:9", "standard", (1920, 1080)),
            ("16:9", "draft", (1280, 720)),
            ("9:16", "standard", (1080, 1920)),
            ("9:16", "draft", (720, 1280)),
        ],
    )
    def test_pixel_sizes(self, layout_module, aspect, quality, expected) -> None:
        assert layout_module.pixel_size(aspect, quality) == expected

    def test_every_preset_matches_its_aspect_ratio(self, layout_module) -> None:
        for aspect, table in layout_module.FRAME_PIXELS.items():
            width_ratio, height_ratio = layout_module.FRAME_ASPECTS[aspect]
            for quality, (pixel_w, pixel_h) in table.items():
                assert pixel_w / pixel_h == pytest.approx(width_ratio / height_ratio, abs=0.001), (
                    f"{aspect}/{quality} の画素比がアスペクト比と食い違う"
                )

    def test_portrait_subtitles_are_shorter(self, layout_module) -> None:
        assert layout_module.subtitle_max_chars("9:16") < layout_module.subtitle_max_chars("16:9")

    def test_safe_area_is_inside_the_frame(self, layout_module) -> None:
        for aspect in ("16:9", "9:16"):
            frame_w, frame_h = layout_module.frame_size(aspect)
            safe_w, safe_h = layout_module.safe_area(aspect)
            assert 0 < safe_w < frame_w
            assert 0 < safe_h < frame_h


@pytest.fixture(scope="module")
def registered() -> set[str]:
    """`render_scene.TEMPLATES` のキー集合（manim 非依存で読むため ast を使う）。"""
    tree = ast.parse(_RENDER_SCENE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "TEMPLATES" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            return {k.value for k in node.value.keys}  # type: ignore[union-attr]
    pytest.fail("TEMPLATES 辞書を読み取れなかった")


class TestRenderSceneWiring:
    """`render_scene.py` は主 venv から import できないので ast で検査する。"""

    @pytest.mark.parametrize("template", VIDEO_TEMPLATES)
    def test_template_is_registered(self, registered, template) -> None:
        assert template in registered

    @pytest.mark.parametrize("template", VIDEO_TEMPLATES)
    def test_template_module_exists(self, template) -> None:
        assert (_REPO_ROOT / "tools" / "visualize" / "templates" / f"{template}.py").is_file()

    def test_frame_and_quality_are_separate_axes(self) -> None:
        """`quality` は段だけ、`frame` はアスペクト比だけを決めること。

        両方が画素寸法を直接持つと、`quality=high` が縦型を横型へ戻すような
        取り違えが起きる。実体は layout.FRAME_PIXELS の1箇所だけにする。
        """
        source = _RENDER_SCENE.read_text(encoding="utf-8")
        assert "QUALITY_PRESETS" not in source, "旧 QUALITY_PRESETS が残っている"
        assert "layout.pixel_size(" in source
        assert "layout.frame_size(" in source


class TestSceneSpecFrame:
    def test_frame_is_optional(self) -> None:
        spec = _card_spec(
            "title", "title_card", [{"type": "statement", "text": "表紙", "decorative": True}]
        )
        assert "frame" not in spec
        assert validate_scene_spec(spec).ok, "既存 spec（frame なし）が通らなくなっている"

    @pytest.mark.parametrize("aspect", ["16:9", "9:16"])
    def test_known_aspect_ratio_is_accepted(self, aspect) -> None:
        spec = _card_spec(
            "title",
            "title_card",
            [{"type": "statement", "text": "表紙", "decorative": True}],
            frame={"aspect_ratio": aspect},
        )
        assert validate_scene_spec(spec).ok

    def test_unknown_aspect_ratio_is_rejected(self) -> None:
        spec = _card_spec(
            "title",
            "title_card",
            [{"type": "statement", "text": "表紙", "decorative": True}],
            frame={"aspect_ratio": "1:1"},
        )
        result = validate_scene_spec(spec)
        assert not result.ok
        assert [e.path for e in result.errors] == ["frame.aspect_ratio"]

    def test_chapter_index_must_be_positive(self) -> None:
        spec = _card_spec(
            "chapter",
            "chapter_card",
            [{"type": "statement", "text": "ねらい", "decorative": True}],
            chapter_index=0,
        )
        assert not validate_scene_spec(spec).ok


class TestVideoCardKinds:
    def test_all_video_kinds_are_registered(self) -> None:
        registered = {k["template"] for k in SCENE_KINDS}
        assert set(VIDEO_TEMPLATES) <= registered

    def test_quote_requires_a_source(self) -> None:
        spec = _card_spec("quote", "quote_card", [{"type": "quote", "text": "原文のまま"}])
        result = validate_scene_spec(spec)
        assert not result.ok
        assert any(e.code == "missing_source_refs" for e in result.errors)

    def test_quote_cannot_be_decorative(self) -> None:
        """引用は「KB にこう書いてある」という主張そのもの（装飾で免除しない）。"""
        spec = _card_spec(
            "quote",
            "quote_card",
            [{"type": "quote", "text": "原文のまま", "decorative": True, "source_refs": ["s1"]}],
        )
        result = validate_scene_spec(spec)
        assert not result.ok
        assert any(e.path.endswith(".decorative") for e in result.errors)

    def test_quote_with_source_is_valid(self) -> None:
        spec = _card_spec(
            "quote",
            "quote_card",
            [
                {
                    "type": "quote",
                    "text": "原文のまま",
                    "attribution": "議事録",
                    "source_refs": ["s1"],
                }
            ],
        )
        assert validate_scene_spec(spec).ok

    @pytest.mark.parametrize("bad", ["../secret.png", "/etc/passwd", "C:/x.png", "a\\b.png"])
    def test_image_path_cannot_escape_the_scene_dir(self, bad) -> None:
        spec = _card_spec(
            "image", "image_still", [{"type": "image", "path": bad, "decorative": True}]
        )
        result = validate_scene_spec(spec)
        assert not result.ok, f"{bad} が通ってしまった"
        assert any(e.path.endswith(".path") for e in result.errors)

    def test_image_relative_path_is_accepted(self) -> None:
        spec = _card_spec(
            "image",
            "image_still",
            [{"type": "image", "path": "captures/editor-home.png", "source_refs": ["s1"]}],
        )
        assert validate_scene_spec(spec).ok

    def test_code_needs_a_source_unless_decorative(self) -> None:
        spec = _card_spec(
            "code", "code_block", [{"type": "code", "text": "x = 1", "language": "py"}]
        )
        assert not validate_scene_spec(spec).ok
        spec = _card_spec(
            "code",
            "code_block",
            [{"type": "code", "text": "x = 1", "language": "py", "decorative": True}],
        )
        assert validate_scene_spec(spec).ok

    def test_empty_card_is_rejected_when_a_beat_is_required(self) -> None:
        assert not validate_scene_spec(_card_spec("key_points", "key_points", [])).ok
        assert not validate_scene_spec(_card_spec("quote", "quote_card", [])).ok

    def test_wrong_beat_type_for_kind_is_rejected(self) -> None:
        spec = _card_spec("quote", "quote_card", [{"type": "statement", "text": "違う型"}])
        result = validate_scene_spec(spec)
        assert not result.ok
        assert any(e.code == "unknown_beat_type" for e in result.errors)
