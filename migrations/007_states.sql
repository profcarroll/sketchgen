-- 007_states.sql — the operator's reason for a rejection, and the archived state.
--
-- Two changes, both to `entries`, both from the lineage ledger's packet 2
-- (docs/plans/lineage-ledger.md §5).
--
-- 1. `reject_reason`. Rejecting an entry now publishes it to the rejections
--    catalog with the reason a person typed, so that reason is part of the
--    record and has to live in a column of its own. It was previously written
--    into the originating job's `last_error`, which is the executor's field:
--    overloading it meant a repair message and an operator's sentence were the
--    same string, and the page could not tell which it was holding. The
--    backfill (`sketchgen publish-rejected`) copies the old `last_error` text
--    into this column where it reads as an operator's reason.
--
-- 2. `archived` joins the state CHECK. It is the terminal state for a held
--    entry, or a kept failure nobody published, that the operator wants off
--    their primary lists. Nothing is deleted: the entry row, its attempt
--    directories and its strip all stay exactly where they were. `archived` is
--    not a public state, so an archived entry has no page under `e/`.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt: new table,
-- copy, drop the old one, rename the new one into its place. Other tables keep
-- saying `REFERENCES entries` throughout — nothing renames a table they point
-- at by that name, so no other schema is touched — and the new table arrives
-- under it before anything reads it again.
--
-- `sketchgen/db.py:migrate` runs every migration with `PRAGMA foreign_keys`
-- off, which is the first line of SQLite's own procedure for this and is what
-- the DROP below needs: dropping a parent table counts one deferred violation
-- per child row and re-creating the parent does not count them back down, so
-- with foreign keys on the COMMIT is refused even though nothing dangles.
-- `migrate` runs `PRAGMA foreign_key_check` afterwards, so a migration that
-- really did leave a dangling reference still fails loudly.
--
-- The column list below is 001's, in 001's order, plus the one new column at
-- the end; keeping the order means `SELECT *` and the row factory see what
-- they saw before.

CREATE TABLE entries_007 (
    id                       INTEGER PRIMARY KEY,
    job_id                   INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    state                    TEXT NOT NULL CHECK (state IN (
                                 'held', 'published', 'rejected', 'failed-kept',
                                 'archived')),
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
    shape                    TEXT,
    seed                     INTEGER,
    parent_entry_id          INTEGER REFERENCES entries(id),
    submitted_by             TEXT,
    source_dir               TEXT,
    strip_path               TEXT,
    png_path                 TEXT,
    published_utc            TEXT,
    publish_commit           TEXT,
    created_utc              TEXT NOT NULL,
    reject_reason            TEXT             -- why a person rejected it
);

INSERT INTO entries_007 (
    id, job_id, state, prompt, brief, statement, planner,
    planner_prompt_version, executor, executor_prompt_version, rules_file,
    assertions_json, attempts, prompt_tokens, completion_tokens, wall_s, shape,
    seed, parent_entry_id, submitted_by, source_dir, strip_path, png_path,
    published_utc, publish_commit, created_utc
)
SELECT
    id, job_id, state, prompt, brief, statement, planner,
    planner_prompt_version, executor, executor_prompt_version, rules_file,
    assertions_json, attempts, prompt_tokens, completion_tokens, wall_s, shape,
    seed, parent_entry_id, submitted_by, source_dir, strip_path, png_path,
    published_utc, publish_commit, created_utc
FROM entries;

DROP TABLE entries;

ALTER TABLE entries_007 RENAME TO entries;

CREATE INDEX IF NOT EXISTS entries_state_idx ON entries (state, created_utc, id);
