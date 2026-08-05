from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from abist_kb.config import Settings
from abist_kb.presentation.common.container import ServiceContainer


@pytest.fixture
def container(tmp_root: Path) -> Iterator[ServiceContainer]:
    settings = Settings(root_dir=tmp_root, _env_file=None)
    settings.ensure_directories()
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    # `check_same_thread=False`: FastAPI `TestClient` の ASGI portal が
    # 接続作成スレッドとは別スレッドでリクエストを処理するため。
    cont = ServiceContainer(settings, check_same_thread=False)
    try:
        yield cont
    finally:
        cont.close()
