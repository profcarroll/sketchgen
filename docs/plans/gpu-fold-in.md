# Folding in the rented GPU's sketches

The rented A10 (`sld-gpu`) made 3,518 sketches between 2026-09-23 and 09-27 and published none
of them: it was a private node. It was terminated on 2026-09-27. The operator's question: can
those sketches go into the gallery `sld-cloud` publishes, so the kiosks play work made on the
GPU, with a record that stays clear and accurate about where each one came from? Or does that
wait for the gallery hub (`gallery-hub.md`)?

It does not wait for the hub. The hub is for nodes that publish *continuously*: push races, ids
issued while two generators run, pairs drawn across live databases. `sld-gpu` is gone. What is
left is a finished archive, and bringing a finished archive into one node's database is an
import: a verb, a migration, and a line on the page. sld-cloud then publishes the way it always
has.

Two packets. What bounds the work is not the engineering but GitHub Pages: there is room for a
selection, not for all of it (§1.2).

Repositories and conventions as in `held-batch.md`: `profcarroll/sketchgen`, Python 3.12, stdlib
only, `python3 -m unittest discover -s tests`, no network in a test. One packet per branch.

## 0. What is true today

Read on 2026-09-27, after the teardown.

**The archive.** `sld-cloud:~/sketchgen-backups/sld-gpu/` holds the node's whole `~/sketchgen`,
8.5 GB, 69,247 files, each one's sha256 compared with the node before it was terminated. Its
database is the snapshot `backups/2026-09-27T135015Z` (sha256 `20e2e95d…`, verified, taken after
the last job). The laptop has the snapshots and every job's text, and no images. **The copy on
sld-cloud is the only one with the frames**, and it is on a single instance, outside the path
`bin/pull-backup.sh` mirrors.

| in the archive | |
| --- | --- |
| entries | 3,518: 3,255 `held`, 263 `failed-kept` |
| held, clean pass / off-plan | 2,852 / 403 |
| distinct prompts among the held | 1,242, a median of 2 entries each, 15 at most |
| models | executor `qwen3-coder:30b` on 3,318, two unsloth Q5 quantisations on 100 each; planner `gemma4:e4b`, 100 with it on the CPU |
| rules file | 2,169 treatment, 1,349 control |
| prompts, gate | `executor-v3`, harness 4, build `a72f076`, on every one |
| `entries.shape` | `VM.GPU.A10.1 15/240 + A10 24G`, on every one |
| judgments, critiques, parents | none |

