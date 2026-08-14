-- Agent Room V0 schema. Applied idempotently at startup (see db/database.py).
-- Deterministic initialization: every statement is CREATE ... IF NOT EXISTS.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id          TEXT PRIMARY KEY,
    join_code   TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    objective   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | expired | closed
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    -- guardrail bookkeeping (see services/guardrails.py)
    agent_turns_used    INTEGER NOT NULL DEFAULT 0,
    last_speaker_id     TEXT,
    consecutive_turns   INTEGER NOT NULL DEFAULT 0,
    autonomy_enabled    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS agents (
    id                  TEXT PRIMARY KEY,
    room_id             TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    owner_name          TEXT NOT NULL,
    agent_name          TEXT NOT NULL,
    provider            TEXT NOT NULL,            -- openai | claude-code | human | other
    public_objective    TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'active',  -- active | left
    autonomous          INTEGER NOT NULL DEFAULT 0,      -- server-driven agent loop?
    joined_at           TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    -- opaque token the agent presents to act as itself
    token               TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_agents_room ON agents(room_id);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id             TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    agent_id            TEXT REFERENCES agents(id) ON DELETE SET NULL,  -- NULL => system/human
    sender_label        TEXT NOT NULL,
    recipient_agent_id  TEXT REFERENCES agents(id) ON DELETE SET NULL,  -- NULL => whole room
    content             TEXT NOT NULL,
    message_type        TEXT NOT NULL DEFAULT 'chat',
    -- chat | system | human | memory_update | task_update | ask_human | join | leave
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id, id);

CREATE TABLE IF NOT EXISTS shared_memory (
    room_id     TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
    data        TEXT NOT NULL,          -- JSON blob, see models.SharedMemoryData
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    room_id             TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    assigned_agent_id   TEXT REFERENCES agents(id) ON DELETE SET NULL,
    status              TEXT NOT NULL DEFAULT 'open',  -- open | claimed | done | cancelled
    result              TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_room ON tasks(room_id);
