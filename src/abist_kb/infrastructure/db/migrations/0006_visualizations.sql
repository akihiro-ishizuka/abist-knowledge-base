-- visualizations / visualization_sources（設計書 §9.2 の
-- 「SceneSpec、状態、成果物manifest、出典検証結果」）。
--
-- **正本は reports/visualizations/<id>/manifest.json であり、この2テーブルは
-- 一覧・検索・状態照会のための索引に徹する。** manifest 本文は複製せず
-- manifest_sha256 でドリフトのみ検知する（application/visualization/catalog.py 参照）。
-- DB を消してもディスクから完全に再構築できる状態を保つこと。

CREATE TABLE IF NOT EXISTS visualizations (
    id                TEXT PRIMARY KEY,              -- <UTC-ts>-<slug>-<4hex>
    -- ON DELETE SET NULL: ジョブ行が整理されても成果物の記録は残す
    -- (ジョブは運用ログ、成果物はディスク上の実体なので寿命が違う)。
    job_id            TEXT REFERENCES jobs (id) ON DELETE SET NULL,
    state             TEXT NOT NULL,                 -- 'succeeded' | 'failed'
    code              TEXT,                          -- 失敗時の RENDER_* / SOURCE_* コード
    schema_version    TEXT NOT NULL,
    scene_kind        TEXT NOT NULL,
    template          TEXT NOT NULL,
    output_format     TEXT NOT NULL,                 -- mp4 | png
    title             TEXT NOT NULL,
    query             TEXT,                          -- カタログの主要な検索キー
    output_dir        TEXT NOT NULL,                 -- root_dir からの相対 posix パス
    manifest_path     TEXT,                          -- 同上
    manifest_sha256   TEXT,                          -- ドリフト検知用。本文は持たない
    output_path       TEXT,                          -- 'output.mp4' | 'output.png'
    output_sha256     TEXT,
    output_size_bytes INTEGER,
    duration_ms       REAL,
    warnings          TEXT,                          -- JSON 配列
    source            TEXT NOT NULL DEFAULT 'render',-- 'render' | 'disk'(取り込み)
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_visualizations_created_at ON visualizations (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_visualizations_state ON visualizations (state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_visualizations_job ON visualizations (job_id);

-- 出典検証結果。source_verifier が算出する source_status の唯一の保存先であり、
-- 「どの文書を引用した図解か」の逆引きにも使う。
CREATE TABLE IF NOT EXISTS visualization_sources (
    visualization_id TEXT NOT NULL REFERENCES visualizations (id) ON DELETE CASCADE,
    source_id        TEXT NOT NULL,
    path             TEXT NOT NULL,                  -- docs/ 相対
    start_line       INTEGER,
    end_line         INTEGER,
    content_hash     TEXT,
    status           TEXT NOT NULL,                  -- ok | not_found | hash_mismatch | unknown
    PRIMARY KEY (visualization_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_visualization_sources_path ON visualization_sources (path);
