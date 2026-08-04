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
    # `check_same_thread=False`: `tests/ui/test_api.py` drives this fixture through
    # FastAPI's `TestClient`, whose ASGI portal runs requests on a worker thread
    # different from the one that created this fixture's connection.
    cont = ServiceContainer(settings, check_same_thread=False)
    try:
        yield cont
    finally:
        cont.close()
