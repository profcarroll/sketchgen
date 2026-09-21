-- 015_process_cost.sql — what the work around the reply cost, said by the
-- agent that did it.
--
-- The node meters what it runs. For an off-node step it measures the round
-- trip (`wall_s`, export to import) and records the token counts the agent
-- reports of its reply (`usage`), and both are true. Job 1286 — entry 1279,
-- 2026-09-21 — shows what they leave out: the node saw a 111 s round trip,
-- and the 37 minutes and 193,000 generated tokens the agent spent before
-- `paid start`, building a way to look at and time its sketch, were nowhere.
-- Read beside a local model's decode time, that entry says the sketch cost
-- under two minutes to write.
--
-- 012_harness_version.sql made this argument about the referee: what judged
-- an entry was not recorded, so a comparison across the day it changed would
-- read one distribution as two. The same holds here for how an entry was
-- made. Entry 1279 used a skill and a local prototype; a plain paid run uses
-- neither; the corpus cannot tell them apart, and the difference is larger
-- than either variable the A/B sets on purpose.
--
-- So five nullable columns, additive, no CHECK to rebuild:
--
--   jobs.since_utc       when the agent says the task began — declared by it
--                        (`paid start --since`), never inferred here. The
--                        node's clock cannot see a session it did not start.
--   jobs.note            free text from `paid start --note`: a skill, a local
--                        prototype, anything the node cannot see.
--   attempts.process_json  {session_s, output_tokens, thinking_tokens,
--                        tool_calls, screenshots, effort, tries} — every field
--                        nullable, from the item's `process` at import, with
--                        `tries` filled by the node from the try record
--                        (014's packet 7): a first-attempt pass after four
--                        tries is not a first-attempt pass.
--   entries.note, entries.process_json  copied at _create_entry from the job
--                        and the attempt the entry kept, while the worker
--                        still knows which attempt counted, the way every
--                        other entry column is filled.
--
-- NULL is every row written before this column existed, and every row a local
-- model made: nobody reported anything, and that is a fact about them rather
-- than missing data. Nothing sums these columns into a batch total, and the
-- judge never sees them — process cost sits beside the entry and stays out of
-- every measure (docs/plans/agent-rig.md §5.3).

ALTER TABLE jobs ADD COLUMN since_utc TEXT;
ALTER TABLE jobs ADD COLUMN note TEXT;
ALTER TABLE attempts ADD COLUMN process_json TEXT;
ALTER TABLE entries ADD COLUMN note TEXT;
ALTER TABLE entries ADD COLUMN process_json TEXT;
