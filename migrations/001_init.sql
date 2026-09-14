-- 001_init.sql — the schema the whole system lives in.
-- Applied by sketchgen/db.py:migrate() inside one transaction. Safe to rerun:
-- every statement is IF NOT EXISTS or INSERT OR IGNORE, and the runner skips
-- migrations already recorded in schema_version.
--
-- All timestamps are UTC, ISO 8601 with a trailing Z; the column names say so.

-- One row per job, from the prompt to a terminal state. The legal transitions
-- between `state` values are enforced in db.py, not here.
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY,
    state            TEXT NOT NULL CHECK (state IN (
                         'queued', 'planning', 'executing', 'gating',
                         'repairing', 'needs-laptop', 'held', 'published',
                         'rejected', 'failed')),
    prompt           TEXT NOT NULL,
    brief            TEXT,
    assertions_json  TEXT,                    -- JSON list of vocabulary words
    planner          TEXT,                    -- model id, or 'paid'
    executor         TEXT,                    -- model id
    rules_file       TEXT CHECK (rules_file IS NULL OR rules_file IN (
                         'control', 'treatment', 'random')),
                                              -- 'random' resolves to control or
                                              -- treatment at execute time and the
                                              -- resolved value lands on the attempt
    submitted_by     TEXT NOT NULL,           -- GitHub username, nothing else
    parent_entry_id  INTEGER REFERENCES entries(id),
    publication      TEXT NOT NULL DEFAULT 'hold'
                         CHECK (publication IN ('hold', 'auto')),
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    created_utc      TEXT NOT NULL,
    updated_utc      TEXT NOT NULL,
    needs            TEXT CHECK (needs IS NULL OR needs IN (
                         'plan', 'repair', 'review')),
                                              -- set while in needs-laptop, else NULL
    last_error       TEXT
);

CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs (state, created_utc, id);

-- One row per execute+gate attempt.
CREATE TABLE IF NOT EXISTS attempts (
    id                INTEGER PRIMARY KEY,
    job_id            INTEGER NOT NULL REFERENCES jobs(id),
    n                 INTEGER NOT NULL,       -- 1-based attempt number
    started_utc       TEXT,
    finished_utc      TEXT,
    model             TEXT,
    rules_file        TEXT,                   -- resolved: 'control' or 'treatment'
    prompt_version    TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    prefill_s         REAL,
    decode_s          REAL,
    wall_s            REAL,
    source_dir        TEXT,
    gate_exit         INTEGER,
    gate_report_path  TEXT,
    evidence          TEXT,                   -- the text fed back to REPAIR
    statement         TEXT,                   -- the executor's own commentary, unedited
    UNIQUE (job_id, n)
);

CREATE INDEX IF NOT EXISTS attempts_job_idx ON attempts (job_id, n);

-- One row per job that reached a gallery-visible outcome.
CREATE TABLE IF NOT EXISTS entries (
    id                       INTEGER PRIMARY KEY,
    job_id                   INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    state                    TEXT NOT NULL CHECK (state IN (
                                 'held', 'published', 'rejected', 'failed-kept')),
    prompt                   TEXT,
    brief                    TEXT,
    statement                TEXT,
    planner                  TEXT,
    planner_prompt_version   TEXT,
    executor                 TEXT,
    executor_prompt_version  TEXT,
    rules_file               TEXT,
    assertions_json          TEXT,
    attempts                 INTEGER,
    prompt_tokens            INTEGER,
    completion_tokens        INTEGER,
    wall_s                   REAL,
    shape                    TEXT,            -- e.g. 'VM.Standard.A1.Flex 16/96'
    seed                     INTEGER,
    parent_entry_id          INTEGER REFERENCES entries(id),
    submitted_by             TEXT,            -- GitHub username only
    source_dir               TEXT,
    strip_path               TEXT,
    png_path                 TEXT,
    published_utc            TEXT,
    publish_commit           TEXT,
    created_utc              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS entries_state_idx ON entries (state, created_utc, id);

-- Paired comparisons. One row is one judge's answer to one question about one pair.
CREATE TABLE IF NOT EXISTS judgments (
    id             INTEGER PRIMARY KEY,
    entry_a        INTEGER NOT NULL REFERENCES entries(id),
    entry_b        INTEGER NOT NULL REFERENCES entries(id),
    judge_kind     TEXT NOT NULL CHECK (judge_kind IN ('human', 'agent')),
    judge_id       TEXT NOT NULL,             -- GitHub username, or a model id
    question       TEXT NOT NULL CHECK (question IN ('brief', 'look')),
    choice         TEXT NOT NULL CHECK (choice IN ('A', 'B', 'tie')),
    prompt_version TEXT,
    artefact_hash  TEXT,
    created_utc    TEXT NOT NULL,
    -- a judge answers each (pair, question) once
    UNIQUE (judge_kind, judge_id, entry_a, entry_b, question)
);

-- Which prompt begat which, through which critique.
CREATE TABLE IF NOT EXISTS lineage (
    child_entry_id  INTEGER PRIMARY KEY REFERENCES entries(id),
    parent_entry_id INTEGER REFERENCES entries(id),
    generation      INTEGER NOT NULL DEFAULT 1,
    critique_by     TEXT,                     -- model id, or a GitHub username
    critique        TEXT,
    created_utc     TEXT NOT NULL
);

-- Engagement, kept apart from judgment on purpose (spec §5).
CREATE TABLE IF NOT EXISTS engagement (
    entry_id INTEGER PRIMARY KEY REFERENCES entries(id),
    views    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS likes (
    entry_id    INTEGER NOT NULL REFERENCES entries(id),
    username    TEXT NOT NULL,                -- GitHub username only
    created_utc TEXT NOT NULL,
    PRIMARY KEY (entry_id, username)
);

-- The operator's one switch, read by the worker. Singleton by construction.
CREATE TABLE IF NOT EXISTS control (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    state       TEXT NOT NULL CHECK (state IN ('running', 'pausing', 'paused')),
    reason      TEXT,
    updated_utc TEXT NOT NULL
);

INSERT OR IGNORE INTO control (id, state, reason, updated_utc)
VALUES (1, 'running', NULL, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
