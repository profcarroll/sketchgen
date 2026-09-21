-- 014_needs_execute.sql — a job can wait for the laptop to write its sketch.
--
-- Packet 4 of docs/plans/agentic-cli.md, the paid executor. `needs` says what
-- a job parked at `needs-laptop` is waiting for, and until now it could say
-- 'plan', 'repair' or 'review' and not 'execute': there was no state for a job
-- whose sketch is written by a model that is not on this node. The worker now
-- parks such a job at `needs-laptop` with `needs='execute'` once per attempt,
-- `sketchgen paid import` writes the reply into that attempt's directory and
-- puts the job back on the queue, and the worker gates it here as it gates
-- every sketch. The gate stays on the node; only the writing moves.
--
-- 'repair' is not reused for this, though a second attempt is a repair: the
-- first attempt is not, and a value that is true of some attempts and false of
-- others would make the column mean less than it does.
--
-- SQLite cannot alter a CHECK constraint, so `jobs` is rebuilt, exactly as
-- 007_states.sql rebuilt `entries` — read its header for why `migrate` runs
-- with `PRAGMA foreign_keys` off (attempts, entries, critiques, activity and
-- submissions all reference `jobs`, and a DROP of a parent counts violations a
-- re-create does not count back down) and why it runs `foreign_key_check`
-- afterwards.
--
-- The column list is the table as it stands: 001's columns in 001's order,
-- then 005's two lineage columns at the end, where `ALTER TABLE ADD COLUMN`
-- put them. Keeping the order means `SELECT *` and the row factory see what
-- they saw before. The one change is the word 'execute' in the CHECK.

CREATE TABLE jobs_014 (
    id               INTEGER PRIMARY KEY,
    state            TEXT NOT NULL CHECK (state IN (
                         'queued', 'planning', 'executing', 'gating',
                         'repairing', 'needs-laptop', 'held', 'published',
                         'rejected', 'failed')),
    prompt           TEXT NOT NULL,
    brief            TEXT,
    assertions_json  TEXT,                    -- JSON list of vocabulary words
    planner          TEXT,                    -- model id, or 'paid'
    executor         TEXT,                    -- model id, or 'paid'
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
                         'plan', 'execute', 'repair', 'review')),
                                              -- set while in needs-laptop, else NULL
    last_error       TEXT,
    critique         TEXT,
    critique_by      TEXT
);

INSERT INTO jobs_014 (
    id, state, prompt, brief, assertions_json, planner, executor, rules_file,
    submitted_by, parent_entry_id, publication, max_attempts, created_utc,
    updated_utc, needs, last_error, critique, critique_by
)
SELECT
    id, state, prompt, brief, assertions_json, planner, executor, rules_file,
    submitted_by, parent_entry_id, publication, max_attempts, created_utc,
    updated_utc, needs, last_error, critique, critique_by
FROM jobs;

DROP TABLE jobs;

ALTER TABLE jobs_014 RENAME TO jobs;

CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs (state, created_utc, id);
