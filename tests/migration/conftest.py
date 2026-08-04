"""M8 移行テスト共通の縮小 fixture(旧システムの実スキーマを模した合成データ)。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_BATCH_CONFIG_JS = """#!/usr/bin/env node

export const batchConfigs = {
  'catia-flotherm-prep': {
    'type': 'git',
    'repository': 'https://github.com/abist-co-ltd/catia-flotherm-prep',
    'branch': 'main',
    'outputDir': 'docs/catia-flotherm-prep'
  },
  'C#ATIA': {
    'type': 'web',
    'url': 'http://example.invalid/catia',
    'outputDir': 'C#ATIA',
    'maxDepth': 3,
    'delay': 500
  }
};
"""


@pytest.fixture
def old_repo(tmp_path: Path) -> Path:
    """§4 の実測事実を再現した縮小 fixture。

    - docs/a.md: sync-state.sqlite に登録されている通常文書。
    - docs/knowledge/catiadoc/b.md: どちらの旧DBにも未登録の孤児(§4 の再現)。
    - C#ATIA/outside.md: docs/ の外にある web バッチ出力(§4 の再現)。
    - docs/broken.md: front matter の閉じ区切りが無い壊れたファイル。
    - docs/mojibake.md: UTF-8 として読めないバイト列。
    """
    root = tmp_path / "old-repo"
    docs = root / "docs"
    (docs / "knowledge" / "catiadoc").mkdir(parents=True)
    (root / "C#ATIA").mkdir(parents=True)
    (root / "data").mkdir(parents=True)

    (docs / "a.md").write_text("---\ntitle: A\n---\n本文A\n", encoding="utf-8")
    (docs / "knowledge" / "catiadoc" / "b.md").write_text(
        "---\nsource: web\n---\n本文B(孤児)\n", encoding="utf-8"
    )
    (root / "C#ATIA" / "outside.md").write_text(
        "---\ntitle: Outside\n---\ndocs外の本文\n", encoding="utf-8"
    )
    (docs / "broken.md").write_text("---\ntitle: broken\n本文(閉じ区切り無し)\n", encoding="utf-8")
    (docs / "mojibake.md").write_bytes(b"---\ntitle: mojibake\n---\n\xff\xfe\x00broken bytes\n")

    # 実データ確認済み: batch-config.js はリポジトリ**直下**に置かれる
    # (`data/` 配下ではない。旧実装 `tools/lib/batch-config-store.js` とその
    # テストで確認済み)。
    (root / "batch-config.js").write_text(_BATCH_CONFIG_JS, encoding="utf-8")

    sync_db = root / "data" / "sync-state.sqlite"
    conn = sqlite3.connect(sync_db)
    conn.execute("CREATE TABLE meta (schema_version INTEGER)")
    conn.execute("INSERT INTO meta VALUES (1)")
    conn.execute(
        """
        CREATE TABLE documents (
            path TEXT PRIMARY KEY,
            uuid TEXT,
            source TEXT,
            document_type TEXT,
            status TEXT,
            title TEXT,
            sync_status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "docs/a.md",
            "11111111-1111-1111-1111-111111111111",
            "git",
            "work",
            "active",
            "A",
            "synced",
            "2026-07-29T07:09:56Z",
            "2026-07-29T07:09:56Z",
        ),
    )
    conn.commit()
    conn.close()

    return root
