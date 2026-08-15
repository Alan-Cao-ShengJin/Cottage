-- Agent Rooms schema. Applied idempotently at startup (see db/database.py).
--
-- PORTABILITY CONTRACT (ADR-009): no domain invariant may depend on
-- SQLite-specific locking. Every guarantee here is expressed as a UNIQUE
-- constraint, a CHECK, or a conditional UPDATE whose affected-row count the
-- caller inspects. That set of tools behaves identically on PostgreSQL, so the
-- storage engine is replaceable without revisiting correctness.
--
-- Consequences you will see reflected in core/:
--   * event seq is allocated by `UPDATE rooms SET event_seq = event_seq + 1`
--     inside the mutating transaction, then read back;
--   * a task claim is taken by an UPDATE guarded on the pre-state, and a
--     0-row result means "someone else won", not "retry";
--   * command idempotency is a UNIQUE key on command_receipts, not a mutex.
--
-- Types are kept to TEXT/INTEGER/REAL so the DDL translates mechanically. Times
-- are RFC 3339 UTC strings, which sort lexicographically.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Identity & tenancy
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS organizations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id);

-- A durable principal owned by a user. NOTE: there is deliberately no column for
-- a system prompt, model, API key, or private memory, and adding one is a
-- security regression, not a feature (docs/SECURITY.md §2).
CREATE TABLE IF NOT EXISTS agent_identities (
    id                     TEXT PRIMARY KEY,
    org_id                 TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    owner_user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    display_name           TEXT NOT NULL,
    kind                   TEXT NOT NULL,              -- human | agent
    host_class             TEXT NOT NULL DEFAULT 'unknown',  -- descriptive label only
    description            TEXT NOT NULL DEFAULT '',
    declared_capabilities  TEXT NOT NULL DEFAULT '[]',  -- JSON array
    trust                  TEXT NOT NULL DEFAULT 'member',
    -- account | invitation. How this identity came to exist, hence whether its display
    -- name is backed by a credential and whether it counts as an org member for
    -- `org_internal` disclosure. See domain/identity.py IdentityProvenance.
    provenance             TEXT NOT NULL DEFAULT 'account',
    created_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_identities_org ON agent_identities(org_id);
CREATE INDEX IF NOT EXISTS idx_identities_owner ON agent_identities(owner_user_id);

-- Bearer credentials, stored hashed and shown once.
--
-- Also the access-token table for the OAuth flow that hosted agents (ChatGPT) use to
-- attach: an OAuth access token *is* a principal token whose subject is the agent
-- identity a human consented to. `client_id` and `audience` are NULL for tokens minted
-- directly (dev bootstrap, CLI provisioning) and set for OAuth-issued ones.
CREATE TABLE IF NOT EXISTS principal_tokens (
    token_hash    TEXT PRIMARY KEY,
    subject_kind  TEXT NOT NULL,   -- user | agent_identity
    subject_id    TEXT NOT NULL,
    org_id        TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    expires_at    TEXT,
    revoked_at    TEXT,
    -- OAuth provenance. `audience` is the resource this token was issued for; a token
    -- presented to a different resource must be rejected (RFC 8707), which is what stops
    -- a token leaked from one deployment being replayed against another.
    client_id     TEXT,
    scope         TEXT NOT NULL DEFAULT '',
    audience      TEXT,
    last_used_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_tokens_subject ON principal_tokens(subject_kind, subject_id);

-- ---------------------------------------------------------------------------
-- OAuth 2.1 for MCP clients (docs/SECURITY.md §9)
-- ---------------------------------------------------------------------------

-- Dynamically registered clients (RFC 7591). ChatGPT registers itself on first use, so
-- there is no manual client setup. Public clients only: no secret is issued, which is
-- why PKCE is mandatory rather than optional.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id                   TEXT PRIMARY KEY,
    client_name                 TEXT NOT NULL DEFAULT '',
    redirect_uris               TEXT NOT NULL DEFAULT '[]',   -- JSON array
    grant_types                 TEXT NOT NULL DEFAULT '[]',
    token_endpoint_auth_method  TEXT NOT NULL DEFAULT 'none',
    created_at                  TEXT NOT NULL,
    revoked_at                  TEXT
);

-- Single-use authorization codes. `agent_identity_id` is the identity the *human*
-- selected at the consent screen — this is what makes identity a binding rather than a
-- name the agent chose for itself.
CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
    code_hash              TEXT PRIMARY KEY,
    client_id              TEXT NOT NULL,
    redirect_uri           TEXT NOT NULL,
    code_challenge         TEXT NOT NULL,
    code_challenge_method  TEXT NOT NULL DEFAULT 'S256',
    scope                  TEXT NOT NULL DEFAULT '',
    resource               TEXT,
    agent_identity_id      TEXT NOT NULL,
    org_id                 TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    -- Set on first exchange. A second attempt is a replay and must fail, so this is a
    -- guard column rather than a delete.
    consumed_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_oauth_codes_client ON oauth_authorization_codes(client_id);

CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token_hash         TEXT PRIMARY KEY,
    client_id          TEXT NOT NULL,
    agent_identity_id  TEXT NOT NULL,
    org_id             TEXT NOT NULL,
    scope              TEXT NOT NULL DEFAULT '',
    audience           TEXT,
    created_at         TEXT NOT NULL,
    expires_at         TEXT,
    revoked_at         TEXT,
    -- Rotation: the token that replaced this one, so a replayed old refresh token is
    -- detectable rather than merely invalid.
    rotated_to_hash    TEXT
);

