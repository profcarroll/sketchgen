-- 006_critiques.sql — what the critic said about a published entry, once.
--
-- The worker does bounded idle work when the queue is empty (packet 5.4): it
-- judges a pair, then critiques a published entry and spawns the child that
-- critique asks for. This table is the record of the second half, and it is
-- also the *bookkeeping* that stops the worker critiquing the same entry on
-- every idle round for the rest of the semester.
--
-- One row per (entry, prompt_version). An entry is critiqued at most once under
-- a given version of prompts/critic.md; editing that prompt bumps its
-- prompt_version (lineage.py reads it from the file, never from code) and the
-- gallery may critique the entry again under the new one. That is the only way
-- an entry is ever revisited, and it is deliberate: a second opinion from the
-- same prompt is the same opinion.
--
-- spawned_job_id is the child job lineage.spawn() queued, or NULL when nothing
-- was queued — because the critique failed lineage.py's one-sentence validator
-- (rejected_reason says which rule), or because the parent turned out not to be
-- spawnable. A rejected row is kept rather than deleted: it is the evidence of
-- what the critic actually said, and it is what stops the entry being retried.
--
-- critique_by is a model id (`gemma4:e4b`) or a GitHub username and nothing
-- else, the same rule as jobs.critique_by in 005. Timestamps are UTC, ISO 8601
-- with a trailing Z.

CREATE TABLE IF NOT EXISTS critiques (
    id             INTEGER PRIMARY KEY,
    entry_id       INTEGER NOT NULL REFERENCES entries(id),
    critique       TEXT,                      -- the sentence, or the raw text
                                              -- that failed validation
    critique_by    TEXT,                      -- model id or GitHub username
    prompt_version TEXT NOT NULL,             -- from prompts/critic.md
    spawned_job_id INTEGER REFERENCES jobs(id),
    rejected_reason TEXT,                     -- NULL when it spawned a child
    created_utc    TEXT NOT NULL,
    UNIQUE (entry_id, prompt_version)
);

CREATE INDEX IF NOT EXISTS critiques_entry_idx ON critiques (entry_id);
CREATE INDEX IF NOT EXISTS critiques_created_idx ON critiques (created_utc);
