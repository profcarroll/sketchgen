-- 018_build.sql — the build each attempt and each plan ran on.
--
-- The hardware A/B memo of 2026-09-23 says both arms ran on a72f076. On
-- 2026-09-24 sld-cloud's reflog said otherwise: its checkout moved to 995a9d8
-- (#166, #167) at 05:23:40Z, with 7 of arm B's attempts started before that
-- and 141 after. The arm is clean — neither PR touches the generation path —
-- but showing it took the reflog, `attempts.started_utc` and a `git diff`, and
-- it only proved where the *checkout* was: whether the worker was restarted
-- onto it, nothing kept. With four nodes updated between batches
-- (docs/plans/fleet.md), "which build ran this" has to be a column, not an
-- archaeology.
--
--   attempts.build   the build the worker was *running* when it recorded the
--                    attempt: the full sha it started on, `-dirty` appended
--                    when its checkout had tracked changes (sketchgen/build.py
--                    `running`). Not the checkout at the time, which a pull
--                    without a restart makes a different thing. A paid
--                    attempt carries the node's build too: the node's gate
--                    judged it.
--   jobs.plan_build  the same, for the process that wrote the plan — the
--                    worker on the local path, `paid import` on the paid one.
--
-- NULL on every row before this migration, which is true: nobody recorded it.
-- Two nullable columns, additive, no CHECK to rebuild (AGENTS.md, "Working on
-- the code"). The A/B's question becomes one query:
--
--   SELECT build, COUNT(*) FROM attempts WHERE job_id BETWEEN ? AND ? GROUP BY build;

ALTER TABLE attempts ADD COLUMN build TEXT;
ALTER TABLE jobs ADD COLUMN plan_build TEXT;
