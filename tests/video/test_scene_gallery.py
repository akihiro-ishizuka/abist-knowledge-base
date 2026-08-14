"""シーン種別の見本（ギャラリー）。

`list_scene_kinds` が返すのは文字の説明だけで、書き手は17種の見た目を**想像して**
選んでいた。その結果が「14シーン中10が key_points」という実測値になっている。
**見えないものは選ばれない。**

単調さを後から警告する（`test_composition.py`）だけでは足りず、
選ぶ前に実物を見せる側も要る。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.domain.scene_spec import SCENE_KINDS

REPO_ROOT = Path(__file__).resolve().parents[2]
GALLERY_DIR = REPO_ROOT / "assets" / "scene-gallery"
GENERATOR = REPO_ROOT / "scripts" / "generate-scene-gallery.py"


def _kinds() -> list[str]:
    return [info["kind"] for info in SCENE_KINDS]


# -- 見本が揃っていること -------------------------------------------------------------


def test_every_scene_kind_has_a_preview() -> None:
    """種別を足したら見本も足す。**見本の無い種別は選ばれない。**"""
    missing = [kind for kind in _kinds() if not (GALLERY_DIR / f"{kind}.png").is_file()]
    assert not missing, f"見本の無い種別: {missing}"


def test_previews_are_not_empty() -> None:
    for kind in _kinds():
        path = GALLERY_DIR / f"{kind}.png"
        assert path.stat().st_size > 1000, f"{kind} の見本が小さすぎる（描けていない）"


def test_no_orphan_previews() -> None:
    """存在しない種別の見本が残っていないこと。"""
    known = set(_kinds())
    orphans = [p.stem for p in GALLERY_DIR.glob("*.png") if p.stem not in known]
    assert not orphans, f"対応する種別の無い見本: {orphans}"


# -- 生成器 -------------------------------------------------------------------------


def test_the_generator_is_tracked() -> None:
    """コミット済みの PNG をどう作ったか復元できること（音源生成器と同じ扱い）。"""
    assert GENERATOR.is_file()


def test_the_generator_covers_every_kind() -> None:
    """例文の定義漏れを、描く前に気付けるようにする。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("generate_scene_gallery", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.EXAMPLES) == set(_kinds())


def test_examples_are_valid_scene_specs() -> None:
    """見本そのものが SceneSpec の検証を通ること（通らない例を見せない）。"""
    import importlib.util

    from abist_kb.domain.scene_spec import validate_scene_spec

    spec = importlib.util.spec_from_file_location("generate_scene_gallery", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for kind in _kinds():
        result = validate_scene_spec(module.build_spec(kind))
        assert result.ok, (kind, [e.to_dict() for e in result.errors])


# -- MCP から見えること ---------------------------------------------------------------


def test_list_scene_kinds_exposes_the_preview() -> None:
    from abist_kb.presentation.mcp.kb_visualize import scene_kinds_payload

    payload = scene_kinds_payload()
    for entry in payload["scene_kinds"]:
        assert entry.get("preview"), entry["kind"]
        assert (REPO_ROOT / entry["preview"]).is_file(), entry["kind"]


def test_preview_paths_are_repo_relative() -> None:
    """絶対パスを返すと、別マシンへ持って行った成果物が壊れる。"""
    from abist_kb.presentation.mcp.kb_visualize import scene_kinds_payload

    for entry in scene_kinds_payload()["scene_kinds"]:
        preview = entry["preview"]
        assert not Path(preview).is_absolute()
        assert preview.startswith("assets/scene-gallery/")


# -- 決定論 -------------------------------------------------------------------------


@pytest.mark.skipif(
    not (GALLERY_DIR / "manifest.json").is_file(), reason="manifest がまだ生成されていない"
)
def test_recorded_hashes_match_the_files() -> None:
    """再実行で同じ絵が出ること（音源 manifest と同じ担保）。"""
    import hashlib

    manifest = json.loads((GALLERY_DIR / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["previews"]:
        payload = (GALLERY_DIR / entry["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"], entry["kind"]
