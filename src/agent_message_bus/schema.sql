PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    sender TEXT NOT NULL CHECK (sender IN ('hermes', 'codex', 'claude')),
    recipient TEXT NOT NULL CHECK (recipient IN ('hermes', 'codex', 'claude')),
    message_type TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    body_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'claimed', 'acknowledged', 'rejected')),
    correlation_id TEXT,
    in_reply_to TEXT,
    hop_count INTEGER NOT NULL DEFAULT 0 CHECK (hop_count BETWEEN 0 AND 6),
    requires_user_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_user_approval IN (0, 1)),
    created_at TEXT NOT NULL,
    claimed_by TEXT,
    claim_expires_at TEXT,
    acknowledged_at TEXT,
    rejected_at TEXT,
    rejection_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_status_created ON messages (recipient, status, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_claim_expiry ON messages (status, claim_expires_at);
CREATE INDEX IF NOT EXISTS idx_messages_retention ON messages (status, acknowledged_at, rejected_at);
CREATE INDEX IF NOT EXISTS idx_messages_in_reply_to ON messages (in_reply_to);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL CHECK (size >= 0),
    sha256 TEXT NOT NULL,
    content_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_updated_at ON files (updated_at);

CREATE TABLE IF NOT EXISTS knowledge (
    project TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project, path)
);
