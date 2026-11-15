CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS staff (
    id          SERIAL PRIMARY KEY,
    chat_id     VARCHAR(50) UNIQUE,
    email       VARCHAR(255) UNIQUE,
    name        VARCHAR(255) NOT NULL,
    role        VARCHAR(50) DEFAULT 'user',
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS documents (
    id               SERIAL PRIMARY KEY,
    title            VARCHAR(500) NOT NULL,
    doc_type         VARCHAR(50),
    matter           VARCHAR(255),
    source_filename  VARCHAR(500),
    content_sha256   VARCHAR(64),
    source_storage_key TEXT,
    source_sha256    VARCHAR(64),
    source_size_bytes BIGINT,
    full_text        TEXT,
    uploaded_by      INTEGER REFERENCES staff(id),
    created_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_documents_matter ON documents(matter);
-- The catalogue is filtered by time far more often than it is listed whole.
CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_sha256 ON documents(content_sha256);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_storage_key TEXT;
-- The wrapped data key of a document kept in the object storage bucket. Its presence is
-- what says where the original lives, since bucket objects are always
-- encrypted and volume files never are. Deleting the row destroys the only
-- copy of the key, so the object left in the bucket becomes unrecoverable:
-- that is what LGPD erasure rests on here.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_wrapped_key BYTEA;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_kek_version INTEGER;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_sha256 VARCHAR(64);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_size_bytes BIGINT;
-- The official page a law in the corpus is kept current against, and when it
-- was last compared with it. Empty for everything that is not a law.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_checked_at TIMESTAMPTZ;
-- Legislation's sphere (federal, estadual, municipal): a label on the card.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS sphere TEXT;

CREATE TABLE IF NOT EXISTS document_chunks (
    id           SERIAL PRIMARY KEY,
    document_id  INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    content      TEXT NOT NULL,
    embedding    vector(1536),
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON document_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON document_chunks
    USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS document_access_log (
    id           SERIAL PRIMARY KEY,
    staff_id     INTEGER REFERENCES staff(id),
    chat_id      VARCHAR(50),
    action       VARCHAR(50),
    query        TEXT,
    document_ids INTEGER[],
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_access_log_staff ON document_access_log(staff_id);
CREATE INDEX IF NOT EXISTS idx_access_log_created ON document_access_log(created_at);

CREATE TABLE IF NOT EXISTS access_requests (
    id           SERIAL PRIMARY KEY,
    request_code VARCHAR(16) UNIQUE NOT NULL,
    chat_id      VARCHAR(50) UNIQUE NOT NULL,
    name         VARCHAR(255) NOT NULL,
    username     VARCHAR(255),
    status       VARCHAR(16) NOT NULL DEFAULT 'pending',
    decided_by   INTEGER REFERENCES staff(id),
    requested_at TIMESTAMP DEFAULT NOW(),
    decided_at   TIMESTAMP,
    CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_access_requests_status
    ON access_requests(status, requested_at);

CREATE TABLE IF NOT EXISTS staff_management_log (
    id               BIGSERIAL PRIMARY KEY,
    action           VARCHAR(32) NOT NULL,
    actor_staff_id   INTEGER NOT NULL REFERENCES staff(id),
    target_staff_id  INTEGER NOT NULL REFERENCES staff(id),
    target_role      VARCHAR(50) NOT NULL,
    reason           VARCHAR(64) NOT NULL,
    created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    CHECK (action IN ('deactivate', 'erase'))
);

CREATE INDEX IF NOT EXISTS idx_staff_management_target_created
    ON staff_management_log(target_staff_id, created_at DESC);

-- What has already been read out of the firm's document bucket, keyed by
-- the name and the content's ETag rather than the name: a corrected file uploaded over
-- an old name is a different object and must be read again, which keying by
-- name would have silently ignored.
CREATE TABLE IF NOT EXISTS inbox_imports (
    file_id     VARCHAR(200) PRIMARY KEY,
    file_name   VARCHAR(500) NOT NULL,
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    outcome     VARCHAR(32) NOT NULL,
    detail      VARCHAR(200),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (outcome IN ('ingested', 'duplicate', 'skipped', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_inbox_imports_imported_at
    ON inbox_imports(imported_at DESC);

-- One pending step per chat, so a restart does not lose the flow a person is
-- halfway through. The primary key is the invariant: a chat waits on exactly
-- one thing, and starting a new flow replaces the old one instead of leaving
-- two live questions that both claim the next message.
CREATE TABLE IF NOT EXISTS pending_actions (
    chat_id    VARCHAR(50) PRIMARY KEY,
    kind       VARCHAR(16) NOT NULL,
    action     VARCHAR(32),
    approved   BOOLEAN,
    target_id  INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    args       JSONB,
    -- The window that asked; a destructive confirmation is consumed only there.
    channel    VARCHAR(8) NOT NULL DEFAULT 'telegram',
    CHECK (kind IN ('identifier', 'decision', 'export', 'action'))
);

-- Sweeping expired rows is a scan by time, never by chat.
CREATE INDEX IF NOT EXISTS idx_pending_actions_expires
    ON pending_actions(expires_at);

-- A button press, bound to who may press it and what it does. callback_data
-- carries only the opaque token: no client name, process number or document
-- content ever travels through Telegram's payload.
--
-- The row is the authority. Identity, authorization, state and validity are
-- re-checked on every press against this row and the current staff record, so
-- a button issued before a demotion stops working at the demotion, not at its
-- own expiry.
CREATE TABLE IF NOT EXISTS callback_actions (
    token      VARCHAR(43) PRIMARY KEY,
    chat_id    VARCHAR(50) NOT NULL,
    staff_id   INTEGER NOT NULL REFERENCES staff(id),
    issued_role VARCHAR(50) NOT NULL,
    action     VARCHAR(64) NOT NULL,
    operation  VARCHAR(32),
    resource   VARCHAR(128),
    state      VARCHAR(16) NOT NULL DEFAULT 'pending',
    message_id BIGINT,
    channel    VARCHAR(8) NOT NULL DEFAULT 'telegram',
    reference  VARCHAR(64),
    result     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    CHECK (state IN ('pending', 'used', 'cancelled'))
);

-- Invalidating the siblings of a decided button reads by chat and resource;
-- the sweep reads by time.
CREATE INDEX IF NOT EXISTS idx_callback_actions_resource
    ON callback_actions(chat_id, resource);
CREATE INDEX IF NOT EXISTS idx_callback_actions_expires
    ON callback_actions(expires_at);

-- What each person follows in the DJEN, and which publications they were
-- already told about. Also created at startup by ensure_publication_schema.
CREATE TABLE IF NOT EXISTS watched_oabs (
    staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    oab_number  VARCHAR(10) NOT NULL,
    oab_uf      CHAR(2) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (staff_id, oab_number, oab_uf)
);

CREATE TABLE IF NOT EXISTS watched_processes (
    staff_id        INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    process_number  VARCHAR(20) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (staff_id, process_number)
);

CREATE TABLE IF NOT EXISTS publication_notices (
    publication_id  BIGINT NOT NULL,
    staff_id        INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (publication_id, staff_id)
);

-- Folders: a document is private to whoever added it, shared with a team, or
-- the firm's. Also created at startup by ensure_team_schema.
CREATE TABLE IF NOT EXISTS teams (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(80) UNIQUE NOT NULL,
    created_by  INTEGER REFERENCES staff(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id    INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    staff_id   INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (team_id, staff_id)
);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private'
    CHECK (visibility IN ('private', 'team', 'firm'));
ALTER TABLE documents ADD COLUMN IF NOT EXISTS team_id INTEGER REFERENCES teams(id);
CREATE INDEX IF NOT EXISTS idx_documents_visibility ON documents(visibility);

-- The Microsoft account each person connected. The refresh token is encrypted
-- with MS_TOKEN_KEY before it gets here: the nightly backup leaves this host
-- and that key does not. Also created at startup by ensure_microsoft_schema.
CREATE TABLE IF NOT EXISTS microsoft_accounts (
    staff_id       INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
    account_name   VARCHAR(255) NOT NULL,
    refresh_token  BYTEA NOT NULL,
    connected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at   TIMESTAMPTZ
);

-- A Microsoft sign-in the panel is waiting on, one per person, for the
-- fifteen minutes its code lives. The device code is sealed. Also created at
-- startup by ensure_microsoft_schema.
CREATE TABLE IF NOT EXISTS microsoft_sign_ins (
    staff_id            INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
    flow_id             UUID NOT NULL,
    device_code_sealed  BYTEA NOT NULL,
    user_code           VARCHAR(32) NOT NULL,
    verification_uri    VARCHAR(255) NOT NULL,
    interval_seconds    INTEGER NOT NULL,
    next_poll_at        TIMESTAMPTZ NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- What each month of the pilot budget cost. The meter itself forgets (Redis
-- counters expire, Prometheus gauges reset on deploy); this does not. Written
-- daily by the maintenance pass, frozen when the month rolls over. Also
-- created at startup by ensure_budget_history_schema.
CREATE TABLE IF NOT EXISTS budget_history (
    month             CHAR(7) PRIMARY KEY,
    input_tokens      BIGINT NOT NULL,
    output_tokens     BIGINT NOT NULL,
    cost_ceiling_usd  NUMERIC(10, 4) NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One lawyer's PJe access. The password is sealed with PJE_CRED_KEY, which is
-- a different key from the Microsoft one on purpose: a Microsoft token reads
-- somebody's files, this acts as the lawyer in court, and one leaking must not
-- open the other. Also created at startup by ensure_pje_schema.
CREATE TABLE IF NOT EXISTS pje_credentials (
    staff_id      INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
    cpf           VARCHAR(14) NOT NULL,
    password      BYTEA NOT NULL,
    court         VARCHAR(16) NOT NULL,
    enrolled_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ
);

-- The single-use link that takes a credential. What is stored is the SHA-256
-- of the token, never the token: the raw one exists only in the Telegram
-- message, so a leaked database does not hand over a live enrolment link.
CREATE TABLE IF NOT EXISTS pje_enrolments (
    token_digest  CHAR(64) PRIMARY KEY,
    staff_id      INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ
);

-- Whether an answer helped, according to the person who asked. Every other
-- measurement says what happened, not whether it was any good. Keyed on the
-- answer and the person because an opinion may be corrected, unlike a
-- decision. Also created at startup by ensure_feedback_schema.
CREATE TABLE IF NOT EXISTS answer_feedback (
    answer_ref  VARCHAR(64) NOT NULL,
    staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    chat_id     VARCHAR(64) NOT NULL,
    verdict     VARCHAR(16) NOT NULL,
    question    TEXT NOT NULL DEFAULT '',
    task        VARCHAR(32) NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (answer_ref, staff_id)
);
CREATE INDEX IF NOT EXISTS idx_answer_feedback_created
    ON answer_feedback(created_at DESC);

-- The web pages: an authenticator secret per person (sealed with WEB_TOTP_KEY,
-- since it has to be reproduced), recovery codes (hashed, since they only have
-- to be compared), and sessions as rows so they can be revoked. Also created
-- at startup by ensure_web_access_schema.
CREATE TABLE IF NOT EXISTS staff_totp (
    staff_id      INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
    secret        BYTEA NOT NULL,
    enrolled_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_counter  BIGINT,
    confirmed_at  TIMESTAMPTZ,
    failures      INTEGER NOT NULL DEFAULT 0,
    locked_until  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS staff_recovery_codes (
    staff_id     INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    code_digest  CHAR(64) NOT NULL,
    used_at      TIMESTAMPTZ,
    PRIMARY KEY (staff_id, code_digest)
);

-- A passkey (Face ID) per device, and the one-time challenge each ceremony
-- signs. A passkey is a credential, not a device: one iCloud passkey is on
-- every Apple device of that account. user_handle is random, one per person,
-- never the staff id. Also created at startup by ensure_web_access_schema.
CREATE TABLE IF NOT EXISTS staff_passkeys (
    id             SERIAL PRIMARY KEY,
    staff_id       INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    credential_id  BYTEA NOT NULL UNIQUE,
    public_key     BYTEA NOT NULL,
    sign_count     BIGINT NOT NULL DEFAULT 0,
    user_handle    BYTEA NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_staff_passkeys_staff ON staff_passkeys(staff_id);

-- The person's handle, drawn once and kept before any device is handed it.
CREATE TABLE IF NOT EXISTS staff_passkey_handles (
    staff_id     INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
    user_handle  BYTEA NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS webauthn_challenges (
    challenge_digest  CHAR(64) PRIMARY KEY,
    staff_id          INTEGER REFERENCES staff(id) ON DELETE CASCADE,
    purpose           TEXT NOT NULL,
    session_digest    CHAR(64),
    expires_at        TIMESTAMPTZ NOT NULL,
    used_at           TIMESTAMPTZ,
    CHECK (purpose IN ('register', 'signin', 'unlock'))
);

CREATE TABLE IF NOT EXISTS web_sessions (
    token_digest  CHAR(64) PRIMARY KEY,
    staff_id      INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    method        TEXT NOT NULL DEFAULT 'link' CHECK (method IN ('link', 'passkey')),
    passkey_id    INTEGER REFERENCES staff_passkeys(id) ON DELETE CASCADE,
    rule          TEXT NOT NULL DEFAULT 'browser',
    CONSTRAINT web_sessions_passkey_named CHECK (method = 'link' OR passkey_id IS NOT NULL),
    CONSTRAINT web_sessions_rule_allowed CHECK (rule IN ('browser', 'app')),
    CONSTRAINT web_sessions_app_rule_passkey CHECK (rule = 'browser' OR method = 'passkey')
);
CREATE INDEX IF NOT EXISTS idx_web_sessions_staff ON web_sessions(staff_id);

-- Where a notice for the installed app goes: the push service's address for
-- one device and the two keys its payload is encrypted to. passkey_id is the
-- passkey of the session that subscribed, so removing it stops that device's
-- notices. Stored in the clear: sending also needs the VAPID private key,
-- which is not in the database. Also created at startup by
-- ensure_web_access_schema.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          SERIAL PRIMARY KEY,
    staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    passkey_id  INTEGER NOT NULL REFERENCES staff_passkeys(id) ON DELETE CASCADE,
    endpoint    TEXT NOT NULL UNIQUE CHECK (length(endpoint) <= 1024),
    p256dh      BYTEA NOT NULL CHECK (octet_length(p256dh) = 65),
    auth        BYTEA NOT NULL CHECK (octet_length(auth) = 16),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_ok_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_staff ON push_subscriptions(staff_id);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_passkey ON push_subscriptions(passkey_id);

-- The first factor of the web login: a one-time link only the bot hands out,
-- to the Telegram account bound to the staff row. Stored hashed.
CREATE TABLE IF NOT EXISTS web_login_links (
    token_digest  CHAR(64) PRIMARY KEY,
    staff_id      INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ
);

-- The firm's own days without court business: Pará's and Belém's holidays,
-- the TJPA's closures, suspensions by portaria. The national holidays are not
-- here; core/services/deadlines.py computes them. Read by /prazo, changed by
-- /feriado (admin). Also created at startup by ensure_firm_holidays_schema.
CREATE TABLE IF NOT EXISTS firm_holidays (
    day        DATE PRIMARY KEY,
    reason     TEXT NOT NULL,
    added_by   INTEGER REFERENCES staff(id) ON DELETE SET NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- What each person said to the bot and what it answered: the router reads the
-- last two hours, the web panel will show it. Each person reads only their own;
-- kept query_retention_days. Also created at startup by
-- ensure_conversation_schema.
CREATE TABLE IF NOT EXISTS conversation_turns (
    id         BIGSERIAL PRIMARY KEY,
    staff_id   INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    chat_id    VARCHAR(50) NOT NULL,
    speaker    VARCHAR(8) NOT NULL CHECK (speaker IN ('person', 'bot')),
    text       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    channel    VARCHAR(8) NOT NULL DEFAULT 'telegram',
    extras     JSONB,
    request_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_conversation_turns_staff_time
    ON conversation_turns(staff_id, created_at DESC);

-- A message typed in the web panel, before and while the worker answers it:
-- it runs at most once (the worker starts it with a conditional UPDATE), one
-- at a time per person (the partial unique index), and the page polls it.
-- Also created at startup by ensure_panel_schema.
CREATE TABLE IF NOT EXISTS panel_requests (
    id             UUID PRIMARY KEY,
    staff_id       INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    chat_id        VARCHAR(50) NOT NULL,
    kind           VARCHAR(8) NOT NULL,
    person_turn_id BIGINT,
    status         VARCHAR(10) NOT NULL DEFAULT 'sending',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    started_at     TIMESTAMPTZ,
    -- Set when the worker starts reading an attachment: until this moment
    -- plus the running grace, the request is not given up on.
    media_deadline_at TIMESTAMPTZ,
    CONSTRAINT panel_requests_kind_allowed CHECK (kind IN ('message', 'button')),
    CONSTRAINT panel_requests_status_allowed CHECK (status IN (
        'sending', 'queued', 'deferred', 'running', 'done', 'failed', 'withdrawn'))
);
CREATE UNIQUE INDEX IF NOT EXISTS panel_requests_one_active
    ON panel_requests(staff_id)
    WHERE status IN ('sending', 'queued', 'deferred', 'running');
CREATE INDEX IF NOT EXISTS idx_panel_requests_staff_time
    ON panel_requests(staff_id, created_at DESC);

-- A file the panel offers (an export), sealed with the documents' key and
-- read only by its owner before it expires. Originals are never copied here.
-- Also created at startup by ensure_panel_schema.
CREATE TABLE IF NOT EXISTS panel_files (
    token       TEXT PRIMARY KEY,
    staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    sealed      BYTEA NOT NULL,
    wrapped_key BYTEA NOT NULL,
    content_sha TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_panel_files_staff_expiry
    ON panel_files(staff_id, expires_at);

-- An attachment sent in the panel's chat (a photo, a voice note, an audio
-- file), sealed with the documents' key until the worker reads it. Unattached
-- it lives 15 minutes; attached, until its request ends and at most 24 hours.
-- Never served back. Also created at startup by ensure_panel_schema.
CREATE TABLE IF NOT EXISTS panel_uploads (
    id          UUID PRIMARY KEY,
    staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    request_id  UUID REFERENCES panel_requests(id) ON DELETE CASCADE,
    kind        VARCHAR(8) NOT NULL,
    media_type  TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    sealed      BYTEA NOT NULL,
    wrapped_key BYTEA NOT NULL,
    content_sha TEXT NOT NULL,
    archive     BOOLEAN NOT NULL DEFAULT FALSE,
    filename    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    CONSTRAINT panel_uploads_kind_allowed
        CHECK (kind IN ('photo', 'voice', 'audio', 'document')),
    CONSTRAINT panel_uploads_document_named CHECK ((kind = 'document') = (filename IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS panel_uploads_one_per_request
    ON panel_uploads(request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_panel_uploads_staff_expiry
    ON panel_uploads(staff_id, expires_at);

-- The cases a person follows (alone or with a team), their deadlines, and
-- who was told about each. Also created at startup by ensure_process_schema.
CREATE TABLE IF NOT EXISTS tracked_processes (
    id              SERIAL PRIMARY KEY,
    process_number  VARCHAR(20) NOT NULL,
    client          VARCHAR(160),
    note            VARCHAR(500),
    visibility      TEXT NOT NULL CHECK (visibility IN ('private', 'team')),
    team_id         INTEGER REFERENCES teams(id),
    created_by      INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at     TIMESTAMPTZ,
    CHECK ((visibility = 'team') = (team_id IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tracked_private ON tracked_processes
    (created_by, process_number) WHERE visibility = 'private' AND archived_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tracked_team ON tracked_processes
    (team_id, process_number) WHERE visibility = 'team' AND archived_at IS NULL;

CREATE TABLE IF NOT EXISTS process_deadlines (
    id          SERIAL PRIMARY KEY,
    process_id  INTEGER NOT NULL REFERENCES tracked_processes(id) ON DELETE CASCADE,
    title       VARCHAR(120) NOT NULL,
    due_date    DATE NOT NULL,
    created_by  INTEGER REFERENCES staff(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    done_at     TIMESTAMPTZ,
    done_by     INTEGER REFERENCES staff(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_process_deadlines_open
    ON process_deadlines (due_date) WHERE done_at IS NULL;

CREATE TABLE IF NOT EXISTS deadline_alerts (
    deadline_id  INTEGER NOT NULL REFERENCES process_deadlines(id) ON DELETE CASCADE,
    staff_id     INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    threshold    SMALLINT NOT NULL CHECK (threshold IN (10, 5, 0)),
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (deadline_id, staff_id, threshold)
);