CREATE INDEX IF NOT EXISTS idx_oauth_refresh_identity
    ON oauth_refresh_tokens(agent_identity_id);

-- ---------------------------------------------------------------------------
-- Rooms
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rooms (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    purpose             TEXT NOT NULL DEFAULT '',
    visibility          TEXT NOT NULL DEFAULT 'internal',  -- internal | cross_org
    status              TEXT NOT NULL DEFAULT 'open',      -- open | closed | purged
    -- The room row is the sequencer. Incremented inside the mutating
    -- transaction; see core/eventlog.py.
    event_seq           INTEGER NOT NULL DEFAULT 0,
    retained_from_seq   INTEGER NOT NULL DEFAULT 1,
    policy              TEXT NOT NULL DEFAULT '{}',   -- JSON RoomPolicy
    retention           TEXT NOT NULL DEFAULT '{}',   -- JSON RetentionPolicy
    created_at          TEXT NOT NULL,
    created_by_user_id  TEXT NOT NULL,
    expires_at          TEXT,
    closed_at           TEXT,
    CHECK (event_seq >= 0),
    CHECK (retained_from_seq >= 1)
);

CREATE INDEX IF NOT EXISTS idx_rooms_org ON rooms(org_id, status);

CREATE TABLE IF NOT EXISTS invitations (
    id                          TEXT PRIMARY KEY,
    room_id                     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    token_hash                  TEXT NOT NULL UNIQUE,
    target_kind                 TEXT NOT NULL,          -- email | org | link
    target_value                TEXT,
    role                        TEXT NOT NULL,
    scopes                      TEXT NOT NULL,          -- JSON array
    max_redemptions             INTEGER NOT NULL DEFAULT 1,
    redemptions                 INTEGER NOT NULL DEFAULT 0,
    expires_at                  TEXT,
    created_at                  TEXT NOT NULL,
    created_by_participant_id   TEXT,
    revoked_at                  TEXT,
    CHECK (redemptions >= 0),
    CHECK (redemptions <= max_redemptions)
);

CREATE INDEX IF NOT EXISTS idx_invitations_room ON invitations(room_id);

CREATE TABLE IF NOT EXISTS participants (
    id                  TEXT PRIMARY KEY,
    room_id             TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    agent_identity_id   TEXT NOT NULL REFERENCES agent_identities(id) ON DELETE CASCADE,
    org_id              TEXT NOT NULL,
    role                TEXT NOT NULL,
    scopes              TEXT NOT NULL,                  -- JSON array
    trust               TEXT NOT NULL DEFAULT 'member',
    state               TEXT NOT NULL DEFAULT 'joined', -- invited | joined | left | removed
    display_name        TEXT NOT NULL,
    token_hash          TEXT UNIQUE,
    joined_at           TEXT,
    left_at             TEXT,
    -- One participant row per identity per room: rejoining reuses the row, so a
    -- participant id is stable across reconnects and remains a valid audit ref.
    UNIQUE (room_id, agent_identity_id)
);

CREATE INDEX IF NOT EXISTS idx_participants_room ON participants(room_id, state);

-- A durable runtime attachment. One logical agent may attach several runtimes to
-- one seat -- a worker that loops and a chat surface that steers -- and each keeps
-- its identity across transport reconnects. The connection below is the ephemeral
-- transport instance; the attachment is the runtime (D-032). Keyed on a label the
-- client reuses, because a server-minted connection id is not stable across a chat
-- host's turn boundary: one participant was observed against three connection ids
-- in as many turns.
CREATE TABLE IF NOT EXISTS attachments (
    id             TEXT PRIMARY KEY,
    room_id        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    label          TEXT NOT NULL,
    host_class     TEXT NOT NULL DEFAULT 'unknown',
    -- A row exists only when the client supplied a stable label; a client that
    -- cannot is ephemeral and gets no row at all (D-034). So this is not "did we
    -- get a label" — it is the client's separate declaration that the label will
    -- address the SAME runtime after a *process* restart, not merely a transport
    -- one. It is recorded, never used to switch affinity: affinity already keys on
    -- the attachment and lapses when nothing of it is live. Where the declaration
    -- matters is recovery — "the same attachment came back" is evidence only if
    -- the client promised the label would mean that (D-036, D-038).
    is_resumable   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    -- Stable identity is the whole point: the same runtime reattaching must land on
    -- the same row, so affinity survives a transport reconnect.
    UNIQUE (participant_id, label)
);

