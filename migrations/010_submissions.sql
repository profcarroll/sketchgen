-- 010_submissions.sql — what the public asked for, before a person released it.
--
-- A submission is not a job. It is a sentence a signed-in visitor typed on the
-- gallery, pulled down from the write path, and it becomes a job only when the
-- operator releases it. Keeping it out of `jobs` is what guarantees the worker
-- cannot claim unreviewed text: claim_next reads `jobs` and this is not it.
--
-- `state` is this table's own and has nothing to do with jobs.state:
--   pending   — waiting for a person
--   released  — became job_id
--   declined  — a person said no; decline_reason says why, and the row stays
--
-- `remote_id` is the write path's own id. It is UNIQUE because /pull is
-- at-least-once: the same row arrives again on the boundary second and the
-- upsert must recognise it rather than queue the prompt twice.

CREATE TABLE IF NOT EXISTS submissions (
    id             INTEGER PRIMARY KEY,
    remote_id      INTEGER NOT NULL UNIQUE,
    kind           TEXT NOT NULL CHECK (kind IN ('prompt', 'critique')),
    username       TEXT NOT NULL,            -- GitHub login, nothing else
    entry_id       INTEGER REFERENCES entries(id),
    text           TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'released', 'declined')),
    job_id         INTEGER REFERENCES jobs(id),
    decline_reason TEXT,
    created_utc    TEXT NOT NULL,            -- when the visitor submitted
    pulled_utc     TEXT NOT NULL,            -- when this node first saw it
    decided_utc    TEXT
);

CREATE INDEX IF NOT EXISTS submissions_state_idx ON submissions (state, created_utc, id);