**sld-cloud.** Entries to id 1757 (1,288 published), jobs to 1770, schema 17, as the archive is.
Its checkout is `8956210` (#178), so it predates migration 018.

Four things follow.

1. **The hardware is already on the record, and already on the page.** `entries.shape` is
   copied into `meta.json` and printed as *Node shape*. An imported row would say A10 with no
   new code at all.
2. **The node is not.** Nothing in the schema says which node made an entry, or what it was
   called there. *An A10* is a shape; *sld-gpu's entry 412, build a72f076, brought here on the
   28th* is a provenance. That is the column this plan adds.
3. **The ids collide.** The archive's entries 1–3518 sit on top of sld-cloud's 1–1757, and
   `e/<id>/` is the public URL. An imported entry takes a new id from sld-cloud's own sequence
   and keeps its old one beside it.
4. **Nearly all of it is a second take.** The rental's queue was fed from sld-cloud's own
   prompts: 3,107 of the 3,255 share a prompt with an entry sld-cloud has already published
   (1,128 of the 1,242 distinct prompts). On a wall that plays both, the same prompt comes round
   twice. That is what these are, and the page should let a viewer see it (§3.1). It is **not**
   a hardware comparison: `gallery-hub.md` §0 says why (the originals are the winners of a
   harvest, under other prompt versions), and nothing here tries to make it one.

## 1. Decisions

### 1.1 An import, by a verb, into sld-cloud's own tables

`sketchgen import` reads a foreign snapshot read-only and writes jobs, attempts and entries
through `db.py`, as the worker does. No `sqlite3` by hand (AGENTS.md rule 4), and no second
gallery: an imported entry is an ordinary `held` entry from the moment it lands, and everything
downstream — the Held page, the personal-data scan, `publish`, the batch, the QR code, views
and likes on the Worker — treats it as one. The write-path Worker does not change.

### 1.2 A selection, because of Pages

The published tree is 349 MB for 1,515 entry folders, about 230 KB each since the WebP frames
(#164). `kiosk.json` is 4.0 MB. Pages caps a site at 1 GB.

| imported and published | tree | `kiosk.json` |
| --- | --- | --- |
| 200 | ~395 MB | ~4.6 MB |
| 1,000 | ~580 MB | ~7 MB |
| all 3,255 | ~1.1 GB — over the cap | ~14 MB |

**The ceiling on Pages is about 1,500 imported entries**, which leaves the tree near 700 MB
with room for sld-cloud's own. Past that is `gallery-hub.md` Step 2, not a bigger push. The
repository's `.git` is 1.4 GB already and every published frame is in it for good.

The first import is small — a couple of hundred — and goes on a wall before there is a
second. Whether GPU-made work earns more attention than what is already playing is something to
look at, not to assume (the 2026-09-24 refocus: the shortage is people looking, not sketches).

### 1.3 The operator chooses; the verb only lists

`import list` prints what is in the archive, with filters, and `import run` takes the ids it is
given. There is no rule in the code about which sketches are good. Imported entries land in
`held`, where a person publishes, as for anything else.

### 1.4 What is copied, and what is rewritten

Copied as written: prompt, brief, statement, assertions, models, prompt versions, rules file,
seed, every token count and timing, `harness_version`, `offplan_json`, `shape`, `submitted_by`,
and **`created_utc`** — the sketch was made when it was made. The job directory is copied byte
for byte, frames included, because the judge and the critic read the PNGs.

Rewritten: the three ids, and the five path columns (`entries.source_dir`, `strip_path`,
`png_path`; `attempts.source_dir`, `gate_report_path`), by replacing the archive's
`jobs/<old>` prefix with this node's `jobs/<new>`. Files inside the directory are not touched,
so a path printed inside a `report.json` still names the rental's job number; `origin_job_id`
is what explains it.

Not copied: `activity`, `billing_usage`, `meta`. And `attempts.build` stays NULL on an imported
attempt. `a72f076` is where the rental's *checkout* was, which is what `origins.build` records;
which build its worker was *running* nobody kept, and migration 018's column means exactly
that.

### 1.5 The idle critic leaves imported entries alone

`db.entries_to_critique` offers published entries oldest first, and these carry dates from the
23rd to the 27th: published, they would go to the head of the list and sld-cloud would spend
its idle rounds writing CPU-made children of GPU-made parents. The query gains `AND
e.origin_node IS NULL`. A person can still mark *Critique* on a card; that child is made here,
its own shape says so, and its lineage names its parent. What stops is the machine doing it
unasked.

### 1.6 The judge does not

Published, they join the pool `pairs.py` draws from, and a verdict may set a GPU entry beside a
CPU one. That is wanted: without standings an entry has no place in three of the kiosk's seven
orders (*most reviewed*, *controversial*, *consensus*). But it puts a second variable into the rules-file A/B the gallery measures, as a paid
executor does (AGENTS.md, *Other steps and settings*): **an analysis of that A/B reads `WHERE
origin_node IS NULL`**, and the column is what makes that one clause.

## 2. Packet 1 — `feat/import-entries`

### 2.1 Migration 019

Additive, nullable, no CHECK to rebuild:

```sql
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
```

NULL on every existing row, which is true: they were made here. The unique index is what makes
a second import of the same entry a no-op and not a duplicate.

### 2.2 `sketchgen import list`

```
$ sketchgen import list --from ~/sketchgen-backups/sld-gpu/sketchgen --clean --one-per-prompt
origin  prompt                               executor         rules      att  buffers  here
   412  a tide of slow lines                 qwen3-coder:30b  treatment    1  -        e/812
   413  hot air balloons over a salt flat    qwen3-coder:30b  control      2  yes      e/1017
…
1144 listed of 3255 held (newest snapshot 2026-09-27T135015Z, verified)
```

It opens the newest snapshot under `--from` read-only and never writes. Filters: `--clean` (no
`offplan_json`), `--one-per-prompt` (the earliest clean entry of each), `--executor`, `--rules`,
`--prompt-of ENTRY`, `--not-imported`. Two columns are worked out, not stored: **`buffers`**,
whether the kept sketch calls `createGraphics()` — harness 4 read a buffer as the sketch, so
that entry's `is_looping` and `size` are the buffer's (AGENTS.md, *Working on the code*); about
317 of the rental's jobs — and **`here`**, the published entry on this node with the same
prompt. `--json` for an agent; `--ids` prints the ids alone, one per line, which is the file
`run` reads.

### 2.3 `sketchgen import run`

```
$ sketchgen import run --from ~/sketchgen-backups/sld-gpu/sketchgen --node sld-gpu \
      --ids first-200.txt --by profcarroll --note "rented OCI A10, terminated 2026-09-27"
```

Refused, exit 3, nothing written, when: the generator is not paused (as `bench` refuses; the
worker must not be issuing ids and writing `jobs/` under it); the snapshot does not verify; its
schema is newer than this node's; an id is not in the snapshot or is not `held` there; `--node`
is unregistered and there is no `--note` to register it with; the disk has less than the
selection's size and a margin free.

Then, per entry, in one transaction each: insert the job (`held`), learn its id, copy the
directory to `jobs/.import-<id>` and rename it into place, insert the attempts with their paths
rewritten, insert the entry with its origin. A copy that fails rolls the transaction back and
removes the partial directory; the run goes on and says so. An entry already imported is
reported as `already e/<id>` and skipped. `--dry-run` does everything but write, and prints the
megabytes.

The last line is the one the Held page cares about: `imported 200 of 200 → entries 1758–1957,
held; 0 skipped, 0 failed; 483 MB copied`. Exit 1 if any failed.

### 2.4 The critic's query

§1.5, with a test beside the ones that already cover the dead-child and strip-less cases: an
imported published entry at the head of the list is not offered, and the one behind it is.

### 2.5 Tests

Migration 019 up from 018, and idempotent. `import` against two scratch databases and a scratch
archive built in `setUp`: ids renumbered and origins kept; all five paths rewritten and
resolving; `created_utc` unchanged; a second run imports nothing; every refusal refuses before
any row or directory exists; a copy that fails half way leaves neither. No network, no browser.

## 3. Packet 2 — `feat/origin-on-the-page`

### 3.1 The entry page and `meta.json`

`meta.json` gains `origin`: `null` for an entry made here, else `{node, entry_id, job_id,
shape, build, first_utc, last_utc, note, snapshot, imported_utc}`. The provenance table gains
one row above *Node shape*:

> **Made on** — sld-gpu, as its entry 412 · rented OCI A10, terminated 2026-09-27 · imported
> here 2026-09-28 from snapshot 2026-09-27T135015Z

and, where there is one, a line under the prompt: *the same prompt, made on this node:
[entry 812](…)* — and the reverse on that entry's page. The card on the index gets a chip
with the node's name, built as the `off-node` chip is, its title saying the same sentence.

The name `sld-gpu` is public by this. It is a fleet name, already in this repository's
documents; the scan's `instance-` mark (`HOSTNAME_MARK`) is about Oracle's hostnames and does
not match it. An origin whose name did match would be refused at `import run`.

### 3.2 The kiosk and the phone

`kiosk.json` and `swipe.json` rows gain `origin` (the node's name; absent when made here) and
`shape`. The *authors* overlay adds the machine after the executor.

The kiosk reads one more launch parameter, **`origin=`**: `origin=sld-gpu` plays only entries
from that node, `origin=home` only those made on the node that publishes the gallery, and
without it everything plays, as today. Like `site=` it is read from the launch URL only, never
stored and never bound to a key: which work a wall shows is a property of the wall somebody set
up (`kiosk-mac.md` §1.1). An origin that matches nothing plays everything and says so in the
menu footer; a dark wall is the worse failure.

That is the three-screen room: one launch URL per Mac, no second gallery.

### 3.3 Tests

`test_gallery.py`: `origin` in `meta.json` and both manifests, null and absent where they should
be; the *Made on* row; the twin links both ways; the chip. `test_gallery_js.py` and
`tests/js/kiosk.js`: the three values of `origin=`, that it is not written to storage, the
fallback.

## 4. The first import

Between batches, with the tray on Held empty.

1. `bin/fleet update sld-cloud` with both packets merged. It brings #179–#184 and migrations
   018 and 019, and Packet 2 changes a template, so it is the full render.
2. `bin/sg control pause --reason "import"`, then `import list … --ids > first-200.txt`, then
   `import run --dry-run`, then `import run`. Resume.
3. Held now has 200 more cards. Publish from there, in batches (`held-batch.md`): one push and
   one index per batch. The index re-renders every entry page, so it grows with the gallery:
   about 25 minutes at 1,500 pages.
4. Point one kiosk at `?origin=sld-gpu` and look.

**Traps.**

- **The laptop's mirror.** An imported entry adds about 2.4 MB to `~/sketchgen/jobs`, frames
  included, and `bin/pull-backup.sh` mirrors all of `jobs/`. sld-cloud's archive is 2.9 GB
  against the laptop's 1.2 GB copy of 09-18, and the laptop had 5.7 GB free on the 27th: the
  next pull is already 1.7 GB, and 200 imports add 0.5 GB to it. Pull with `--no-jobs` until
  there is somewhere larger to pull to.
- **Do not delete the archive after an import.** `import run` copies. What was not chosen is
  still only there.
- **A push is not the place to find the cap.** Check the tree's size before a batch of
  publications, not after Pages has refused the build.

Packet 1 adds the first two to AGENTS.md, with the verb.

## 5. Acceptance

1. An entry imported from the archive is `held` on sld-cloud under a new id, with its job
   directory in place and its strip readable by the judge; its `created_utc`, shape, models and
   timings are the archive's; `origin_node`, `origin_entry_id` and `origin_job_id` name where
   it came from.
2. Running the same import again changes nothing.
3. Published, its page says where it was made and links the entry of the same prompt made
   here; `meta.json` carries the same.
4. A kiosk launched with `origin=sld-gpu` plays only imported entries; one without it plays
   everything.
5. With imported entries published and older than everything else, an idle round on sld-cloud
   critiques none of them.
6. `SELECT origin_node, count(*) FROM entries GROUP BY 1` separates the two populations.

## 6. Out of scope

- **All 3,255.** `gallery-hub.md` Steps 1–2.
- **Re-gating under harness 6.** The gate would run on the A1 and the entry would then say it
  was made on one machine and measured on another, for every timing on its page. The 92 entries
  sld-cloud gated under the same misreading were left as recorded; these are treated the
  same, and `buffers` (§2.2) lets a selection leave them out.
- **The D12 pair.** The verb takes any archive and any registered name, so d12's 201 jobs could
  come the same way. Nothing here asks for it.
- **A filter on the public index.** The chip is there; a filter is a change to `gallery.js`
  that nobody has asked for.
- **Live nodes publishing.** That is the hub.

## 7. Decisions for the operator

1. **How many first, and chosen how.** Recommended: about 200, from `--clean --one-per-prompt`
   with `buffers` left out, then by eye on Held.
2. **Do imported entries join the judge's pool?** Recommended yes (§1.6). The alternative
   keeps the A/B untouched without a `WHERE`, and leaves them unranked on the wall.
3. **Does the idle critic skip them?** Recommended yes (§1.5).
4. **The twin links** (§3.1): both ways (recommended), one way, or not at all.
5. **Where the second copy of the archive goes.** It is on one instance. The laptop cannot hold
   it. This plan does not need the answer, but the archive does.
