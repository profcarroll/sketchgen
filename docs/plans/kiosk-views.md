# Kiosk views

An amendment to `docs/plans/kiosk.md` §1.5. That decision — *a kiosk play is not a view* — was
confirmed by the instructor before the kiosk was built and is now reversed: a sketch played on the
projector counts as a view. This document says what that costs, what it does not touch, and the
three decisions that are still open.

One packet, one branch `feat/kiosk-views`, one PR.

## 0. What the repository says today

The kiosk's silence is not one line. It is asserted in six places, and all six move together:

| where | what it says |
| --- | --- |
| `sketchgen/assets/kiosk.js:20–29` | the header's *what it must never do* block: POST anything, present an identity |
| `sketchgen/assets/kiosk.js:205–207` | `fetchCounts`: *the kiosk reads and never writes* |
| `sketchgen/templates/kiosk.html:86` | the menu footer, to the viewer: *a play here is never counted as a view* |
| `tests/test_gallery_js.py:613` | `test_it_never_writes_anything` — `method:` appears nowhere in the file |
| `tests/js/kiosk.js:260` | the fixture rebuilds that footer string verbatim |
| `docs/plans/kiosk.md` §1.5, §4.5, §5 | the decision, the prohibition, the acceptance test |

## 1. What does *not* change, and why it matters

Worth stating plainly, because §1.5's stated reason for the prohibition does not survive reading
the code, and a reviewer should not have to re-derive that:

1. **No sort reads views.** `gallery.js` `SORTS` is `newest, oldest, random, liked, reviewed,
   controversial, consensus`; `byLiked` reads likes, `byReviewed` reads pair counts. Views are not
   a sort key on the grid, on the kiosk, or on any page. §1.5's worry that kiosk plays would
   *"bend the 'most liked' and 'most reviewed' neighbourhoods"* is not borne out — those two
   orders cannot see views at all.
2. **No judgment reads views.** `judge.py` `BLIND_FIELDS` lists `views`, so the word cannot reach
   the agent judge's prompt; `pairs.py` reads neither `engagement` nor `likes`. The Bradley–Terry
   measurement is untouched by this change, in either population.
3. **The node's mirror is a max.** `sync.py` `_UPSERT_VIEWS` is
   `SET views = MAX(engagement.views, excluded.views)` over the Worker's roll-up, so nothing about
   how a view arose changes how it lands on the node.

So the only thing this change actually affects is the views number itself, on the card and on the
entry page. The objection that survives is about what that number honestly means — not about
contaminating the experiment.

## 2. The real constraint: nothing downstream will restrain the kiosk

`writepath/worker.js` `routeView` de-duplicates a view **only when the viewer is signed in**, for
60 s per entry, keyed by `sha256(session cookie)`. `writepath/schema.sql:54–58` says why anonymous
views are not de-duplicated: doing it would mean keeping an IP or a fingerprint, and this service
keeps nothing of the kind.

The kiosk signs nobody in. So **every POST the kiosk makes is counted, unconditionally**, and there
is no server-side lever to stop it — the `control` table is the node's pipeline switch and the
Worker never reads it. Whatever restraint exists has to be written into `kiosk.js`.

Scale, at the defaults: 222 published entries × 60 s is one loop every 3.7 h, so a screen running a
9-hour day adds roughly 2.4 views to every entry, per screen, per day. Over a semester that is the
dominant source of views unless it is bounded. And `→` held down advances as fast as the fade
allows.

That is the whole design problem. The rest is mechanical.

## 3. Decisions

### 3.1 A view is a sketch that stayed on screen — recommended

Not every seat. Count a sketch when it has been on the stage, playing, for
`VIEW_AFTER_S = 10` seconds of un-paused time, clamped to `min(VIEW_AFTER_S, every)` so a 15 s
setting still counts something. One view per seat, ever — a boolean guard, reset in `seat()`, not a
timestamp.

This rides on machinery that already exists. `tick()` accumulates `state.elapsed` from
`requestAnimationFrame` deltas and stops accumulating while `state.paused`; a hidden or throttled
tab stalls rAF, so a kiosk on a background tab already stops counting without a `visibilitychange`
handler. Skipping with `→` never reaches the threshold, so skipped sketches cost nothing.

### 3.2 An attendance limit — recommended, and separable

§1.5's strongest case was *one screen left running*. A dwell threshold does not answer it: a
projector left on over a long weekend still posts views nobody saw.

Stop counting — not playing — after `VIEW_STOP_S = 8 * 60 * 60` playing seconds with no key press
and no mouse movement, counted off the same rAF deltas as everything else here.
The page keeps playing, the caption keeps updating, and the first key or twitch of the mouse starts
counting again. The kiosk already tracks mouse movement for the 3 s cursor hide, so the input
already exists.

This is the piece that makes the number defensible to anyone who asks. It can ship in the same
packet or a second one; it does not change any interface.

### 3.3 Where the off switch lives

There is no server-side one (§2), so the gallery must carry it:

