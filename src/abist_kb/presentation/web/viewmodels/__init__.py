"""Web/TUI 共通の view-model 層(設計書 §5, §7.1)。

`ServiceContainer`(`container.py`)が `application/` サービスを束ね、
`screens.py` の関数群がそれを9画面向けの JSON 互換 dict へ変換する。
色・記号(セマンティックトークン)の割当は `tokens.py` に集約し、Rich style /
web hex の実体は `presentation/console/theme.py::TOKEN_STYLES` を1箇所だけ
参照する。将来の Textual TUI は `screens.py` をそのまま import して
再実装を避ける(design/plans/M6-M10-remaining.md M6 節)。
"""

from __future__ import annotations

from .container import ServiceContainer, build_container

__all__ = ["ServiceContainer", "build_container"]
