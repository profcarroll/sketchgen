-- schema.sql — the D1 (SQLite) tables behind the gallery write path.
--
-- Apply with:  wrangler d1 execute sketchgen-writepath --remote --file=./schema.sql
-- Safe to rerun: every statement is IF NOT EXISTS.
--
-- All timestamps are UTC, ISO 8601 with a trailing Z, second resolution, which
-- is what the node's app database uses too (migrations/001_init.sql).
--
-- The only thing here that identifies a person is `username`, a GitHub login.
-- No email, no display name, no avatar URL, no IP address, no request headers.
-- The OAuth access token is used once during /callback and never written down.

-- One row per (judge, pair, question). A later vote on the same key updates
-- this row rather than adding another, which is the same constraint the app
-- database enforces with UNIQUE (judge_kind, judge_id, entry_a, entry_b, question).
CREATE TABLE IF NOT EXISTS votes (
    username    TEXT    NOT NULL,           -- GitHub login, nothing else
    entry_a     INTEGER NOT NULL,
    entry_b     INTEGER NOT NULL,
    question    TEXT    NOT NULL CHECK (question IN ('brief', 'look')),
    choice      TEXT    NOT NULL CHECK (choice IN ('A', 'B', 'tie')),
    created_utc TEXT    NOT NULL,           -- when this judge first answered
    updated_utc TEXT    NOT NULL,           -- when it last changed; the pull watermark
    PRIMARY KEY (username, entry_a, entry_b, question)
);

CREATE INDEX IF NOT EXISTS votes_updated_idx ON votes (updated_utc);

-- Likes. `active` is an extension to the row shape the packet named, and it is
-- there for one reason: an unlike has to be pullable. If "off" deleted the row,
-- /pull could never tell the node that a like went away, and the node's count
-- would drift upward for ever. So "off" sets active = 0 and stamps updated_utc;
-- sync.py deletes the matching row in the app database.
CREATE TABLE IF NOT EXISTS likes (
    entry_id    INTEGER NOT NULL,
    username    TEXT    NOT NULL,           -- GitHub login, nothing else
    active      INTEGER NOT NULL DEFAULT 1, -- 1 = liked, 0 = unliked
    created_utc TEXT    NOT NULL,
    updated_utc TEXT    NOT NULL,
    PRIMARY KEY (entry_id, username)
);

CREATE INDEX IF NOT EXISTS likes_updated_idx ON likes (updated_utc);

-- View counters. One row per entry, an absolute running count.
--
-- `count` is every view, whatever asked for it, and it is the only number
-- /counts and /pull ever report: the gallery shows one figure and the node
-- mirrors one column. `kiosk_count` is the subset of `count` that arrived from
-- the projector page (docs/plans/kiosk-views.md §3.4), kept so that the two
-- can still be told apart when the term is written up. Nothing reads it yet;
-- that is the point of writing it now rather than later.
--
-- An existing database does not pick this column up from a CREATE IF NOT
-- EXISTS. It is added by hand, once, before the Worker that writes it deploys:
--   ALTER TABLE views ADD COLUMN kiosk_count INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS views (
    entry_id    INTEGER PRIMARY KEY,
    count       INTEGER NOT NULL DEFAULT 0,
    kiosk_count INTEGER NOT NULL DEFAULT 0,
    updated_utc TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS views_updated_idx ON views (updated_utc);

-- The 60 s de-duplication window for /view. `session_hash` is SHA-256 of the
-- session cookie, hex — never the cookie itself, so this table cannot be
-- replayed into a session even if it leaks. Signed-out viewers have no session
-- and therefore no row here: anonymous views are NOT de-duplicated, on purpose,
-- because de-duplicating them would mean keeping an IP or a fingerprint.
CREATE TABLE IF NOT EXISTS view_log (
    session_hash TEXT    NOT NULL,
    entry_id     INTEGER NOT NULL,
    seen_utc     TEXT    NOT NULL,
    PRIMARY KEY (session_hash, entry_id)
);

-- Short-lived OAuth state values, consumed by /callback and swept by age.
CREATE TABLE IF NOT EXISTS oauth_state (
    state       TEXT PRIMARY KEY,
    created_utc TEXT NOT NULL
);

-- One row per thing a signed-in visitor asked for, prompt or critique. Written
-- once and never updated; `updated_utc` mirrors `created_utc` so a submission
-- rides the same inclusive /pull watermark as a vote.
--
-- A submission is not a job. It is text in this table until the operator
-- releases it on the node, which is what guarantees nothing the public types
-- can reach a model by accident.
--
-- The day's budget is counted straight off this table — COUNT(*) by username,
-- kind and the start of the UTC day — so there is no counter to drift and no
-- second table to keep in step. That is what submissions_budget_idx serves.
CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL CHECK (kind IN ('prompt', 'critique')),
    username    TEXT    NOT NULL,           -- GitHub login, nothing else
    entry_id    INTEGER,                    -- the parent, for a critique; NULL for a prompt
    text        TEXT    NOT NULL,
    created_utc TEXT    NOT NULL,
    updated_utc TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS submissions_updated_idx ON submissions (updated_utc);
CREATE INDEX IF NOT EXISTS submissions_budget_idx  ON submissions (username, kind, created_utc);
