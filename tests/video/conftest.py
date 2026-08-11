"""動画テスト共通の設定。

`tools/visualize/templates/layout.py` は manim 非依存の純関数モジュールで、
主 venv からそのまま import できる（フレームプリセットの正本がここにある）。
sys.path 操作をこのファイルへ寄せて、各テストの E402 を避ける。
"""

from __future__ import annotations

import sys
from pathlib import Path

_VISUALIZE_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "visualize"
if str(_VISUALIZE_TOOLS) not in sys.path:
    sys.path.insert(0, str(_VISUALIZE_TOOLS))
