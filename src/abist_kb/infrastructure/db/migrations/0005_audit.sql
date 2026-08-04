-- audit_runs / audit_findings(設計書 §9.2, M7 task-2)。
--
-- 個々の破壊的操作を1件ずつ記録する `audit_events`(0003)とは別物で、こちらは
-- 品質監査4種(verify-integrity / find-duplicates / check-contradictions /
-- backfill-metadata)の実行履歴と指摘事項を保持する。監査は(backfill-metadata の
-- 書込を除き)文書を変更しない。

CREATE TABLE IF NOT EXISTS audit_runs (
    id TEXT PRIMARY KEY,
    audit_type TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'report',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    params TEXT,
    totals TEXT,
    report_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_runs_type ON audit_runs (audit_type, started_at);

CREATE TABLE IF NOT EXISTS audit_findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES audit_runs (id),
    finding_type TEXT NOT NULL,
    path TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_findings_run ON audit_findings (run_id, finding_type);
