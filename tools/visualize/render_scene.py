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
import platform
import shutil
import sys
import traceback
from importlib import import_module
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TEMPLATES = {
    "step_explanation": "templates.step_explanation",
    "data_flow_v1": "templates.data_flow_v1",
}


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def load_spec(spec_path: str | Path) -> dict:
    return json.loads(Path(spec_path).read_text(encoding="utf-8"))


def build_scene_class(spec_path: str | Path):
    """scene.py（出力ディレクトリに保存する再現用ファイル）から使われる入口。

    manim CLI からも `manim render scene.py KbScene` で再現描画できるよう、
    output_format に応じた Scene クラスを 1 つ返す。
    """
    spec = load_spec(spec_path)
    module = import_module(TEMPLATES[spec["template"]])
    anim, static = module.make_scene_classes(spec)
    return static if spec.get("output_format") == "png" else anim


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
        if is_png:
            # 静的専用シーン + 最終フレーム保存（アニメ途中フレームに依存しない）
            conf.update({"save_last_frame": True, "format": "png", "pixel_width": 1920, "pixel_height": 1080})
            scene_cls = static_cls
        else:
            conf.update({"quality": "medium_quality"})
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
