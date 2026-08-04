-- ジョブ基盤(設計書 §9.2, §10)。
--
-- jobs: 永続ジョブ本体。owner_id/resource_key/heartbeat_at/lease_expires_at は
-- そのジョブを claim したワーカーの生存確認に使う(§10.3 の異常終了検知)。
-- クラッシュ復旧はこの4列と resource_leases だけで判定でき、worker_leases の
-- 現在のリーダーが誰であるかには依存しない(リーダー交代後も判定できるように)。
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    params TEXT NOT NULL,
    result TEXT,
    error TEXT,
    progress TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    retry_of TEXT REFERENCES jobs (id),
    owner_id TEXT,
    resource_key TEXT,
    heartbeat_at TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_state_created_at ON jobs (state, created_at);

-- job_events: 追記専用の進捗履歴。(job_id, seq) が単調増加する一意なイベント順序。
CREATE TABLE IF NOT EXISTS job_events (
    job_id TEXT NOT NULL REFERENCES jobs (id),
    seq INTEGER NOT NULL,
    phase TEXT,
    current INTEGER,
    total INTEGER,
    message TEXT,
    severity TEXT NOT NULL,
    item TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);

-- worker_leases: リーダー選出用の単一行(id は常に 1 に固定)。所有者はプロセス
-- UUID。BEGIN IMMEDIATE の下で読み書きすることで、複数プロセスが同時に
-- 取得・更新を試みても DB 自体が調停する(§10.2)。
CREATE TABLE IF NOT EXISTS worker_leases (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner_id TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- resource_leases: リソース種別ごとの排他区画。`docs-write`/`render` は
-- resource_key がそのまま種別名、`corpus-write:<corpus>` はコーパスごとに
-- 独立した行を持つ(§10.2)。
CREATE TABLE IF NOT EXISTS resource_leases (
    resource_key TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    job_id TEXT REFERENCES jobs (id),
    expires_at TEXT NOT NULL
);
