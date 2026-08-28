#!/usr/bin/env python3
"""エージェントが描いた図（ローカルの HTML / SVG）を PNG にする。

組み込みの Manim テンプレートで描けない図（スイムレーン・ER 図・象限図など）は、
エージェントが HTML+SVG として描いたほうが速いし表現力もある。それを動画へ
載せるための橋渡しがこのスクリプト。

**ネットワークは完全に遮断する。** 図の中に外部リソースへの参照が紛れていても、
社内文書を材料に描いた図をレンダリングする過程で外へ出ていくことは無い、を
機械的に保証する（`screen_capture` のゲート思想と同じ）。

**倍解像度で書き出す。** 動画側で Ken Burns のズームをかけるので、等倍だと
寄ったときに眠い絵になる。

呼び出し:
    <python> tools/visualize/rasterize_diagram.py --input diagram.html \\
        --output diagram.png [--aspect 16:9] [--scale 2]

最終行に JSON を1行出力する:
    成功: {"ok": true, "output": "...", "width": 3840, "height": 2160}
    失敗: {"ok": false, "code": "...", "error": "..."}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: 出力の基準サイズ（アスペクト比ごと）。`scale` 倍して書き出す。
BASE_SIZE: dict[str, tuple[int, int]] = {"16:9": (1920, 1080), "9:16": (1080, 1920)}
DEFAULT_ASPECT = "16:9"
#: 既定の倍率。動画解像度の2倍で書き出し、ズームしても解像度が足りる状態にする。
DEFAULT_SCALE = 2
#: 描画完了を待つ上限（ミリ秒）。
LOAD_TIMEOUT_MS = 15_000
#: 受け付ける入力の拡張子。
ALLOWED_SUFFIXES = (".html", ".htm", ".svg")


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _block_everything_remote(route, request) -> None:
    """ローカルファイル以外の要求をすべて落とす。"""
    if request.url.startswith("file://"):
        route.continue_()
    else:
        route.abort()


def rasterize(
    source: Path,
    target: Path,
    *,
    aspect_ratio: str = DEFAULT_ASPECT,
    scale: int = DEFAULT_SCALE,
) -> dict:
    """ローカルの HTML / SVG を PNG へ焼く。"""
    if source.suffix.lower() not in ALLOWED_SUFFIXES:
        return {
            "ok": False,
            "code": "UNSUPPORTED_INPUT",
            "error": f"対応形式は {' / '.join(ALLOWED_SUFFIXES)} です（指定: {source.suffix}）",
        }
    if not source.is_file():
        return {"ok": False, "code": "INPUT_NOT_FOUND", "error": f"図がありません: {source}"}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return {
            "ok": False,
            "code": "PLAYWRIGHT_NOT_FOUND",
            "error": f"playwright が入っていません（uv sync --group dev で導入します）: {exc}",
        }

    width, height = BASE_SIZE.get(aspect_ratio, BASE_SIZE[DEFAULT_ASPECT])
    factor = max(1, min(int(scale), 4))
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": height},
                    device_scale_factor=factor,
                )
                # **外部への通信を落とす。** ローカルファイル以外は一切通さない。
                page.route("**/*", _block_everything_remote)
                page.goto(source.resolve().as_uri(), timeout=LOAD_TIMEOUT_MS)
                # フォント適用とレイアウト確定を待つ（待たないと文字が化けて写る）。
                page.wait_for_load_state("load", timeout=LOAD_TIMEOUT_MS)
                page.evaluate("() => document.fonts && document.fonts.ready")
                page.screenshot(path=str(target), full_page=False)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - 失敗理由をそのまま JSON で返す
        return {"ok": False, "code": "RASTERIZE_FAILED", "error": str(exc)}

    if not target.is_file():
        return {"ok": False, "code": "OUTPUT_NOT_FOUND", "error": "PNG が生成されませんでした"}
    return {
        "ok": True,
        "output": str(target),
        "width": width * factor,
        "height": height * factor,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--aspect", default=DEFAULT_ASPECT, choices=sorted(BASE_SIZE))
    parser.add_argument("--scale", type=int, default=DEFAULT_SCALE)
    args = parser.parse_args()

    result = rasterize(
        Path(args.input), Path(args.output), aspect_ratio=args.aspect, scale=args.scale
    )
    emit(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
