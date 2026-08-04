"""NiceGUI Python-level fixture: PARTIAL ジョブが警告バッジ(記号+ラベル)で
表示され、成功バッジと混同されないことを確認する(§6.1、M3 回帰ガード)。

`tests/ui/test_web_pages.py` のスモークテストを補完し、`nicegui.testing.User`
経由でダッシュボード画面の実際の描画結果(DOM 上のテキスト)を検査する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nicegui.testing import User

from abist_kb.config import Settings
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.web.app import register_pages
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

pytest_plugins = ["nicegui.testing.plugin"]


@pytest.fixture
def wired_container(tmp_path: Path) -> ServiceContainer:
    settings = Settings(root_dir=tmp_path / "root", _env_file=None)
    settings.ensure_directories()
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    cont = ServiceContainer(settings, check_same_thread=False)
    register_pages(cont)
    return cont


async def test_dashboard_shows_partial_job_as_warning_badge(
    user: User, wired_container: ServiceContainer
) -> None:
    repo = JobRepository(wired_container.conn)
    job = repo.submit("batch", {"batch_id": "release-notes"})
    claimed = repo.claim("owner-1", ttl_seconds=30)
    assert claimed is not None
    repo.finish(job.id, state=JobState.PARTIAL, result={"failed_batches": 2})

    await user.open("/")
    await user.should_see("警告")
    # 記号+ラベル(badge_label)がバッジ文字列に含まれる。
    await user.should_see("! partial")
