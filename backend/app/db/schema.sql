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

-- Human authentication is deliberately separate from principal bearer tokens. A password
-- establishes a short-lived browser session; the OAuth client still receives only its own
-- audience-bound agent access token after consent.
CREATE TABLE IF NOT EXISTS user_password_credentials (
    user_id              TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    password_hash        TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    password_changed_at  TEXT NOT NULL
);

-- Session tokens are high-entropy bearers and are stored only as SHA-256 hashes. The CSRF
-- value is not an authenticator by itself; it is stored so server-rendered forms can submit it
-- alongside the HttpOnly session cookie.
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash    TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    revoked_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions(user_id);

-- Account state is separate from `users` so it can be added to an existing Hosted-lite
-- database without rewriting the original identity table. Bootstrap/provisioned operators
-- are verified explicitly; public signups remain unusable until their email challenge wins.
CREATE TABLE IF NOT EXISTS account_status (
    user_id             TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    email_verified_at   TEXT,
    disabled_at         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- Verification and recovery values are high-entropy bearers, stored only as hashes and
-- consumed with guarded updates. A reset token never doubles as a login session.
CREATE TABLE IF NOT EXISTS account_action_tokens (
    token_hash    TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose       TEXT NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    consumed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_account_action_user
    ON account_action_tokens(user_id, purpose);

-- One-shot CSRF state for standalone account forms. OAuth has its own request-bound flow;
-- signup/login/recovery need the same property without pretending an OAuth request exists.
CREATE TABLE IF NOT EXISTS account_browser_flows (
    flow_hash     TEXT PRIMARY KEY,
    purpose       TEXT NOT NULL,
    csrf_token    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    consumed_at   TEXT
);

-- No email or IP address is retained in the throttle ledger. Each bucket is a hash of a
-- namespace plus the normalized value, which is sufficient to enforce account- and IP-level
-- limits without turning this table into an identity log.
CREATE TABLE IF NOT EXISTS login_attempts (
    bucket_hash    TEXT PRIMARY KEY,
    failures       INTEGER NOT NULL DEFAULT 0,
    first_failed_at TEXT NOT NULL,
    last_failed_at  TEXT NOT NULL,
    blocked_until   TEXT
);

-- ---------------------------------------------------------------------------
-- Billing and creator entitlements
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS billing_customers (
    org_id                 TEXT PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
    provider               TEXT NOT NULL,
    provider_customer_id   TEXT NOT NULL UNIQUE,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS billing_subscriptions (
    provider_subscription_id TEXT PRIMARY KEY,
    org_id                    TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider                  TEXT NOT NULL,
    price_id                  TEXT NOT NULL DEFAULT '',
    status                    TEXT NOT NULL,
    current_period_end        TEXT,
    cancel_at_period_end      INTEGER NOT NULL DEFAULT 0,
    provider_event_created_at INTEGER NOT NULL DEFAULT 0,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_billing_subscriptions_org
    ON billing_subscriptions(org_id, status);

-- Generic rather than plan-named: room creation asks for one entitlement, while checkout
-- and bootstrap provisioning are merely sources that can grant it.
CREATE TABLE IF NOT EXISTS organization_entitlements (
    org_id         TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    entitlement    TEXT NOT NULL,
    source         TEXT NOT NULL,
    active_until   TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (org_id, entitlement, source)
);

-- Stripe retries and may reorder deliveries. The unique event id makes processing
-- idempotent; provider_event_created_at on subscriptions prevents an older event from
-- overwriting newer state.
CREATE TABLE IF NOT EXISTS billing_webhook_events (
    provider_event_id TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,
    event_created_at  INTEGER NOT NULL,
    received_at       TEXT NOT NULL,
    processed_at      TEXT
);

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

-- A browser authorization flow holds only a request that already passed OAuth validation.
-- Keeping it server-side means login and consent forms do not have to trust hidden copies of
-- redirect URIs, PKCE challenges, or state. The opaque cookie is hashed at rest and expires
-- before an authorization code would be useful.
CREATE TABLE IF NOT EXISTS oauth_browser_flows (
    flow_hash       TEXT PRIMARY KEY,
    csrf_token      TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    redirect_uri    TEXT NOT NULL,
    code_challenge  TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT '',
    state           TEXT,
    resource        TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_oauth_browser_flows_client
    ON oauth_browser_flows(client_id);

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
    charter             TEXT NOT NULL DEFAULT '',
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

-- A narrow, expiring credential for one runtime of a seat (D-048).
--
-- The problem it removes: running a companion worker used to mean copying the
-- participant token into a daemon, and that token carries everything the seat can
-- do — including `room.admin` if it has it, and the ability to mint more
-- credentials. A background process that only needs to claim and finish its own
-- work should not be handed the authority to reconfigure the room.
--
-- Scopes here are always a subset of both the participant's own scopes and a
-- fixed runtime allowlist, computed at mint time and re-clamped on every use, so
-- narrowing a seat later narrows its outstanding credentials too.
CREATE TABLE IF NOT EXISTS participant_credentials (
    id             TEXT PRIMARY KEY,
    room_id        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    token_hash     TEXT NOT NULL UNIQUE,
    label          TEXT NOT NULL DEFAULT '',
    scopes         TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    -- Never null. A runtime credential that outlives the machine it was put on is
    -- the failure this exists to bound, so there is no "forever" option.
    expires_at     TEXT NOT NULL,
    revoked_at     TEXT,
    last_used_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_credentials_participant
    ON participant_credentials(participant_id, revoked_at);

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
    -- What this runtime is *for*, and how it does the work. Declared by the client
    -- and never verified, which is why they are grouped under `declared` in every
    -- projection: attribution, not verification (D-054). Nothing in the server
    -- branches on these — behaviour comes from negotiated capabilities alone
    -- (principle 4), and a room that started routing on `executor_kind` would have
    -- reinvented vendor labels with extra steps.
    runtime_role   TEXT NOT NULL DEFAULT 'unspecified',
    executor_kind  TEXT NOT NULL DEFAULT '',
    executor_model TEXT NOT NULL DEFAULT '',
    -- Projected work posture. Unlike connection liveness this is durable state,
    -- but it never grants authority or claims that the runtime is connected.
    operational_state TEXT NOT NULL DEFAULT 'monitoring',
    operational_summary TEXT NOT NULL DEFAULT '',
    waiting_reason TEXT NOT NULL DEFAULT '',
    operational_task_id TEXT,
    operational_work_id TEXT,
    operational_updated_at TEXT,
    created_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    -- Which run of this runtime is current. Bumped by a drain, never reused. A fence
    -- says which run of a *lease* a caller belongs to; this says which run of a
    -- *runtime*, and the two are independent: a process can hold a perfectly valid
    -- fence and still be a run that was told to stop.
    epoch          INTEGER NOT NULL DEFAULT 1,
    -- Set when this runtime was drained, cleared only by an explicit resume. Sticky
    -- on purpose. Killing a process is not something a server can do — in the hosted
    -- product the runtime is on someone else's machine — so the only durable control
    -- is refusing to accept its work. A drained runtime that survives its kill, or
    -- reconnects afterwards, lands back on this same row and is refused again;
    -- clearing it is a separate, visible act rather than a side effect of reconnecting.
    drained_at     TEXT,
    drained_reason TEXT NOT NULL DEFAULT '',
    -- Stable identity is the whole point: the same runtime reattaching must land on
    -- the same row, so affinity survives a transport reconnect.
    UNIQUE (participant_id, label),
    CHECK (epoch >= 1),
    CHECK (operational_state IN ('monitoring', 'working', 'waiting'))
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

-- One-use browser realtime credentials. The durable participant token is exchanged
-- over authenticated HTTP and never appears in a WebSocket URL or proxy log.
CREATE TABLE IF NOT EXISTS stream_tickets (
    token_hash     TEXT PRIMARY KEY,
    room_id        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    consumed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_stream_tickets_expiry ON stream_tickets(expires_at);

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
    -- "The owner's runtime is still here." Refreshed by the connection heartbeat as
    -- well as by declare/update, so a worker inside a long step does not read as gone.
    heartbeat_at      TEXT NOT NULL,
    -- "The work itself moved." Refreshed only by declare/update/checkpoint — never by
    -- a transport beat, which is why staleness stays reachable for a wedged worker
    -- whose connection is perfectly healthy (D-059).
    progress_at       TEXT,
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
    -- Human control over work in flight (D-045). `running` is the absence of a
    -- directive. `paused` keeps the holder's place but forbids progress; `stopped`
    -- also forbids re-claiming, without which "stop" would mean "stop until the
    -- worker's next loop iteration". Never a hint the worker is asked to respect:
    -- claim, complete and update all check it, so a worker that ignores its steering
    -- cannot act on the room regardless of what it decides internally.
    steering                    TEXT NOT NULL DEFAULT 'running',
    steering_reason             TEXT NOT NULL DEFAULT '',
    steering_by_participant_id  TEXT,
    steering_at                 TEXT,
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
    CHECK (steering IN ('running','paused','stopped')),
    CHECK (
        claim_lease_id IS NOT NULL
        OR (executor_attachment_id IS NULL AND executor_connection_id IS NULL)
    )
    -- NOTE: SQLite cannot add a CHECK with ALTER TABLE, so a database file created
    -- before these columns existed enforces the two above in code only (they are
    -- written from one resolution point and cleared with the claim everywhere).
    -- A fresh file and the eventual PostgreSQL schema get the real constraint.
);

CREATE TABLE IF NOT EXISTS directives (
    id                             TEXT PRIMARY KEY,
    room_id                        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    -- Addressed to a seat, not a runtime: whoever is steering does not, and should
    -- not, know which of the target's runtimes happens to be executing right now.
    target_participant_id          TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    task_id                        TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    action                         TEXT NOT NULL,
    reason                         TEXT NOT NULL DEFAULT '',
    issued_by_participant_id       TEXT NOT NULL,
    -- Derived from the issuer's identity, never accepted from a caller. Attribution,
    -- not verification: it says the issuing identity is a human principal, not that a
    -- human was present. Authorization is `room.admin` and lives nowhere near here.
    human_origin                   INTEGER NOT NULL DEFAULT 0,
    created_seq                    INTEGER NOT NULL,
    -- Effect and observation are ORTHOGONAL and must not be flattened into one
    -- lifecycle. A control action applies at issue, because waiting for the target to
    -- acknowledge would make stopping a runaway worker depend on the runaway worker.
    -- `applied but never acknowledged` is therefore a real state, and the one an
    -- incident review most wants to be able to read.
    effect_status                  TEXT NOT NULL,
    created_at                     TEXT NOT NULL,
    applied_at                     TEXT,
    acknowledged_at                TEXT,
    acknowledged_by_participant_id TEXT,
    CHECK (action IN ('pause','stop','resume','reprioritize','input')),
    CHECK (effect_status IN ('pending','applied','rejected','superseded'))
);

-- Drives "what is waiting for me" without a scan.
CREATE INDEX IF NOT EXISTS idx_directives_target
    ON directives(target_participant_id, acknowledged_at);
CREATE INDEX IF NOT EXISTS idx_directives_room ON directives(room_id, created_seq);

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

-- Durable progress on a task (D-050). Append-only in the strong sense: nothing in
-- the codebase updates or deletes a row here, because a checkpoint that could be
-- edited would be a claim about the past the past does not support.
CREATE TABLE IF NOT EXISTS task_checkpoints (
    id                  TEXT PRIMARY KEY,
    room_id             TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    -- The seat, never the runtime: a companion worker and the chat surface sharing
    -- one participant are one accountable party.
    participant_id      TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    -- Which runtime of that seat wrote it, when there is a durable one. NULL means
    -- an ephemeral runtime, not an unknown one.
    attachment_id       TEXT REFERENCES attachments(id) ON DELETE SET NULL,
    -- The lease generation this was written under. History from a superseded
    -- generation stays true; the fence says which run it belongs to.
    fence               INTEGER NOT NULL,
    -- Room-visible outcome. Bounded so a transcript pasted here is conspicuous.
    summary             TEXT NOT NULL,
    -- The same-seat bookmark, or NULL. Never returned to another participant by any
    -- projection; the log frame carrying it is restricted to the writing seat.
    resume_state        TEXT,
    seq                 INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    CHECK (fence >= 0)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON task_checkpoints(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_checkpoints_participant
    ON task_checkpoints(participant_id, seq);

-- Worker -> human, which the control plane cannot express by construction: a
-- directive requires room.admin so a worker cannot manufacture instructions, so a
-- question cannot be a directive with the ends swapped (D-051).
CREATE TABLE IF NOT EXISTS questions (
    id                        TEXT PRIMARY KEY,
    room_id                   TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    -- Required when blocking: blocking means "this task cannot proceed", and a task
    -- is the only thing the room knows how to halt.
    task_id                   TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    asked_by_participant_id   TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    -- WHICH RUNTIME of that seat asked, so the self-answer refusal can be scoped to
    -- the runtime rather than the seat (D-055). Scoped to the seat it blocked the
    -- person who most obviously should answer: a human's chat surface and their
    -- companion worker are one participant.
    asked_by_attachment_id    TEXT,
    answered_by_attachment_id TEXT,
    -- NULL means the room at large. Addressing narrows who is *expected* to reply,
    -- never who may: a question nobody answers is worse than one answered by the
    -- wrong person.
    to_participant_id         TEXT REFERENCES participants(id) ON DELETE SET NULL,
    body                      TEXT NOT NULL,
    -- Whether the asker released its work to wait. Opt-in, because a worker that
    -- halts on every uncertainty cannot work unattended.
    blocking                  INTEGER NOT NULL DEFAULT 0,
    created_seq               INTEGER NOT NULL,
    created_at                TEXT NOT NULL,
    answered_at               TEXT,
    answered_by_participant_id TEXT,
    answer_id                 TEXT,
    CHECK (blocking = 0 OR task_id IS NOT NULL),
    -- Answered is all-or-nothing, so "answered by nobody" is unrepresentable.
    CHECK (
        (answered_at IS NULL AND answered_by_participant_id IS NULL AND answer_id IS NULL)
        OR (answered_at IS NOT NULL AND answered_by_participant_id IS NOT NULL
            AND answer_id IS NOT NULL)
    )
);

-- Drives "what is waiting on me" and "what is my worker blocked on" without a scan.
CREATE INDEX IF NOT EXISTS idx_questions_open ON questions(room_id, answered_at);
CREATE INDEX IF NOT EXISTS idx_questions_to ON questions(to_participant_id, answered_at);
CREATE INDEX IF NOT EXISTS idx_questions_task ON questions(task_id, answered_at);

-- Its own row, not a column on the question: an answer has its own author and its
-- own place in the log, and collapsing it would make the first reply the only one.
CREATE TABLE IF NOT EXISTS answers (
    id                        TEXT PRIMARY KEY,
    room_id                   TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    question_id               TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    answered_by_participant_id TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    body                      TEXT NOT NULL,
    seq                       INTEGER NOT NULL,
    created_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_answers_question ON answers(question_id, seq);

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

-- ---------------------------------------------------------------------------
-- The coordination hierarchy (D-088)
-- ---------------------------------------------------------------------------

-- Where a seat sits in the work hierarchy, which is NOT what it may do.
-- `participants.role` answers "what may this seat do" and resolves to scopes;
-- this answers "who coordinates whom". Kept independent on purpose: deriving
-- authority from a hierarchy label would let a coordination position mint
-- privileges, which is the failure ADR-013 records.
--
-- Its own table rather than a column on `participants`, for two reasons. SQLite
-- cannot add a CHECK with ALTER TABLE, so a `room_role` column would be
-- unconstrained on every database created before it -- the hole already annotated
-- on `tasks` and `attachments`. And a role is assigned by somebody, at some seq,
-- for some reason: three columns that only mean anything together.
CREATE TABLE IF NOT EXISTS participant_roles (
    -- `participants.id` is unique across rooms, so it is the natural key. `room_id`
    -- is carried for tenancy-scoped reads and for the uniqueness index below.
    participant_id             TEXT PRIMARY KEY
                                   REFERENCES participants(id) ON DELETE CASCADE,
    room_id                    TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    room_role                  TEXT NOT NULL,
    -- Who put this seat here. NULL means the room itself did -- creation, or the
    -- one-time backfill -- never "unknown".
    assigned_by_participant_id TEXT,
    assigned_seq               INTEGER NOT NULL DEFAULT 0,
    reason                     TEXT NOT NULL DEFAULT '',
    -- How the assignment arose. Attribution; nothing branches on it. It exists so
    -- an audit can tell a migration from a human's choice.
    source                     TEXT NOT NULL DEFAULT 'assigned',
    -- Stood down without replacement. Kept rather than deleted so the row stays a
    -- valid audit reference, and so the partial unique index can ignore it.
    retired_at                 TEXT,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    CHECK (room_role IN ('orchestrator','supervisor','observer','unassigned')),
    CHECK (source IN ('room_creator','joined','assigned','migration')),
    CHECK (assigned_seq >= 0)
);

CREATE INDEX IF NOT EXISTS idx_participant_roles_room
    ON participant_roles(room_id, room_role);

-- At most one live orchestrator per room, enforced by the engine rather than by a
-- read-then-write in the service. A partial unique index is the portable form of
-- "at most one row matching this predicate": SQLite 3.8+ and PostgreSQL 9.5+ both
-- honour it, and a second concurrent promotion loses the insert instead of
-- producing two orchestrators (ADR-009).
CREATE UNIQUE INDEX IF NOT EXISTS uq_participant_roles_one_orchestrator
    ON participant_roles(room_id)
    WHERE room_role = 'orchestrator' AND retired_at IS NULL;

-- ---------------------------------------------------------------------------
-- Supervisor goals: versioned, replaceable direction
-- ---------------------------------------------------------------------------

-- What one supervisor is currently responsible for, as a pointer to its current
-- version. This row IS the version allocator, exactly as `rooms.event_seq` is for
-- events:
--   UPDATE supervisor_goals SET current_version = current_version + 1, ...
--    WHERE id = ? AND current_version = ?
-- inside the mutating transaction. A 0-row result means another revision landed
-- first -- which is "you are stale", not "retry" -- and no version is reused.
CREATE TABLE IF NOT EXISTS supervisor_goals (
    id                        TEXT PRIMARY KEY,
    room_id                   TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    -- The seat, never a runtime. A goal outlives the companion executing it, and
    -- the seat is the accountable party.
    supervisor_participant_id TEXT NOT NULL
                                  REFERENCES participants(id) ON DELETE CASCADE,
    current_version           INTEGER NOT NULL DEFAULT 1,
    status                    TEXT NOT NULL DEFAULT 'active',
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    closed_at                 TEXT,
    CHECK (current_version >= 1),
    CHECK (status IN ('active','achieved','abandoned')),
    -- Closing is all-or-nothing, so "abandoned at no time" is unrepresentable.
    CHECK ((status = 'active' AND closed_at IS NULL)
        OR (status <> 'active' AND closed_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_supervisor_goals_room
    ON supervisor_goals(room_id, status);

-- One live goal per seat. A supervisor with two active goals has no active goal.
CREATE UNIQUE INDEX IF NOT EXISTS uq_supervisor_goals_one_active
    ON supervisor_goals(supervisor_participant_id)
    WHERE status = 'active';

-- Every version the goal has ever had. Append-only in the strong sense
-- `task_checkpoints` uses: the only columns ever updated are the supersession pair
-- and the acknowledgement, both stamped once. "What was the objective when this job
-- was posted" therefore stays answerable after ten revisions.
CREATE TABLE IF NOT EXISTS supervisor_goal_versions (
    goal_id                    TEXT NOT NULL
                                   REFERENCES supervisor_goals(id) ON DELETE CASCADE,
    version                    INTEGER NOT NULL,
    room_id                    TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    objective                  TEXT NOT NULL,
    instructions               TEXT NOT NULL DEFAULT '',
    worker_plan                TEXT NOT NULL DEFAULT '',
    related_job_ids            TEXT NOT NULL DEFAULT '[]',   -- JSON array
    dependencies               TEXT NOT NULL DEFAULT '[]',   -- JSON array
    constraints_json           TEXT NOT NULL DEFAULT '[]',   -- JSON array
    acceptance_criteria        TEXT NOT NULL DEFAULT '[]',   -- JSON array
    reporting_requirements     TEXT NOT NULL DEFAULT '',
    worker_disposition         TEXT NOT NULL DEFAULT 'stop',
    reason                     TEXT NOT NULL DEFAULT '',
    priority                   INTEGER NOT NULL DEFAULT 0,
    source                     TEXT NOT NULL DEFAULT 'orchestrator',
    -- Free-form text, so it is disclosure-checked like any other room content and
    -- carries a class of its own (docs/SECURITY.md §6). This DDL does not protect
    -- the field; `privacy.check_disclosure` does.
    privacy_class              TEXT NOT NULL DEFAULT 'room_public',
    -- No FK: authorship must survive the issuer leaving the room, the same choice
    -- `tasks.created_by_participant_id` makes.
    issued_by_participant_id   TEXT NOT NULL,
    replaces_version           INTEGER,
    created_seq                INTEGER NOT NULL DEFAULT 0,
    created_at                 TEXT NOT NULL,
    superseded_at              TEXT,
    superseded_by_version      INTEGER,
    -- Evidence the target observed this version. Never permission for the effect.
    acknowledged_at            TEXT,
    acknowledged_note          TEXT NOT NULL DEFAULT '',
    acknowledged_rejected      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (goal_id, version),
    CHECK (version >= 1),
    CHECK (created_seq >= 0),
    CHECK (acknowledged_rejected IN (0,1)),
    CHECK (worker_disposition IN ('stop','drain','continue')),
    CHECK (source IN ('orchestrator','supervisor','migration')),
    -- Supersession is all-or-nothing and always forward.
    CHECK ((superseded_at IS NULL AND superseded_by_version IS NULL)
        OR (superseded_at IS NOT NULL AND superseded_by_version IS NOT NULL
            AND superseded_by_version > version)),
    CHECK (replaces_version IS NULL OR replaces_version < version)
);

CREATE INDEX IF NOT EXISTS idx_goal_versions_room
    ON supervisor_goal_versions(room_id, created_seq);
CREATE INDEX IF NOT EXISTS idx_goal_versions_live
    ON supervisor_goal_versions(goal_id, superseded_at);

-- ---------------------------------------------------------------------------
-- Supervisor capacity: a declared allocation signal with derived counts
-- ---------------------------------------------------------------------------

-- What a supervisor says it can take on. Deliberately not a raw count: "two
-- workers running" says nothing about whether a third would help, so the seat
-- publishes a judgement and the room counts the rows itself.
--
-- `offline` is absent from the CHECK on purpose. It is derived from connection
-- liveness at read time, because a runtime that has stopped beating cannot be
-- trusted to report that it is gone (principle 5).
CREATE TABLE IF NOT EXISTS supervisor_capacity (
    participant_id          TEXT PRIMARY KEY
                                REFERENCES participants(id) ON DELETE CASCADE,
    room_id                 TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    declared                TEXT NOT NULL DEFAULT 'available',
    max_concurrent_workers  INTEGER NOT NULL DEFAULT 1,
    note                    TEXT NOT NULL DEFAULT '',
    declared_at             TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    CHECK (declared IN ('available','partially_allocated','fully_allocated','blocked')),
    CHECK (max_concurrent_workers >= 0)
);

CREATE INDEX IF NOT EXISTS idx_supervisor_capacity_room
    ON supervisor_capacity(room_id, declared);

-- ---------------------------------------------------------------------------
-- The job board: durable human intent, and where it went
-- ---------------------------------------------------------------------------

-- A unit of work as the BOARD sees it: who wants it done, in whose words, who it
-- was allocated to, and how it ended. Deliberately not a second task table --
-- `tasks` remains the only thing carrying a lease, a fence and an executor, so the
-- room never has two answers to "who holds this".
--
-- A job reaches a terminal state only with an attributable reason, and nothing
-- deletes one.
CREATE TABLE IF NOT EXISTS jobs (
    id                          TEXT PRIMARY KEY,
    room_id                     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    -- The lease-bearing task, once this is execution rather than a listing. SET NULL
    -- on delete: cancelling a task does not erase the board's memory of the job.
    task_id                     TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    title                       TEXT NOT NULL,
    desired_outcome             TEXT NOT NULL DEFAULT '',
    -- The person's own words, unedited. A paraphrase cannot be un-paraphrased once
    -- the intent is disputed, which is the whole reason the board exists.
    human_instruction           TEXT NOT NULL DEFAULT '',
    room_goal_relationship      TEXT NOT NULL DEFAULT '',
    constraints_json            TEXT NOT NULL DEFAULT '[]',   -- JSON array
    acceptance_criteria         TEXT NOT NULL DEFAULT '[]',   -- JSON array
    targets                     TEXT NOT NULL DEFAULT '[]',   -- JSON array
    -- Urgency as requested vs as decided. Both, so a supervisor can see that its
    -- request was ranked below something else rather than silently ignored.
    requested_urgency           INTEGER NOT NULL DEFAULT 0,
    priority                    INTEGER NOT NULL DEFAULT 0,
    state                       TEXT NOT NULL DEFAULT 'posted',
    origin                      TEXT NOT NULL DEFAULT 'human_steer',
    -- PROVENANCE. No FK on the poster, for the same reason tasks does not have one.
    posted_by_participant_id    TEXT NOT NULL,
    on_behalf_of_participant_id TEXT,
    source_goal_id              TEXT,
    source_goal_version         INTEGER,
    parent_job_id               TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    -- ALLOCATION.
    assigned_to_participant_id  TEXT,
    assigned_by_participant_id  TEXT,
    assigned_at                 TEXT,
    accepted_at                 TEXT,
    assigned_goal_version       INTEGER,
    -- TERMINATION.
    terminal_reason             TEXT NOT NULL DEFAULT '',
    terminated_by_participant_id TEXT,
    superseded_by_job_id        TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    privacy_class               TEXT NOT NULL DEFAULT 'room_public',
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    closed_at                   TEXT,
    CHECK (state IN ('posted','assigned','accepted','active','paused','blocked',
                     'completed','cancelled','superseded','rejected')),
    CHECK (origin IN ('human_steer','agent_proposal','decomposition','migration')),
    CHECK (source_goal_version IS NULL OR source_goal_version >= 1),
    CHECK (assigned_goal_version IS NULL OR assigned_goal_version >= 1),
    -- Assignment is all-or-nothing: an assignee with no timestamp, or a timestamp
    -- with no assignee, is a half-written allocation nobody can audit.
    CHECK ((assigned_to_participant_id IS NULL AND assigned_at IS NULL)
        OR (assigned_to_participant_id IS NOT NULL AND assigned_at IS NOT NULL)),
    -- Closing is all-or-nothing, and always carries a reason.
    CHECK ((closed_at IS NULL AND state NOT IN ('completed','cancelled','superseded','rejected'))
        OR (closed_at IS NOT NULL AND state IN ('completed','cancelled','superseded','rejected')
            AND terminal_reason <> '')),
    -- A supersession that does not name its replacement is a cancellation wearing
    -- the wrong label.
    CHECK (state <> 'superseded' OR superseded_by_job_id IS NOT NULL),
    CHECK (parent_job_id IS NULL OR parent_job_id <> id),
    CHECK (superseded_by_job_id IS NULL OR superseded_by_job_id <> id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_room ON jobs(room_id, state, priority);
CREATE INDEX IF NOT EXISTS idx_jobs_assignee
    ON jobs(assigned_to_participant_id, state);
CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs(task_id);

-- One task belongs to at most one job. Two jobs pointing at one lease would make
-- "which intent is this work serving" unanswerable.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_task
    ON jobs(task_id)
    WHERE task_id IS NOT NULL;

-- Append-only transition history. The event log is the source of truth for all of
-- it; these rows exist so the board can answer "how did this job get here" without
-- replaying a room.
CREATE TABLE IF NOT EXISTS job_events (
    job_id               TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ordinal              INTEGER NOT NULL,
    room_id              TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    from_state           TEXT,
    to_state             TEXT NOT NULL,
    actor_participant_id TEXT,
    reason               TEXT NOT NULL DEFAULT '',
    seq                  INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (job_id, ordinal),
    CHECK (ordinal >= 1),
    CHECK (seq >= 0)
);

CREATE INDEX IF NOT EXISTS idx_job_events_room ON job_events(room_id, seq);
-- ---------------------------------------------------------------------------
-- Workers: downstream execution a supervisor answers for (D-077)
-- ---------------------------------------------------------------------------

-- A worker is not a participant. Membership has exactly one entry path, and a
-- supervisor that could mint participants would be minting membership. So this is
-- the supervisor's own account of an executor it owns, recorded so the room can say
-- who created it, what it was for, which goal version caused it, and where its
-- result went.
--
-- Nothing here is verified and nothing here is presence. `state` is the
-- supervisor's last claim; a worker that dies silently stays 'working' until its
-- supervisor notices, which is why readers show `last_activity_at` beside it. Where
-- the worker IS a durable runtime of that seat, `attachment_id` points at it and
-- liveness comes from core/presence.py like anything else.
CREATE TABLE IF NOT EXISTS workers (
    id                        TEXT PRIMARY KEY,
    room_id                   TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    -- The accountable seat.
    supervisor_participant_id TEXT NOT NULL
                                  REFERENCES participants(id) ON DELETE CASCADE,
    -- Which runtime of that seat spawned it, so a restarted supervisor can tell its
    -- own workers from a previous run's. SET NULL: a runtime going away does not
    -- make the worker's history untrue.
    supervisor_attachment_id  TEXT REFERENCES attachments(id) ON DELETE SET NULL,
    -- Set only for 'room_attachment' provenance. There is deliberately no CHECK
    -- tying the two: a CHECK must never reference a column another table's
    -- ON DELETE SET NULL can clear, or deleting the attachment would fail the CHECK
    -- and abort the delete, so a room purge could not complete. core/workers.py
    -- asserts the pairing at insert time instead.
    attachment_id             TEXT REFERENCES attachments(id) ON DELETE SET NULL,
    -- Stable and supervisor-chosen, so re-declaring the same worker lands on this
    -- row instead of minting a second identity (the attachments.label rule).
    label                     TEXT NOT NULL,
    display_name              TEXT NOT NULL DEFAULT '',
    provenance                TEXT NOT NULL DEFAULT 'declared',
    assignment                TEXT NOT NULL DEFAULT '',
    related_job_id            TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    related_task_id           TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    related_work_id           TEXT,
    -- The goal version that caused this worker to exist. This is what stops stale
    -- work from completing a newer goal: output keeps the provenance of the
    -- direction that produced it.
    created_by_goal_version   INTEGER,
    -- Self-reported runtime detail. Nothing branches on it (D-054).
    declared_runtime          TEXT NOT NULL DEFAULT '',
    declared_model            TEXT NOT NULL DEFAULT '',
    state                     TEXT NOT NULL DEFAULT 'starting',
    summary                   TEXT NOT NULL DEFAULT '',
    waiting_reason            TEXT NOT NULL DEFAULT '',
    result_reference          TEXT NOT NULL DEFAULT '',
    attempts                  INTEGER NOT NULL DEFAULT 0,
    created_at                TEXT NOT NULL,
    started_at                TEXT,
    -- The supervisor's last claim, not an observation. Never rendered as presence.
    last_activity_at          TEXT,
    completed_at              TEXT,
    retired_at                TEXT,
    UNIQUE (supervisor_participant_id, label),
    CHECK (provenance IN ('room_attachment','declared')),
    CHECK (state IN ('starting','working','waiting','completed','failed','stopping','stopped')),
    CHECK (attempts >= 0),
    CHECK (created_by_goal_version IS NULL OR created_by_goal_version >= 1)
);

CREATE INDEX IF NOT EXISTS idx_workers_room ON workers(room_id, state);
CREATE INDEX IF NOT EXISTS idx_workers_supervisor
    ON workers(supervisor_participant_id, retired_at);
CREATE INDEX IF NOT EXISTS idx_workers_job ON workers(related_job_id);

-- One runtime is one worker. Without this, one supervisor could describe the same
-- attachment twice and the board would believe it had twice the capacity it has.
CREATE UNIQUE INDEX IF NOT EXISTS uq_workers_attachment
    ON workers(attachment_id)
    WHERE attachment_id IS NOT NULL AND retired_at IS NULL;

