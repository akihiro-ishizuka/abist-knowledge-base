"""保存済み成果物の SceneSpec が今の検証を通り続けることを固定する。

SceneSpec 1.0 への追加は原則として加算的だが、本ブランチでは例外として
「現在通っているが正しく描画されない入力」を2件だけ弾くようにした
(`duplicate_label` / `unit` の長さ制限)。この種の引き締めを入れるたび、
`reports/visualizations/` に残っている過去の成果物が再現不能にならないかを
機械的に確認する必要がある。それがこのテストの役目。

新しい制約でここが落ちたら、制約値を緩めるか、その成果物を作り直すかを
判断すること。落ちたまま放置すると「過去の図が二度と再生成できない」状態になる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.domain.scene_spec import validate_scene_spec

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VISUALIZATIONS = _REPO_ROOT / "reports" / "visualizations"


def _stored_specs() -> list[Path]:
    if not _VISUALIZATIONS.exists():
        return []
    return sorted(_VISUALIZATIONS.rglob("scene-spec.json"))


@pytest.mark.parametrize("spec_path", _stored_specs(), ids=lambda p: p.parent.name)
def test_stored_scene_spec_is_still_valid(spec_path: Path) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    result = validate_scene_spec(spec)
    assert result.ok, (
        f"{spec_path} が現在の検証を通りません: {[e.to_dict() for e in result.errors]}"
    )


def test_scan_covers_the_visualizations_directory() -> None:
    """成果物が1件も無いのに緑になっている状態を検出する。

    parametrize は空リストだと何も実行されず、テストがあることの意味が失われる。
    ディレクトリが存在するのに spec が0件なら、走査方法が壊れている可能性が高い。
    """
    if not _VISUALIZATIONS.exists():
        pytest.skip("reports/visualizations/ が無い(未レンダリング環境)")
    dirs = [d for d in _VISUALIZATIONS.rglob("*") if d.is_dir() and (d / "manifest.json").exists()]
    if not dirs:
        pytest.skip("成果物ディレクトリが無い")
    assert _stored_specs(), f"manifest.json はあるのに scene-spec.json が見つからない: {dirs}"
