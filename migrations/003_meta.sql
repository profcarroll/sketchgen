-- 003_meta.sql — a two-column scratchpad for facts that belong to the
-- installation rather than to any job.
--
-- Applied by sketchgen/db.py:migrate(). There is no 002: packet 2.3 decided the
-- worker's pause/stop switch needed no schema change and said so in
-- sketchgen/worker.py's docstring, so the number was never used.
--
-- The only key written today is ``worker_started_utc``, stamped by the resident
-- worker at startup (worker.py, run_forever). The console's "session" column is
-- everything since that stamp; with no row, session and total are the same
-- number and the console says the session began at null.
--
-- Values are TEXT. A key holding a timestamp is UTC, ISO 8601 with a trailing Z,
-- like every other timestamp in this schema.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
