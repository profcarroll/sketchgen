-- 008_activity.sql — what the worker is doing, in a place another process can read.
--
-- The operator UI (sketchgen-web.service) and the worker (sketchgen-worker.service)
-- are two processes that share one WAL-mode SQLite file and nothing else. Until
-- now the worker said everything it was doing with `self.log()` into
-- `jobs/<id>/job.log` — which the web server can only tail once it knows which
-- job to look at — and its idle work (judging, critiquing, the nap) wrote to no
-- job log at all. So the Console could say a job was `executing` and not one
-- word about what that meant, and an idle worker looked exactly like a stopped
-- one. This table is the channel: one row per step, written by the worker at
-- the boundaries it already has, read by console.activity().
--
-- `step` is the worker's own vocabulary and is deliberately NOT the job state
-- machine's. `jobs.state` still reads `executing`, `gating`, `repairing` and
-- its CHECK constraint is untouched; the operator reads `writing`,
-- `evaluating`, `correcting`. Two vocabularies on purpose: one names the row a
-- transition is allowed to move to, the other names what a person would say is
-- happening. `headline` and `detail` are already English — the sentence is
-- composed where the facts are, by the worker, rather than by a template that
-- would have to re-derive them.
--
-- `pid` is how liveness is known. The worker blocks inside one non-streaming
-- HTTP call for a whole step (about 67 s for a sketch), so it cannot tick a
-- heartbeat mid-step, and it is not being given a thread to do it with: there
-- is one inference slot and one process that owns it. Liveness is therefore
-- the recorded pid plus `started_utc`, and progress is elapsed against the
-- median of the same step — see sketchgen/console.py:activity.
--
-- `ended_utc` NULL means the step is still running. Exactly one row per pid is
-- open at a time (db.begin_step closes the previous one), and the worker's nap
-- is a step like any other, so an open row exists for as long as the worker is
-- alive. A row left open by a pid that is gone is not tidied away: it is the
-- evidence of what the worker was doing when it stopped, and it is what the
-- card shows instead of a blank.
--
-- The table is pruned to db.ACTIVITY_KEEP rows on insert. It is a status
-- channel, not a transcript; the transcript is still job.log, and nothing here
-- replaces it.

CREATE TABLE IF NOT EXISTS activity (
    id           INTEGER PRIMARY KEY,
    step         TEXT NOT NULL,          -- the key from the vocabulary table
    headline     TEXT NOT NULL,          -- the sentence, already in English
    detail       TEXT,                   -- the line under it, or NULL
    job_id       INTEGER REFERENCES jobs(id),
    entry_id     INTEGER REFERENCES entries(id),
    model        TEXT,
    pid          INTEGER NOT NULL,       -- the worker process that wrote it
    started_utc  TEXT NOT NULL,
    ended_utc    TEXT                    -- NULL while the step is running
);

CREATE INDEX IF NOT EXISTS activity_recent_idx ON activity (id DESC);
