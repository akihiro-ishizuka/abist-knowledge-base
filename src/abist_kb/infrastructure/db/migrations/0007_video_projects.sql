-- video_projects（社内動画生成のカタログ）。
--
-- **正本は reports/videos/<id>/project-spec.json + manifest.json であり、
-- このテーブルは一覧・検索・状態照会のための索引に徹する。**
-- purring の visualizations と同じ二重管理回避の原則に従う
-- （application/video/catalog.py が唯一の書き手。DB を消しても
--   reconcile_from_disk() でディスクから完全に再構築できる）。

CREATE TABLE IF NOT EXISTS video_projects (
    id                TEXT PRIMARY KEY,              -- <UTC-ts>-<slug>-<4hex>
    -- ジョブは運用ログ、成果物はディスク上の実体で寿命が違うので SET NULL。
    job_id            TEXT REFERENCES jobs (id) ON DELETE SET NULL,
    state             TEXT NOT NULL,                 -- draft|rendering|succeeded|failed|cancelled
    code              TEXT,                          -- 失敗時のコード
    schema_version    TEXT NOT NULL,
    title             TEXT NOT NULL,
    purpose           TEXT,
    language          TEXT,
    aspect_ratio      TEXT,
    classification    TEXT,                          -- distribution.classification
    public_candidate  INTEGER NOT NULL DEFAULT 0,
    project_dir       TEXT NOT NULL,                 -- root_dir からの相対 posix パス
    spec_path         TEXT,
    spec_sha256       TEXT,                          -- ドリフト検知用（本文は持たない）
    output_path       TEXT,
    output_sha256     TEXT,
    output_size_bytes INTEGER,
    duration_sec      REAL,
    scene_count       INTEGER,
    warnings          TEXT,                          -- JSON 配列
    source            TEXT NOT NULL DEFAULT 'render',-- 'render' | 'disk'（取り込み）
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_video_projects_created_at ON video_projects (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_video_projects_state ON video_projects (state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_video_projects_job ON video_projects (job_id);

-- どの Markdown を題材にした動画かの逆引き。selection / require_usage を持つので
-- 「明示指定だったのか、ディレクトリからの選抜だったのか」が後から分かる。
CREATE TABLE IF NOT EXISTS video_project_inputs (
    video_id      TEXT NOT NULL REFERENCES video_projects (id) ON DELETE CASCADE,
    path          TEXT NOT NULL,                     -- docs/ 相対
    content_hash  TEXT,
    selection     TEXT NOT NULL,                     -- explicit_primary|collection_candidate|supplemental
    require_usage INTEGER NOT NULL DEFAULT 0,
    origin_type   TEXT NOT NULL,                     -- kb_path|kb_directory|kb_query|esa_post
    origin_selector TEXT,                            -- ディレクトリ／クエリ
    esa_url       TEXT,
    esa_post_id   INTEGER,
    used          INTEGER,                           -- 本編で使われたか（QA が埋める）
    PRIMARY KEY (video_id, path)
);

CREATE INDEX IF NOT EXISTS idx_video_project_inputs_path ON video_project_inputs (path);
CREATE INDEX IF NOT EXISTS idx_video_project_inputs_selector
    ON video_project_inputs (video_id, origin_selector);
