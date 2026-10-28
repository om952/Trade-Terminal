-- Initial app-state schema.
--
-- Every table carries user_id, always 'local' in the local app, so a hosted
-- multi-user version is a data migration rather than a redesign.
-- Timestamps are ISO 8601 strings in IST. JSON columns hold JSON text.

CREATE TABLE strategies (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'local',
    -- Versions that are variations of one idea share a family, which is what
    -- the trial counter counts against.
    family_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('llm', 'builder', 'template', 'import')),
    head_hash   TEXT NOT NULL,
    read_only   INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_strategies_user ON strategies (user_id, archived, updated_at);

CREATE TABLE strategy_versions (
    strategy_id     TEXT NOT NULL REFERENCES strategies (id) ON DELETE CASCADE,
    hash            TEXT NOT NULL,
    user_id         TEXT NOT NULL DEFAULT 'local',
    parent_hash     TEXT,
    schema_version  INTEGER NOT NULL,
    spec_json       TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('llm', 'builder', 'template', 'import')),
    -- Provenance when the version came from the language model.
    model           TEXT,
    prompt_version  TEXT,
    conversation_id TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (strategy_id, hash)
);
CREATE INDEX idx_versions_hash ON strategy_versions (hash);

CREATE TABLE runs (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL DEFAULT 'local',
    strategy_id       TEXT REFERENCES strategies (id) ON DELETE SET NULL,
    strategy_hash     TEXT NOT NULL,
    family_id         TEXT,
    kind              TEXT NOT NULL CHECK (kind IN ('backtest', 'paper', 'replay', 'live')),
    status            TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    params_json       TEXT NOT NULL DEFAULT '{}',
    data_fingerprint  TEXT,
    metrics_json      TEXT,
    result_path       TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);
CREATE INDEX idx_runs_strategy ON runs (user_id, strategy_id, created_at);
CREATE INDEX idx_runs_family ON runs (user_id, family_id, kind);

CREATE TABLE jobs (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL DEFAULT 'local',
    kind              TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    progress          REAL NOT NULL DEFAULT 0,
    message           TEXT NOT NULL DEFAULT '',
    eta_seconds       REAL,
    params_json       TEXT NOT NULL DEFAULT '{}',
    result_json       TEXT,
    error             TEXT,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);
CREATE INDEX idx_jobs_user ON jobs (user_id, status, created_at);

CREATE TABLE app_sessions (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL DEFAULT 'local',
    started_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    ended_at       TEXT,
    end_cause      TEXT CHECK (end_cause IN ('quit', 'idle', 'shutdown', 'crash')),
    scratch_bytes  INTEGER NOT NULL DEFAULT 0,
    outcome        TEXT CHECK (outcome IN ('kept', 'discarded', 'pending'))
);

CREATE TABLE preferences (
    user_id     TEXT NOT NULL DEFAULT 'local',
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE conversations (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL DEFAULT 'local',
    title        TEXT NOT NULL DEFAULT '',
    strategy_id  TEXT REFERENCES strategies (id) ON DELETE SET NULL,
    archived     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_conversations_user ON conversations (user_id, archived, updated_at);

CREATE TABLE messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    user_id          TEXT NOT NULL DEFAULT 'local',
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'event')),
    content          TEXT NOT NULL,
    payload_json     TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_messages_conversation ON messages (conversation_id, id);
