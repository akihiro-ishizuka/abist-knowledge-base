"""`BatchService`: CRUD、旧 `batch-config.js` からの一方向インポート、`run`。

バッチは app.sqlite が正であり、旧ファイルへは書き戻さない
(`migration.batch_config_parser` は読み取り専用の一方向 import)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.batch_service import BatchService
from abist_kb.config import Settings
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.schema import open_app_db

_SAMPLE_BATCH_CONFIG_JS = """#!/usr/bin/env node

export const batchConfigs = {
  '蛇腹形状の自動設計': [
    '設計効率化/三桜工業様/蛇腹形状の自動設計',
    '議事録/設計効率化/三桜工業様定例'
  ],
  'catiadoc': {
    'type': 'web',
    'url': 'http://catiadoc.free.fr/online/interfaces/CAAHomeIdx.htm',
    'outputDir': 'docs/catiadoc',
    'maxDepth': 10,
    'delay': 1000
  },
  'catia-flotherm-prep': {
    'type': 'git',
    'repository': 'https://github.com/abist-co-ltd/catia-flotherm-prep',
    'branch': 'main',
    'outputDir': 'docs/catia-flotherm-prep'
  }
};
"""


@pytest.fixture
def service(tmp_root: Path) -> BatchService:
    settings = Settings(root_dir=tmp_root, _env_file=None)
    settings.ensure_directories()
    conn = open_app_db(settings.app_db_path)
    return BatchService(conn, settings=settings)


def test_add_and_show(service: BatchService) -> None:
    created = service.add(name="b", type="esa", output_dir="docs/b", items=[{"target": "cat1"}])
    fetched = service.show(created["id"])
    assert fetched["name"] == "b"
    assert fetched["items"][0]["target"] == "cat1"


def test_show_raises_not_found(service: BatchService) -> None:
    with pytest.raises(AppError) as excinfo:
        service.show("missing")
    assert excinfo.value.code == ErrorCode.NOT_FOUND


def test_edit_replaces_items(service: BatchService) -> None:
    created = service.add(name="b", type="esa", output_dir="docs/b", items=[{"target": "cat1"}])
    service.edit(created["id"], items=[{"target": "cat1"}, {"target": "cat2"}])
    fetched = service.show(created["id"])
    assert len(fetched["items"]) == 2


def test_remove_requires_confirmation_and_records_audit(service: BatchService) -> None:
    created = service.add(name="b", type="esa", output_dir="docs/b", items=[])
    assert service.remove(created["id"], confirm=lambda _p: False) is False
    assert service.remove(created["id"], confirm=lambda _p: True, actor="tester") is True
    with pytest.raises(AppError):
        service.show(created["id"])


def test_import_from_old_config_creates_batches(service: BatchService, tmp_path: Path) -> None:
    config_path = tmp_path / "batch-config.js"
    config_path.write_text(_SAMPLE_BATCH_CONFIG_JS, encoding="utf-8")

    result = service.import_from_old_config(config_path)

    assert result["imported"] == 2
    names = {b["name"] for b in service.list()}
    assert names == {"蛇腹形状の自動設計", "catiadoc"}

    esa_batch = service.show(service.list_by_name("蛇腹形状の自動設計")["id"])
    assert [item["target"] for item in esa_batch["items"]] == [
        "設計効率化/三桜工業様/蛇腹形状の自動設計",
        "議事録/設計効率化/三桜工業様定例",
    ]
    assert esa_batch["type"] == "esa"

    web_batch = service.show(service.list_by_name("catiadoc")["id"])
    assert web_batch["type"] == "web"
    assert web_batch["output_dir"] == "docs/catiadoc"


def test_import_from_old_config_does_not_write_to_old_file(
    service: BatchService, tmp_path: Path
) -> None:
    config_path = tmp_path / "batch-config.js"
    original = _SAMPLE_BATCH_CONFIG_JS
    config_path.write_text(original, encoding="utf-8")

    service.import_from_old_config(config_path)

    assert config_path.read_text(encoding="utf-8") == original


def test_import_from_old_config_rejects_unsupported_syntax(
    service: BatchService, tmp_path: Path
) -> None:
    config_path = tmp_path / "batch-config.js"
    config_path.write_text("export const batchConfigs = someFunctionCall();\n", encoding="utf-8")
    with pytest.raises(AppError) as excinfo:
        service.import_from_old_config(config_path)
    assert excinfo.value.code == ErrorCode.UNSUPPORTED_BATCH_CONFIG


def test_run_submits_job_via_job_service(service: BatchService) -> None:
    # オフラインで完了する空の web バッチ(アイテム無し → SyncService が即成功)。
    created = service.add(name="b", type="web", output_dir="docs/b", items=[])
    job = service.run(created["id"])
    assert job["kind"] == "batch"
    assert job["params"]["batch_id"] == created["id"]
    assert job["state"] == "succeeded"


def test_run_raises_not_found_for_missing_batch(service: BatchService) -> None:
    with pytest.raises(AppError) as excinfo:
        service.run("missing")
    assert excinfo.value.code == ErrorCode.NOT_FOUND
