from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from abist_kb.config import Settings
from abist_kb.presentation.web.viewmodels.container import ServiceContainer


@pytest.fixture
def container(tmp_root: Path) -> Iterator[ServiceContainer]:
    settings = Settings(root_dir=tmp_root, _env_file=None)
    settings.ensure_directories()
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    # `check_same_thread=False`: FastAPI `TestClient` 経由の API 契約テストでも
    # この fixture を共有するため(ASGI portal が別スレッドでリクエスト処理する)。
    cont = ServiceContainer(settings, check_same_thread=False)
    try:
        yield cont
    finally:
        cont.close()
