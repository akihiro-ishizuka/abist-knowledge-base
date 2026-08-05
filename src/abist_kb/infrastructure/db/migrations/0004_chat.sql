-- conversations / messages / citations(設計書 §7.1, §9.2)。
--
-- 旧 chat-server.js は会話履歴をクライアント側(ブラウザ)に持っており、
-- リロードや別クライアント間での共有ができなかった。
-- ここではサーバー側 DB に持つ(M7 task-1 の設計判断)。
--
-- citations は ChatService が `range_hash`(domain/line_range.py, get_document と
-- 同じ実装)で検証した結果を保持する。`valid=0` の引用も削除せず記録し、
-- 「検証に失敗した引用を黙って落とさない」という要件を満たす
-- (メッセージ側にも `citation_warnings` として提示する)。

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations (id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS citations (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages (id),
    path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    range_hash TEXT,
    valid INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_citations_message ON citations (message_id);
