CREATE TABLE IF NOT EXISTS feedback_entries (
    id TEXT PRIMARY KEY,
    author TEXT NOT NULL CHECK (author IN ('hermes', 'codex', 'claude')),
    kind TEXT NOT NULL CHECK (kind IN ('bug', 'improvement', 'info')),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT NOT NULL CHECK (length(description) BETWEEN 0 AND 16000),
    project TEXT NOT NULL CHECK (
        length(project) BETWEEN 1 AND 100
        AND project NOT GLOB '*[^a-z0-9-]*'
        AND substr(project, 1, 1) GLOB '[a-z0-9]'
        AND substr(project, -1, 1) GLOB '[a-z0-9]'
    ),
    severity TEXT NOT NULL CHECK (severity IN ('low', 'normal', 'high', 'urgent')),
    status TEXT NOT NULL CHECK (status IN ('open', 'triaged', 'planned', 'resolved', 'dismissed')),
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    client_request_id TEXT CHECK (client_request_id IS NULL OR length(client_request_id) BETWEEN 1 AND 200),
    payload_json TEXT,
    payload_hash TEXT,
    CHECK (
        (client_request_id IS NULL AND payload_json IS NULL AND payload_hash IS NULL)
        OR
        (client_request_id IS NOT NULL AND payload_json IS NOT NULL AND length(payload_hash) = 64)
    ),
    UNIQUE (author, client_request_id)
);

CREATE INDEX IF NOT EXISTS feedback_entries_page_idx
    ON feedback_entries (created_at, id);
CREATE INDEX IF NOT EXISTS feedback_entries_project_idx
    ON feedback_entries (project, created_at, id);
CREATE INDEX IF NOT EXISTS feedback_entries_kind_idx
    ON feedback_entries (kind, created_at, id);
CREATE INDEX IF NOT EXISTS feedback_entries_severity_idx
    ON feedback_entries (severity, created_at, id);
CREATE INDEX IF NOT EXISTS feedback_entries_status_idx
    ON feedback_entries (status, created_at, id);
CREATE INDEX IF NOT EXISTS feedback_entries_author_idx
    ON feedback_entries (author, created_at, id);

CREATE TABLE IF NOT EXISTS feedback_history (
    feedback_id TEXT NOT NULL REFERENCES feedback_entries(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    actor TEXT NOT NULL CHECK (actor IN ('hermes', 'codex', 'claude')),
    status TEXT NOT NULL CHECK (status IN ('open', 'triaged', 'planned', 'resolved', 'dismissed')),
    note TEXT NOT NULL CHECK (length(note) BETWEEN 0 AND 16000),
    created_at TEXT NOT NULL,
    PRIMARY KEY (feedback_id, revision)
);

CREATE INDEX IF NOT EXISTS feedback_history_created_idx
    ON feedback_history (feedback_id, created_at, revision);

CREATE TRIGGER IF NOT EXISTS feedback_history_no_update
BEFORE UPDATE ON feedback_history
BEGIN
    SELECT RAISE(ABORT, 'feedback history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS feedback_history_no_delete
BEFORE DELETE ON feedback_history
BEGIN
    SELECT RAISE(ABORT, 'feedback history is immutable');
END;
