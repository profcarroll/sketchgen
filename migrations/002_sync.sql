-- 002_sync.sql — where `sketchgen sync` keeps its place in the write path's log.
--
-- One row per cursor. Today there is exactly one key, 'writepath_since', holding
-- the UTC ISO 8601 watermark the next /pull asks for. It is a table rather than a
-- file so that the watermark moves in the same database as the rows it describes:
-- a restored database and its cursor can never disagree.

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