- `config.json` gains `kiosk_views` (boolean, default `true`). `kiosk.js` reads it beside
  `write_path` and posts nothing when it is `false`. `gallery.py` `Config` is a dataclass with
  `load` and `to_json`, and `load` reads the checkout's own `config.json` — so the field
  round-trips a `render-index` and the instructor can turn kiosk views off by editing one line in
  the gallery repository, with no pipeline change.
- `?views=0` in the URL turns it off for one projector, the same precedence as every other kiosk
  setting (§1.8 of the kiosk spec: a parameter beats storage beats the default). It is **not**
  persisted to `localStorage` and has no key — a bumped keyboard must not silently change what the
  gallery's numbers mean.

### 3.4 Whether a kiosk view stays distinguishable — the one decision that is not reversible

Three answers, and the difference between them is a semester of data.

**(a) One number, no new dimension.** The kiosk POSTs `/view` exactly as the entry page does. No
schema change anywhere. Cheapest, and the numbers can never be separated afterwards.

**(b) One number, but the Worker keeps the split — recommended.** The kiosk POSTs
`{entry_id, source: "kiosk"}`. `routeView` validates `source` against `{"entry", "kiosk"}`
(absent means `"entry"`, so the entry page is unchanged) and `SQL.bumpView` also increments a new
`views.kiosk_count`. Nothing downstream changes: `sqlCountViews` and `SQL.pullViews` both select
`count`, which stays the total, so `/counts`, `/pull`, `sync.py`, the node's `engagement` table and
every template behave exactly as they do now. The split exists in D1, queryable for the write-up,
and costs one `ALTER TABLE` and about ten lines of Worker.

**(c) Two numbers all the way through.** (b), plus `/pull` carrying `kiosk_count`, a node migration
`006` adding `engagement.kiosk_views`, `sync.py` mirroring it, and the card and entry page deciding
how to show two numbers. Three or four times the work of (b), and it is the only answer that lets
the gallery itself show *1,240 views · 300 of them on the projector*.

(b) is the recommendation because it buys back the irreversibility of (a) for almost nothing, and
leaves (c) available later without having lost the data in between.

### 3.5 What stays prohibited

The kiosk still presents **no identity**: no cookie, no `credentials`, no `Authorization` header.
`routeView` accepts an anonymous POST, so nothing is needed. This property is now *more* important
than it was, not less — it is what keeps the kiosk from being a way to attribute views to whoever
last signed in on that machine — and its test stays exactly as written.

Also unchanged: one iframe at a time, no `postMessage`, no `eval`, no storage key but
`sketchgen-kiosk`.

## 4. The work, file by file

Assuming §3.1, §3.2, §3.3 and answer (b).

### `sketchgen/assets/kiosk.js`

- Rewrite the header's *what it must never do* block: it now writes one thing, to one endpoint,
  under three conditions, and the comment should say all three.
- `sendView(entry)` — `fetch(base() + "/view", { method: "POST", headers: { "Content-Type":
  "application/json" }, body: JSON.stringify({ entry_id: entry.id, source: "kiosk" }) })`,
  `.catch(function () {})`. Fire and forget; a view that does not land is not an error, as
  `gallery.js` `sendView` already has it. No `credentials`. Keep it `fetch`, not `sendBeacon` —
  house style, and the suite reads the file as text.
- `state.viewed` — a boolean, cleared in `seat()`.
- In `tick()`, after `state.elapsed` is advanced: if `!state.viewed && !state.paused &&
  state.elapsed >= viewAfter() && countingViews()` then `state.viewed = true; sendView(current())`.
  The guard is set **before** the fetch, so an in-flight request cannot be doubled by the next
  frame.
- `viewAfter()` returns `Math.min(VIEW_AFTER_S, state.every)`.
- `countingViews()` — `base()` is non-empty, `config.kiosk_views !== false`, `?views=0` absent, and
  (§3.2) `state.sinceInput < VIEW_STOP_S`.
- §3.2: `state.sinceInput` accumulates in `tick()` beside `state.elapsed`, and is cleared by
  `onKey` and by the `mousemove` handler `wireIdle` already registers for the cursor hide.
- `query()` carries `&views=0` when it is set. `persist()` rebuilds the address bar from that
  string, so a parameter missing from it is one the first acting key throws away — and the launch
  link in the menu footer would then hand somebody a projector that counts when the one it was
  copied from did not.

### `sketchgen/templates/kiosk.html`

Line 86's note is now false, and it is the only place the room is ever told. New copy:

> Views and likes are live from the write path; a sketch counts as a view once it has been on
> screen for ten seconds.

The mockup does **not** change. `test_gallery.py:2426` cites it as the verbatim source for the
start card, but this line is not the mockup's: the mockup's own note says its numbers are example
numbers, which is still true of a page with no write path behind it.

### `sketchgen/gallery.py`

`Config` gains `kiosk_views: bool = True` in the dataclass, in `load` and in `to_json`. Three small
edits, and `to_json` is `sort_keys=True` so the field lands in place.

### `writepath/`

