"""B32doc モジュールコード(例: `prtug_C2`)とワークベンチ名の対応辞書。

旧実装 `tools/knowledge-curator/module_dictionary.py` の移植。CATIA V5 の各
モジュールディレクトリ名は `<code>_C2` の形をとる。索引・昇格から参照され、
モジュールを人が読める「ワークベンチ名」へ写像する。未知のモジュールは
索引化を止めず、`known=False` を返す(後から本辞書を拡充できるようにするため)。
"""

from __future__ import annotations

#: モジュールコード(フルの `*_C2` 名)→ {workbench, category_hint}
MODULES: dict[str, dict[str, str]] = {
    "basug_C2": {"workbench": "Infrastructure", "category_hint": "infrastructure"},
    "prtug_C2": {"workbench": "Part Design", "category_hint": "mechanical"},
    "sdgug_C2": {"workbench": "Generative Shape Design", "category_hint": "shape"},
    "pstug_C2": {"workbench": "Assembly Design", "category_hint": "assembly"},
    "asmug_C2": {"workbench": "Assembly Design", "category_hint": "assembly"},
    "draug_C2": {"workbench": "Drafting", "category_hint": "drafting"},
    "itfug_C2": {"workbench": "Data Exchange", "category_hint": "interop"},
    "bascuitf_C2": {"workbench": "Data Exchange", "category_hint": "interop"},
    "kinug_C2": {"workbench": "DMU Kinematics", "category_hint": "dmu"},
}


def lookup(module_code: str) -> dict[str, object]:
    """モジュールコードから `{workbench, category_hint, known}` を返す。"""
    hit = MODULES.get(module_code)
    if hit:
        return {"workbench": hit["workbench"], "category_hint": hit["category_hint"], "known": True}
    return {"workbench": None, "category_hint": "unknown", "known": False}


def display_name(module_code: str) -> str:
    """workbench 名が無いときの表示フォールバック(例: `sdgug_C2` → `sdgug`)。"""
    if module_code.endswith("_C2"):
        return module_code[:-3]
    return module_code


def workbench_slug(module_code: str) -> str:
    """curated 出力ディレクトリ用スラッグ。"""
    info = lookup(module_code)
    workbench = info["workbench"]
    if workbench:
        return str(workbench).lower().replace(" ", "-").replace("/", "-")
    return display_name(module_code)


__all__ = ["MODULES", "display_name", "lookup", "workbench_slug"]
