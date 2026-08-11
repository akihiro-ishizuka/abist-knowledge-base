#!/usr/bin/env python3
"""SceneSpec を読み、テンプレート Scene のみで Manim を programmatic 実行する

呼び出し（Node の visualize-runner.js から）:
    <python> tools/visualize/render_scene.py --spec <outdir>/scene-spec.json --outdir <outdir>

- テンプレート（templates/ の固定モジュール）以外のコードは実行しない
- 出力は <outdir>/output.mp4 または output.png に確定させ、media/ は片付ける
- 最終行に JSON を 1 行出力する（Node がパースする）:
    成功: {"ok": true, "output": "output.mp4", "python": "...", "manim": "..."}
    失敗: {"ok": false, "code": "MANIM_NOT_FOUND" | "RENDER_FAILED", "error": "..."}
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import traceback
from importlib import import_module
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from templates import layout  # noqa: E402  (sys.path を通した後でなければ import できない)

TEMPLATES = {
    "step_explanation": "templates.step_explanation",
    "data_flow_v1": "templates.data_flow_v1",
    "timeline_v1": "templates.timeline_v1",
    "comparison_v1": "templates.comparison_v1",
    "domain_map_v1": "templates.domain_map_v1",
    # --- 動画専用（Phase 7）。単独でも使えるが、主用途は章立て動画の構成要素 ---
    "title_card": "templates.title_card",
    "chapter_card": "templates.chapter_card",
    "key_points": "templates.key_points",
    "quote_card": "templates.quote_card",
    "summary_card": "templates.summary_card",
    "cta_card": "templates.cta_card",
    "ending_card": "templates.ending_card",
    "code_block": "templates.code_block",
    "formula_block": "templates.formula_block",
    "image_still": "templates.image_still",
}


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def load_spec(spec_path: str | Path) -> dict:
    """SceneSpec を読み、`asset_base` を注入する。

    `image` beat の `path` は **scene-spec.json と同じディレクトリからの相対**と
    定義しているので、そのディレクトリをここで教える。spec 本体には書き戻さない
    （ディスク上の spec に絶対パスが混ざると、成果物を別マシンへ持って行った
    ときに壊れるため）。
    """
    path = Path(spec_path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["asset_base"] = str(path.resolve().parent)
    return spec


def build_scene_class(spec_path: str | Path):
    """scene.py（出力ディレクトリに保存する再現用ファイル）から使われる入口。

    manim CLI からも `manim render scene.py KbScene` で再現描画できるよう、
    output_format に応じた Scene クラスを 1 つ返す。
    """
    spec = load_spec(spec_path)
    module = import_module(TEMPLATES[spec["template"]])
    anim, static = module.make_scene_classes(spec)
    return static if spec.get("output_format") == "png" else anim


#: 出力品質。Manim の quality プリセット名ではなく寸法と fps を直接指定する。
#: `high_quality` は 60fps でレンダリング時間が倍増するため使わない。
#: 寸法の実体は templates.layout.FRAME_PIXELS（アスペクト比 x 品質）にあり、
#: **quality は段だけ、frame はアスペクト比だけ**を決める（2つの軸を混ぜない）。
QUALITY_FRAME_RATES = {"draft": 30, "standard": 30, "high": 60}
DEFAULT_QUALITY = "standard"
DEFAULT_ASPECT_RATIO = "16:9"


def _resolve_quality(spec: dict) -> str:
    """spec > 環境変数 > 既定 の順で品質を決める(`resolve_font` と同じ流儀)。"""
    requested = spec.get("quality") or os.environ.get("KB_VISUALIZE_QUALITY") or DEFAULT_QUALITY
    return requested if requested in QUALITY_FRAME_RATES else DEFAULT_QUALITY


def _resolve_aspect_ratio(spec: dict) -> str:
    """`frame.aspect_ratio` を解決する（未指定は 16:9）。

    **既存 spec は frame を持たないので必ず 16:9 に落ちる。** 縦型を明示した
    ときだけフレーム形状が変わる、という後方互換の担保がここ。
    """
    frame = spec.get("frame")
    requested = frame.get("aspect_ratio") if isinstance(frame, dict) else None
    return requested if requested in layout.FRAME_ASPECTS else DEFAULT_ASPECT_RATIO


def _frame_config(spec: dict) -> dict:
    """画素寸法・フレーム寸法・fps をまとめて返す。

    `frame_width` / `frame_height` も明示する。Manim は pixel_width を変えても
    フレーム座標系を追随させないため、縦型では自分で 4.5 x 8.0 に設定しないと
    レイアウトが 16:9 のまま横に潰れる。
    """
    quality = _resolve_quality(spec)
    aspect_ratio = _resolve_aspect_ratio(spec)
    pixel_width, pixel_height = layout.pixel_size(aspect_ratio, quality)
    frame_width, frame_height = layout.frame_size(aspect_ratio)
    return {
        "pixel_width": pixel_width,
        "pixel_height": pixel_height,
        "frame_rate": QUALITY_FRAME_RATES[quality],
        "frame_width": frame_width,
        "frame_height": frame_height,
    }


def _find_output(media_dir: Path, ext: str) -> Path | None:
    matches = sorted(media_dir.rglob(f"output*.{ext}"))
    return matches[0] if matches else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    spec = load_spec(args.spec)
    outdir = Path(args.outdir).resolve()

    try:
        import manim  # noqa: F401
    except ImportError as e:
        emit({"ok": False, "code": "MANIM_NOT_FOUND", "error": str(e)})
        return 3

    from manim import tempconfig

    try:
        module = import_module(TEMPLATES[spec["template"]])
        anim_cls, static_cls = module.make_scene_classes(spec)

        media_dir = outdir / "media"
        is_png = spec["output_format"] == "png"
        conf = {
            "media_dir": str(media_dir),
            "output_file": "output",
            "disable_caching": True,
            "progress_bar": "none",
            "verbosity": "ERROR",
        }
        preset = _frame_config(spec)
        conf.update(preset)
        if is_png:
            # 静的専用シーン + 最終フレーム保存（アニメ途中フレームに依存しない）
            conf.update({"save_last_frame": True, "format": "png"})
            conf.pop("frame_rate", None)
            scene_cls = static_cls
        else:
            scene_cls = anim_cls

        with tempconfig(conf):
            scene_cls().render()

        ext = "png" if is_png else "mp4"
        produced = _find_output(media_dir, ext)
        if produced is None:
            emit({"ok": False, "code": "RENDER_FAILED", "error": f"media/ に output.{ext} が生成されませんでした"})
            return 1
        final = outdir / f"output.{ext}"
        shutil.move(str(produced), str(final))
        shutil.rmtree(media_dir, ignore_errors=True)

        emit({
            "ok": True,
            "output": final.name,
            "python": platform.python_version(),
            "manim": manim.__version__,
        })
        return 0
    except Exception:
        emit({"ok": False, "code": "RENDER_FAILED", "error": traceback.format_exc(limit=5)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