- `schema.sql`: `kiosk_count INTEGER NOT NULL DEFAULT 0` on `views`, with a comment saying what it
  is for. `schema.sql` is `CREATE TABLE IF NOT EXISTS`, so **an existing D1 does not pick this up**
  — the deploy runs the `ALTER TABLE views ADD COLUMN kiosk_count INTEGER NOT NULL DEFAULT 0` by
  hand, and §6 has the order.
- `worker.js`: `routeView` reads `body.source`, rejects anything outside `{"entry", "kiosk"}` with
  a 400 (an absent one is `"entry"`), and `SQL.bumpView` increments `kiosk_count` by 0 or 1.
- `writepath/test`: a kiosk view bumps both columns, an entry view bumps only `count`, an unknown
  source is a 400, `/counts` and `/pull` still return the total.

No CORS change: `corsHeaders` allows `env.GALLERY_URL`'s origin and `preflight` already answers
`POST` with `Content-Type`, which is what the entry page's own `/view` call needs.

### `tests/test_gallery_js.py`

- `test_it_never_writes_anything` inverts into **`test_it_writes_one_thing_and_only_one`**: parse
  the `fetch(` calls out of the comment-stripped source, assert exactly one carries `method:`, that
  its URL is `base() + "/view"`, and that its method is `POST`. Strictly better than the old
  assertion — it pins the single write instead of merely forbidding all of them.
- `test_it_presents_no_identity` — **unchanged**. `document.cookie`, `credentials` and
  `Authorization` stay absent (§3.5).
- New, against `tests/js/kiosk.js`:
  - a sketch left on the stage past the threshold posts exactly one view, with the right
    `entry_id` and `source: "kiosk"`;
  - **driving 600 frames over one seat posts exactly one view** — the test that matters most,
    because a per-frame bug would post sixty a second into an undeduplicated endpoint;
  - `→` before the threshold posts nothing;
  - pausing at 9 s and resuming posts at 10 s of un-paused time, not at 10 s of wall clock;
  - `every=15` still posts (the clamp);
  - `?views=0` posts nothing; `config.json` without `write_path` posts nothing;
  - `kiosk_views: false` in `config.json` posts nothing.

### `tests/js/kiosk.js`

The harness that actually runs the script; `tests/js/dom.js` is the generic DOM beneath it and does
not change.

- The `m-note` fixture string at line 260 follows the new copy.
- The `fetch` stub at line 299 is already `function (url, init)` but dispatches on URL alone and
  ignores `init`. It gains a `/view` branch that records `{url, init}` into an array the tests read
  back — that recorder is what every test in the list above asserts on.
- The file header's *fetch answers the four requests the script is allowed to make* becomes five,
  and names the one that is a write.
- The timer queue is stepped by hand and the rAF driver advances frame by frame, which is exactly
  what the dwell and 600-frame tests need — no change.

### `tests/test_gallery.py`

- Pin the footer note copy, so the template and the mockup cannot drift apart silently.
- `Config` round-trips `kiosk_views` through `load` and `to_json`, and a `config.json` without the
  key defaults to `true`.

### `docs/plans/kiosk.md`

§1.5 is rewritten in place — leaving it as written would have the next reader take a reversed
decision as current — to record the reversal, its date, and a pointer here. §4.5 drops *POST
anything* and names what is now allowed. §5's acceptance line goes with it.

## 5. Risks

- **A per-frame bug posts sixty views a second** into an endpoint with no anonymous
  de-duplication and no server-side switch. The boolean guard set before the fetch, and the
  600-frame test, are the whole defence. Neither is optional.
- **The off switch is a Pages deploy, not a dial.** Editing `config.json` in the gallery repository
  is fast, but it is not instant and it is not the node. Accepted; there is no cheaper lever
  without giving the Worker a control read it does not have.
- **`views` now means two things.** The entry page's *Engagement, not judgment* note stays true and
  needs no change, but the gallery no longer distinguishes *a person opened this* from *a projector
  showed this*. Answer (b) keeps the split recoverable in D1; the gallery's own number does not
  show it.
- **A future `views` sort would self-amplify.** Nothing sorts by views today (§1.1). If one is ever
  added, a kiosk playing in that order feeds its own ordering. Worth a comment next to `SORTS` when
  this lands.

## 6. Running it

1. Merge the PR.
2. **D1 before the Worker.** Run the `ALTER TABLE` against the production database, then
   `wrangler deploy`. A Worker deployed against a database without the column fails every `/view`,
   including the entry page's.
3. `update.sh` on the node — pulls, restarts, runs `render-index`, which rewrites `kiosk.html`,
   `assets/kiosk.js` and `config.json`. **Pull the gallery checkout first** if the PR was merged
   from outside the node.
4. `render-all` is not needed: nothing under `e/` changes.
5. Verify on the live site, not on `127.0.0.1` — a sandboxed frame cannot fetch a loopback address
   and the sketches render black locally. Open `kiosk.html`, press Start, leave one sketch for
   fifteen seconds, and watch that entry's count move on the grid.

## 7. Effort

Roughly one packet, comparable to a small feature: the script change is under fifty lines, the
Worker change about ten, and the tests are most of it. Answer (c) in §3.4 would be three or four
times that and should be its own packet if it is ever wanted.
