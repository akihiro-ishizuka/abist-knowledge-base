#!/usr/bin/env python3
"""可視化の依存診断: python / manim / ffmpeg / 日本語フォント

stdout に JSON を 1 行出力する（Node の checkVisualizeDeps がパースする）。
依存が欠けていても診断自体は成功（exit 0）。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys


def main() -> int:
    result: dict = {
        "python": {"found": True, "version": platform.python_version(), "path": sys.executable},
    }

    try:
        import manim

        result["manim"] = {"found": True, "version": manim.__version__}
    except Exception as e:
        result["manim"] = {"found": False, "error": str(e)}

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        try:
            head = subprocess.run(
                ["ffmpeg", "-version"], capture_output=True, text=True, timeout=15
            ).stdout.splitlines()[0]
        except Exception:
            head = None
        result["ffmpeg"] = {"found": True, "version": head, "path": ffmpeg_path}
    else:
        result["ffmpeg"] = {"found": False}

    requested_font = os.environ.get("KB_VISUALIZE_FONT", "Yu Gothic UI")
    try:
        import manimpango

        fonts = set(manimpango.list_fonts())
        result["fonts"] = {"requested": requested_font, "available": requested_font in fonts}
    except Exception:
        result["fonts"] = {"requested": requested_font, "available": None}

    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
