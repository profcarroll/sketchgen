-- 019_origins.sql — entries made on another node, and which node that was.
--
-- The rented A10 (`sld-gpu`) made 3,518 sketches between 2026-09-23 and 09-27
-- and published none; it was terminated on the 27th, and its whole ~/sketchgen
-- survives as an archive on sld-cloud. `sketchgen import run` brings entries
-- of such an archive into this node's tables (docs/plans/gpu-fold-in.md,
-- Packet 1). `entries.shape` already says *an A10*; nothing said *sld-gpu's
-- entry 412, brought here from snapshot 2026-09-27T135015Z*. That is a
-- provenance, and these are its columns.
--
--   origins            one row per node entries were imported from, written by
--                      `import run` the first time it sees the name: what the
--                      node was (shape, its first and last job, the checkout
--                      its snapshot's manifest names) and which snapshot.
--   entries.origin_*   the node, and the entry's and job's ids *there*. An
--                      imported entry takes a new id from this node's sequence,
--                      because `e/<id>/` is the public URL and the archive's
--                      1–3518 sit on top of this node's own; the old ids are
--                      kept beside it, and they are what a `report.json` inside
--                      the copied job directory still names.
--   imported_utc       when it arrived. `created_utc` stays the archive's: the
--                      sketch was made when it was made.
--
-- NULL on every existing row, which is true: they were made here. Additive,
-- nullable, no CHECK to rebuild (AGENTS.md, "Working on the code"). The unique
-- index is what makes a second import of the same entry a no-op rather than a
-- duplicate. The rules-file A/B reads `WHERE origin_node IS NULL` (§1.6).

CREATE TABLE IF NOT EXISTS origins (
    node            TEXT PRIMARY KEY,   -- 'sld-gpu': the name it had in the fleet
    shape           TEXT,               -- as its entries say
    first_utc       TEXT,               -- its first and last job
    last_utc        TEXT,
    build           TEXT,               -- its checkout, from the snapshot's manifest
    snapshot        TEXT,               -- '2026-09-27T135015Z'
    snapshot_sha256 TEXT,               -- of the snapshot's sketchgen.db
    note            TEXT,               -- 'rented OCI A10, terminated 2026-09-27'
    registered_by   TEXT NOT NULL,
    registered_utc  TEXT NOT NULL
);

ALTER TABLE entries ADD COLUMN origin_node     TEXT REFERENCES origins(node);
ALTER TABLE entries ADD COLUMN origin_entry_id INTEGER;
ALTER TABLE entries ADD COLUMN origin_job_id   INTEGER;
ALTER TABLE entries ADD COLUMN imported_utc    TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS entries_origin_idx
    ON entries (origin_node, origin_entry_id) WHERE origin_node IS NOT NULL;
