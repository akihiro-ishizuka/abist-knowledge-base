"""テンプレート群の静的ガード(manim を import せずに ast で検査する)。

`tests/fixtures/PROVENANCE.md` が M7 の引き取り対象として挙げていた
`visualize-templates-guard.test.js` の移植。テンプレートは manim 必須で主 venv から
import できないため、ソースを ast で読んで方針違反を検出する。
テンプレートを増やすたびに自動で対象へ含まれる。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "tools" / "visualize" / "templates"
#: 描画テンプレート本体(共通基盤の base.py / layout.py は対象外)。
_TEMPLATE_FILES = sorted(
    p for p in _TEMPLATES_DIR.glob("*.py") if p.name not in {"__init__.py", "base.py", "layout.py"}
)


def _ids(paths: list[Path]) -> list[str]:
    return [p.stem for p in paths]


def test_template_files_are_discovered() -> None:
    assert _TEMPLATE_FILES, "テンプレートが1つも見つからない(探索方法が壊れている)"


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_template_does_not_use_latex(path: Path) -> None:
    """LaTeX(Tex / MathTex)は使わない(base.py の方針)。

    日本語が描画できず、TeX ディストリビューションへの依存も持ち込むため。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned = {"Tex", "MathTex", "Title", "SingleStringMathTex"}
    used = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (used & banned), f"{path.name} が LaTeX 系を使っている: {used & banned}"


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_template_exposes_required_functions(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for required in ("build_final_layout", "make_scene_classes"):
        assert required in functions, f"{path.name} に {required} がない"


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_static_scene_does_not_animate(path: Path) -> None:
    """png 用の *Static シーンは self.play / self.wait を呼ばないこと。

    静止画は「アニメの途中フレーム」ではなく専用シーンの一発描画である、という
    render_scene.py の前提を守る。呼ぶと save_last_frame の結果が変わりうる。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Static"):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self"
                and call.func.attr in {"play", "wait"}
            ):
                pytest.fail(f"{path.name} の {node.name} が self.{call.func.attr}() を呼んでいる")


def test_registered_templates_all_exist() -> None:
    """render_scene.py の TEMPLATES に書いたモジュールが実在すること。"""
    source = (_TEMPLATES_DIR.parent / "render_scene.py").read_text(encoding="utf-8")
    registered: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "TEMPLATES" for t in node.targets
        ):
            pairs = zip(node.value.keys, node.value.values, strict=True)  # type: ignore[union-attr]
            registered = {k.value: v.value for k, v in pairs}  # type: ignore[union-attr]
    assert registered, "TEMPLATES を読み取れなかった"
    for name, module in registered.items():
        expected = _TEMPLATES_DIR / f"{module.split('.')[-1]}.py"
        assert expected.exists(), f'TEMPLATES["{name}"] = {module} に対応するファイルが無い'
