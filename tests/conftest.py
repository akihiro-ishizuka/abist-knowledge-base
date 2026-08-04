from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from abist_kb import identity


@pytest.fixture(autouse=True)
def _clear_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト間で本アプリの環境変数が漏れないようにする。"""
    for key in list(os.environ):
        if key.startswith(identity.ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)


@pytest.fixture
def tmp_root(tmp_path: Path) -> Iterator[Path]:
    """日本語を含む一時ルートディレクトリ(Windows の日本語パス検証を兼ねる)。"""
    root = tmp_path / "作業ルート"
    root.mkdir()
    yield root
