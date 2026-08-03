-- sources / batches / batch_items / documents / audit_events(設計書 §9.2, §12)。
--
-- documents は旧 `tools/lib/sync-state.js` の `documents` テーブルの27列を
-- 全て保持し、`uuid`(新規の安定識別子)と `source_id`(新設 sources への参照)を
-- 追加する(tests/fixtures/PROVENANCE.md §4 が §9.2 の記述を上書きする実測事実:
-- 旧 sync-state.sqlite は履歴的な同期台帳ではなく、defaultExclude で参照コーパスを
-- 除外した1回限りのバックフィル・スナップショットである。したがってこのテーブルの
-- 不在は「実ファイルが存在しない」ことの証拠にはならない)。
-- 索引(source/sync_status/status/source_key)は旧実装と同じ列・同じ意図を維持する。

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    connection TEXT NOT NULL DEFAULT '{}',
    output_dir TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_type ON sources (type);

-- batches: バッチ定義本体。`type`/`output_dir` は旧 batch-config.js の
-- エントリ形状(esa=カテゴリ配列、web/git=type付きオブジェクト)を引き継ぐ。
-- バッチは app.sqlite が正であり、旧 batch-config.js ファイルへは書き戻さない
-- (migration.batch_config_parser は一方向の import 専用)。
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    output_dir TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- batch_items: バッチ内の個別対象。順序(position)、ソース参照(source_id)、
-- 個別オプション(options JSON)を持つ。esa バッチは1カテゴリにつき1行、
-- web/git バッチは対象1件につき1行(target は使わず options に URL/repository 等を持つ)。
CREATE TABLE IF NOT EXISTS batch_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches (id),
    source_id TEXT REFERENCES sources (id),
    position INTEGER NOT NULL,
    target TEXT,
    options TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items (batch_id);

-- documents: 旧 sync-state.sqlite.documents の27列を全保持 + uuid + source_id。
CREATE TABLE IF NOT EXISTS documents (
    path TEXT PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    source_id TEXT REFERENCES sources (id),
    source TEXT,
    managed_by TEXT,
    document_type TEXT,
    status TEXT,
    title TEXT,
    url TEXT,
    post_number INTEGER,
    category TEXT,
    source_key TEXT,
    source_updated_at TEXT,
    sync_status TEXT NOT NULL DEFAULT 'synced',
    source_content_hash TEXT,
    local_content_hash TEXT,
    downloaded_at TEXT,
    last_checked_at TEXT,
    etag TEXT,
    last_modified TEXT,
    missing_count INTEGER NOT NULL DEFAULT 0,
    missing_since TEXT,
    sync_error TEXT,
    indexed_at TEXT,
    embedding_model TEXT,
    embedding_dimensions INTEGER,
    embedding_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents (source);
CREATE INDEX IF NOT EXISTS idx_documents_sync_status ON documents (sync_status);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents (status);
CREATE INDEX IF NOT EXISTS idx_documents_source_key ON documents (source_key);
CREATE INDEX IF NOT EXISTS idx_documents_source_id ON documents (source_id);

-- audit_events: 破壊的操作(文書削除・バッチ削除等)の監査証跡(設計書 §12)。
-- M7 の `audit_runs`/`audit_findings`(監査スキャンの実行履歴・指摘)とは別物で、
-- こちらは個々の破壊的操作イベントを1件ずつ追記する台帳である。
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    details TEXT,
    actor TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_events_target ON audit_events (target_type, target_id);
