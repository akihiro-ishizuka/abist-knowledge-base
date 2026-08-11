"""可視化テスト共通の設定。

`tools/visualize/templates/layout.py` は manim 非依存の純関数モジュールで、
主 venv(Python 3.12)からそのまま import できる。テンプレート本体(manim 必須)は
ここでは import しない。sys.path 操作をこのファイルに寄せることで、各テストが
モジュールレベルで `sys.path.insert` する必要がなくなる(ruff E402 回避)。
"""

from __future__ import annotations

import sys
from pathlib import Path

_VISUALIZE_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "visualize"
if str(_VISUALIZE_TOOLS) not in sys.path:
    sys.path.insert(0, str(_VISUALIZE_TOOLS))