CREATE INDEX IF NOT EXISTS idx_attachments_participant ON attachments(participant_id);

CREATE TABLE IF NOT EXISTS connections (
    id                   TEXT PRIMARY KEY,
    room_id              TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    participant_id       TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    attachment_id        TEXT REFERENCES attachments(id) ON DELETE SET NULL,
    host_class           TEXT NOT NULL DEFAULT 'unknown',
    profile              TEXT NOT NULL DEFAULT '{}',   -- JSON CapabilityProfile
    delivery_mode        TEXT NOT NULL,
    heartbeat_interval_s INTEGER NOT NULL,
    opened_at            TEXT NOT NULL,
    last_heartbeat_at    TEXT NOT NULL,
    last_delivered_seq   INTEGER NOT NULL DEFAULT 0,
    closed_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_connections_participant
    ON connections(participant_id, closed_at);
CREATE INDEX IF NOT EXISTS idx_connections_open ON connections(room_id, closed_at);

-- ---------------------------------------------------------------------------
-- The event log: system of record (ADR-002 / D-003)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS room_events (
    room_id                        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    seq                            INTEGER NOT NULL,
    id                             TEXT NOT NULL UNIQUE,
    type                           TEXT NOT NULL,
    ts                             TEXT NOT NULL,
    actor_participant_id           TEXT,
    actor_display_name             TEXT NOT NULL DEFAULT 'room',
    actor_kind                     TEXT,
    actor_org_id                   TEXT,
    privacy_class                  TEXT NOT NULL DEFAULT 'room_public',
    audience                       TEXT NOT NULL DEFAULT 'room',
    -- JSON array or NULL. NULL = "apply the privacy-class filter"; a list is an
    -- explicit allowlist enforced at fanout (docs/SECURITY.md §6).
    restricted_to_participant_ids  TEXT,
    causation_id                   TEXT,
    payload                        TEXT NOT NULL DEFAULT '{}',
    -- (room_id, seq) is the total order per room. The PK makes a duplicate seq
    -- impossible even under a concurrent allocator bug, on any engine.
    PRIMARY KEY (room_id, seq),
    CHECK (seq >= 1)
);

CREATE INDEX IF NOT EXISTS idx_events_room_type ON room_events(room_id, type);

-- Idempotency for commands. The UNIQUE PK is the whole mechanism: a second
-- attempt to insert the same command_id fails, and the caller returns the stored
-- result instead of acting twice (docs/PROTOCOL.md §2).
CREATE TABLE IF NOT EXISTS command_receipts (
    command_id      TEXT PRIMARY KEY,
    room_id         TEXT NOT NULL,
    participant_id  TEXT,
    command_type    TEXT NOT NULL,
    seq             INTEGER,
    result          TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipts_room ON command_receipts(room_id);

-- ---------------------------------------------------------------------------
-- Projections (read models rebuilt from the log)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS messages (
    id                 TEXT PRIMARY KEY,
    room_id            TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    participant_id     TEXT,
    body               TEXT NOT NULL,
    about_ref          TEXT,
    privacy_class      TEXT NOT NULL DEFAULT 'room_public',
    audience           TEXT NOT NULL DEFAULT 'room',
    to_participant_id  TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id, seq);

CREATE TABLE IF NOT EXISTS work_declarations (
    id                TEXT PRIMARY KEY,
    room_id           TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    participant_id    TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    headline          TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active',
    targets           TEXT NOT NULL DEFAULT '[]',   -- JSON array
    task_id           TEXT,
    note              TEXT NOT NULL DEFAULT '',
    privacy_class     TEXT NOT NULL DEFAULT 'room_public',
    started_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    heartbeat_at      TEXT NOT NULL,
    expected_done_by  TEXT,
    ended_at          TEXT,
    end_reason        TEXT
);

CREATE INDEX IF NOT EXISTS idx_work_room_open ON work_declarations(room_id, ended_at);
CREATE INDEX IF NOT EXISTS idx_work_participant ON work_declarations(participant_id, ended_at);

CREATE TABLE IF NOT EXISTS tasks (
    id                          TEXT PRIMARY KEY,
    room_id                     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    title                       TEXT NOT NULL,
    description                 TEXT NOT NULL DEFAULT '',
    status                      TEXT NOT NULL DEFAULT 'open',
    targets                     TEXT NOT NULL DEFAULT '[]',
    priority                    INTEGER NOT NULL DEFAULT 0,
    created_by_participant_id   TEXT NOT NULL,
    -- Monotonic per task, never reused, retained across release so a revived
    -- stale claimant can never present a currently-valid fence (docs/PROTOCOL.md §4).
    fence                       INTEGER NOT NULL DEFAULT 0,
    claim_lease_id              TEXT,
    claim_participant_id        TEXT,
    claim_fence                 INTEGER,
    claim_claimed_at            TEXT,
    claim_expires_at            TEXT,
    claim_heartbeat_interval_s  INTEGER,
    claim_renewed_at            TEXT,
    -- Who is *executing* under this lease, as opposed to who holds it. The seat
    -- (claim_participant_id) may have several runtimes attached; only one of them
    -- started the work, and only that one may finish it while it is still live
    -- (D-034, D-035). Exactly one of these is set, or neither: a durable runtime
    -- if the client supplied a resumable attachment, otherwise the connection it
    -- claimed from — NULL means "no durable runtime", never "no executor".
    executor_attachment_id      TEXT REFERENCES attachments(id) ON DELETE SET NULL,
    executor_connection_id      TEXT REFERENCES connections(id) ON DELETE SET NULL,
    result                      TEXT NOT NULL DEFAULT '',
    privacy_class               TEXT NOT NULL DEFAULT 'room_public',
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    completed_at                TEXT,
    CHECK (fence >= 0),
    -- A claim is all-or-nothing: these five columns are set together or not at
    -- all, so "claimed with no expiry" is unrepresentable.
    CHECK (
        (claim_lease_id IS NULL AND claim_participant_id IS NULL
             AND claim_fence IS NULL AND claim_expires_at IS NULL)
        OR (claim_lease_id IS NOT NULL AND claim_participant_id IS NOT NULL
             AND claim_fence IS NOT NULL AND claim_expires_at IS NOT NULL)
    ),
    -- An executor is a property *of a lease*, so it cannot outlive one, and it is
    -- one runtime rather than two kinds of runtime at once.
    CHECK (executor_attachment_id IS NULL OR executor_connection_id IS NULL),
    CHECK (
        claim_lease_id IS NOT NULL
        OR (executor_attachment_id IS NULL AND executor_connection_id IS NULL)
    )
    -- NOTE: SQLite cannot add a CHECK with ALTER TABLE, so a database file created
    -- before these columns existed enforces the two above in code only (they are
    -- written from one resolution point and cleared with the claim everywhere).
    -- A fresh file and the eventual PostgreSQL schema get the real constraint.
);

CREATE INDEX IF NOT EXISTS idx_tasks_room ON tasks(room_id, status);
-- Drives the lease reaper without a table scan.
CREATE INDEX IF NOT EXISTS idx_tasks_claim_expiry ON tasks(claim_expires_at);

CREATE TABLE IF NOT EXISTS task_dependencies (
    room_id                    TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    from_task_id               TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    to_task_id                 TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind                       TEXT NOT NULL,
    created_at                 TEXT NOT NULL,
    created_by_participant_id  TEXT NOT NULL,
    PRIMARY KEY (from_task_id, to_task_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_dependencies_room ON task_dependencies(room_id);

CREATE TABLE IF NOT EXISTS task_proposals (
    id                            TEXT PRIMARY KEY,
    room_id                       TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    task_id                       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    to_participant_id             TEXT NOT NULL,
    proposed_by_participant_id    TEXT NOT NULL,
    note                          TEXT NOT NULL DEFAULT '',
    created_at                    TEXT NOT NULL,
    expires_at                    TEXT,
    resolution                    TEXT,
    resolved_at                   TEXT,
    delegated_to_participant_id   TEXT,
    delegated_from_proposal_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_room ON task_proposals(room_id, resolution);
CREATE INDEX IF NOT EXISTS idx_proposals_to ON task_proposals(to_participant_id, resolution);

CREATE TABLE IF NOT EXISTS conflicts (
    id               TEXT PRIMARY KEY,
    room_id          TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',
    subject_refs     TEXT NOT NULL DEFAULT '[]',
    participant_ids  TEXT NOT NULL DEFAULT '[]',
    detail           TEXT NOT NULL DEFAULT '',
    detected_at      TEXT NOT NULL,
    resolved_at      TEXT,
    resolution       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_conflicts_room ON conflicts(room_id, status);

-- Tombstone left behind by a purge, so deletion is provable without retaining
-- content (docs/SECURITY.md §7).
CREATE TABLE IF NOT EXISTS room_tombstones (
    room_id            TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    purged_at          TEXT NOT NULL,
    participant_count  INTEGER NOT NULL DEFAULT 0,
    event_count        INTEGER NOT NULL DEFAULT 0,
    reason             TEXT NOT NULL DEFAULT ''
);
