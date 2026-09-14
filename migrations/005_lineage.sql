-- 005_lineage.sql — the pending half of a lineage link, carried on the job.
--
-- A critique spawns a child JOB (sketchgen/lineage.py:spawn), but the `lineage`
-- table in 001 keys on the child ENTRY, and that entry does not exist until the
-- worker has finished the job — minutes later, or never, if the job fails
-- before it produces anything. So the link waits here, on the job row, and
-- worker.py copies it into `lineage` at the moment it creates the entry. One
-- place holds the pending link, one place holds the recorded one, and neither
-- has to guess about the other.
--
-- 004 belongs to packet 5.1 (pairs); this is 005 so the two land in either
-- order.
--
-- critique_by is a model id (`gemma4:e4b`) or a GitHub username, and nothing
-- else: the no-personal-data rule applies to this column as hard as it does to
-- jobs.submitted_by. sketchgen/lineage.py:CRITIQUE_BY_RE is what enforces it.

ALTER TABLE jobs ADD COLUMN critique TEXT;
ALTER TABLE jobs ADD COLUMN critique_by TEXT;
