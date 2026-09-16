-- 009_critique_strip.sql — which pixels the critic was actually looking at.
--
-- Until critic-v3 the critic read only words: the parent's prompt, the brief,
-- the gate's assertions, and the executor's own statement about what it built.
-- That statement is unverified self-description and on 2026-09-14 it was false
-- twice in one line, so the critique revised the statement rather than the
-- sketch. sketchgen/lineage.py:critique() now sends entries.strip_path — the
-- gate's four-frame strip — base64 in ollama's `images` field, and refuses
-- rather than critiquing an entry it cannot see.
--
-- These two columns are to a critique what judgments.artefact_hash is to a
-- verdict: strip_path is the file that was read and strip_sha256 is the sha256
-- of exactly the bytes that went to the model. Re-run the sketch, regenerate
-- its strip, and the hash stops matching — the old sentence is then visibly
-- about a picture that no longer exists, instead of silently about the wrong
-- one.
--
-- Both are nullable and both stay NULL on every row written under critic-v2:
-- those critiques were blind, and a backfilled hash would claim they were not.
-- ALTER TABLE ... ADD COLUMN is the whole migration, so an existing database on
-- the node upgrades in place the next time `sketchgen db init` runs
-- sketchgen/db.py:migrate().

ALTER TABLE critiques ADD COLUMN strip_path TEXT;
ALTER TABLE critiques ADD COLUMN strip_sha256 TEXT;
