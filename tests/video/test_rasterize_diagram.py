"""エージェントが描いた図の PNG 化。

組み込みテンプレートで描けない図は、エージェントが HTML/SVG で描いて持ち込む。
ここで固定するのは:

- **ネットワークを一切通さない**（社内文書由来の図をレンダリングする過程で外へ出ない）
- 動画解像度の倍で書き出す（Ken Burns で寄っても眠くならない）
- 対応外の入力・存在しない入力は、落ちずにコード付きで返る

実ブラウザを要する経路は `KB_RUN_CAPTURE_TESTS=1` のときだけ走らせる
（`test_capture_e2e.py` と同じゲート）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).parents[2] / "tools" / "visualize"


@pytest.fixture(scope="module")
def rasterizer():
    spec = importlib.util.spec_from_file_location(
        "rasterize_diagram", _TOOLS / "rasterize_diagram.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["rasterize_diagram"] = module
    spec.loader.exec_module(module)
    return module


gate = pytest.mark.skipif(
    os.environ.get("KB_RUN_CAPTURE_TESTS") != "1",
    reason="実ブラウザ起動を伴うため既定はスキップ。KB_RUN_CAPTURE_TESTS=1 で実行する",
)


# -- 契約（ブラウザ不要） -------------------------------------------------------------


def test_output_is_double_the_video_resolution(rasterizer) -> None:
    width, height = rasterizer.BASE_SIZE["16:9"]
    assert (width, height) == (1920, 1080)
    assert rasterizer.DEFAULT_SCALE == 2


def test_portrait_size_is_supported(rasterizer) -> None:
    assert rasterizer.BASE_SIZE["9:16"] == (1080, 1920)


def test_only_local_markup_is_accepted(rasterizer) -> None:
    assert set(rasterizer.ALLOWED_SUFFIXES) == {".html", ".htm", ".svg"}


def test_unsupported_input_is_reported(rasterizer, tmp_path: Path) -> None:
    source = tmp_path / "diagram.pdf"
    source.write_bytes(b"%PDF")
    result = rasterizer.rasterize(source, tmp_path / "out.png")
    assert not result["ok"]
    assert result["code"] == "UNSUPPORTED_INPUT"


def test_missing_input_is_reported(rasterizer, tmp_path: Path) -> None:
    result = rasterizer.rasterize(tmp_path / "nope.svg", tmp_path / "out.png")
    assert not result["ok"]
    assert result["code"] == "INPUT_NOT_FOUND"


def test_remote_requests_are_aborted(rasterizer) -> None:
    """ローカルファイル以外は落とす。"""
    aborted: list[str] = []
    continued: list[str] = []

    class FakeRoute:
        def abort(self):
            aborted.append("x")

        def continue_(self):
            continued.append("x")

    class FakeRequest:
        def __init__(self, url: str) -> None:
            self.url = url

    rasterizer._block_everything_remote(
        FakeRoute(), FakeRequest("https://cdn.example.invalid/a.js")
    )
    rasterizer._block_everything_remote(FakeRoute(), FakeRequest("file:///C:/tmp/diagram.html"))

    assert len(aborted) == 1, "外部要求が通ってしまっている"
    assert len(continued) == 1, "ローカル読み込みまで塞いでいる"


@gate
def test_scale_is_clamped(rasterizer, tmp_path: Path) -> None:
    """倍率の指定ミスで巨大な PNG を作らせない。"""
    source = tmp_path / "d.svg"
    source.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' width='40' height='30'/>", encoding="utf-8"
    )
    result = rasterizer.rasterize(source, tmp_path / "out.png", scale=99)
    assert result["ok"], result
    assert result["width"] == 1920 * 4, "倍率の上限が効いていない"


# -- 実ブラウザ ---------------------------------------------------------------------


@gate
def test_svg_becomes_a_png(rasterizer, tmp_path: Path) -> None:
    source = tmp_path / "d.svg"
    source.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' width='400' height='300'>"
        "<rect width='400' height='300' fill='#0d1b2a'/></svg>",
        encoding="utf-8",
    )
    target = tmp_path / "out.png"
    result = rasterizer.rasterize(source, target, scale=1)
    assert result["ok"], result
    assert target.is_file() and target.stat().st_size > 0
