# Swipe mode

A new page on the published gallery, `swipe.html`, for a phone in a hand: one sketch at a time,
full screen, nothing timed, and six gestures instead of a keyboard. Up and down walk the gallery
in one of the grid's seven orders; right likes; left judges the sketch against another with the
compare page's two questions; a tap shows and hides the words; a hold hands the touch to the
sketch. It is the entry page's three verbs — view, like, judge — with the page taken away.

Mockup (the visual spec; the copy in it is final): https://claude.ai/artifact/RkxjRLm69rn4qGnd2oHqnY
— and the same page as source, runnable offline, at `docs/plans/swipe-mockup/swipe.html`. The
mockup runs six real sketches in-page with p5 in global mode; the real page runs them in the
sandboxed iframe the gallery already uses (§4.3). Open it on a phone and it takes the whole screen.
Everything else — layout, gestures, thresholds, strings, sheets — is to be taken from it as written.

Three packets, three agents, one branch `feat/swipe`, one PR (§7). It touches the **generator and
its assets**: `sketchgen/gallery.py`, `sketchgen/templates/swipe.html` (new), the nav in
`grid.html`, `entry.html`, `compare.html`, `kiosk.html`, `sketchgen/assets/swipe.js` (new),
`sketchgen/assets/gallery.js` (one function, §5), `sketchgen/assets/gallery.css`,
`sketchgen/cli/gallery.py` (help text only), `tests/test_gallery.py`, `tests/test_gallery_js.py`,
`tests/js/`. **No database, no migration, no write-path change, nothing in `worker.py`,
`lineage.py`, `sync.py` or `writepath/`.** (§1.7 says why not.)

Repositories and conventions are `docs/plans/kiosk.md`'s, and that document is the model for this
one: read it first, and `sketchgen/assets/kiosk.js` beside it, because most of what follows is the
kiosk's machinery with the keyboard taken away. Templates are `string.Template`, so JavaScript in
one escapes `$` as `$$`; assets are copied verbatim. `python3 -m unittest discover -s tests`, no
pytest, no dependency in either language. `gallery.js` is the house style for the script: an IIFE,
`"use strict"`, ES5, comments that say why.

## 1. Decisions already made

1. **A page of its own, shaped like the kiosk.** One static shell, one manifest, one script, the
   same sandboxed frame. Not a mode of the entry page, which is the right page for reading about
   one sketch and the wrong one for browsing; not a mode of the kiosk, whose script is built
   around a timer and a menu this page has neither of. The grid, the entry page and the kiosk
   stay what they are.
2. **A manifest of its own, `swipe.json`.** `kiosk.json` is 2.8 MB, 600 KB gzipped: every brief
   and statement in the gallery, for a page that shows one of each at a time. `swipe.json` (§2)
   carries what the feed, the caption and the seven orders need — about a fifth of that — and the
   words sheet fetches `e/<id>/meta.json` for the rest when it opens. Same rows, same
   Bradley–Terry tables, written by the same `render_index` pass, so the two manifests never
   disagree.
3. **Published entries only**, in id order, as the kiosk's is.
4. **The same sandboxed iframe as everywhere else** — `sandbox="allow-scripts"`,
   `src="e/<id>/sketch/"`, one at a time, no `postMessage`, no source evaluated in the page. The
   consequence this page has to live with: **a finger that lands on the frame belongs to the
   frame**. The page never sees a swipe that starts on the sketch and cannot ask the frame to pass
   it along. So a transparent **shield** sits over the stage and takes every touch (§4.4), and
   swiping works anywhere on the screen, which is what a full-screen feed has taught everyone to
   expect. The cost — a sketch that answers a touch cannot feel one — is paid by the hold: half a
   second without moving lifts the shield, the sketch has the touch, and one pill at the bottom
   gives it back. That hold is also the gesture the frame needs before it will make sound, which
   the kiosk could never deliver.
5. **Six gestures, and nothing advances by itself.** Up next, down previous, right like, left
   judge, tap the words, hold the sketch. No timer, no progress line: a phone is held, not left
   running. Desktop courtesy, not a second interface: `↑ ↓ L J I Esc` do the same six things, and
   a mouse drag is a swipe, because gestures are pointer events and the tests drive them (§6).
6. **A swipe view is a view, on the entry page's terms.** After ten un-paused seconds on screen
   (§4.7) the page POSTs `/view` exactly as the entry page does — `{entry_id}`, the session's
   bearer header, no `source`. A person who stayed ten seconds on a sketch they chose to open is
   what the number already means, which is why this is not the kiosk's case and does not get the
   kiosk's column. *Confirmed by the instructor, 20 September 2026, with §1.2 and the offers in
   §7 packet A; §1.7 lists what the alternative would have cost.*
7. **No write-path change.** The Worker already accepts everything this page sends: `/like` and
   `/vote` take the session, `/view` takes anything, `/counts` and `/me` are reads. Naming the
   view `source: "swipe"` would cost `VIEW_SOURCES`, an `ALTER TABLE views ADD COLUMN swipe_count`,
   a `wrangler deploy` and the deploy-order trap in `docs/plans/kiosk-views.md` §6; it is not
   worth it for a view that means what an entry-page view means.
8. **The session is the gallery's, and this page presents it.** Unlike the kiosk, this page is
   somebody's phone: a like and a vote are counted by GitHub login, so `swipe.js` reads the same
   `sketchgen_session` token `gallery.js` keeps and sends it the same way, as a bearer header
   (§4.6). It asks for a sign-in **only when a like or a vote needs one**, never at start.
9. **Signing in comes back here.** The Worker's `/callback` returns to the gallery's front page
   with the token in the fragment, and only there. Rather than teach the Worker a return path (a
   column on the OAuth state table, and a deploy), the swipe page leaves a note in storage before
   it goes, and `gallery.js` on the front page, having just claimed a token, reads the note once
   and sends the visitor straight back (§5). One extra hop, no Worker change, and the visitor
   lands on the sketch they were looking at, signed in.
10. **Settings live in the URL and in `localStorage`, in that order.** `?order=random&at=827`
    seats the page; `at` follows the seat in the address bar as the visitor swipes, so the URL is
    always a link to what is on screen. `order` is stored; `at` is not. A URL parameter beats a
    stored one; a stored one beats the default (`newest`).
11. **Fit, only fit.** A fixed canvas is laid out at its own size and scaled with a CSS transform
    to fit the short side of the screen, exactly as `docs/plans/kiosk-fullscreen.md` §1 has it —
    but there is no `Z`, and no walk: on a phone *native* is smaller than the screen for every
    canvas in the gallery, and *fill* crops a work to a phone without asking. A window-sized
    sketch fills the screen. Turning the phone refits.
12. **Four sheets, no columns, no code, no QR.** The kiosk's thirteen overlays become one *words*
    sheet; *judge*, *this page* and *sign in* are the other three (§4.5). The phone is the thing
    the QR code was for.
13. **No web font, no CDN, nothing fetched but `swipe.json`, `config.json`, `/counts`, `/me`,
    `e/<id>/meta.json`, `pairs.json` (on the first judge) and the frames.** `gallery.css` plus one
    swipe block appended to it, in the kiosk's black-ground palette; single-theme, because a
    sketch on black has no light mode. `body.swipe` sets the ground explicitly.

## 2. `swipe.json`

Written by `render_index`, after `kiosk.json`, indented like it, sorted keys, trailing newline.
Published entries only, ascending id. Same database, same bytes.

```json
{
  "entries": [
    {
      "id": 827,
      "prompt": "the game of life\n\nRevise: …",
      "submitted_by": "profcarroll",
      "planner": "gemma4:e4b",
      "executor": "qwen3-coder:30b-a3b-q4_K_M",
      "rules_file": "control",
      "attempts": 1,
      "published_utc": "2026-09-18T14:55:19Z",
      "generation": 9,
      "parent_entry_id": 814,
      "critique_by": "gemma4:e4b",
      "responds": ["click"],
      "canvas": [800, 600],
      "judgment": { "human": { "look": { "score": 1.57, "n": 1, "pct": 0.83 }, "brief": { … } },
                    "agent": { … } },
      "sketch": "e/827/sketch/",
      "meta": "e/827/meta.json",
      "href": "e/827/",
      "url": "https://profcarroll.github.io/sketchgen-gallery/e/827/"
    }
  ]
}
```

Every value is one `_kiosk_entry` already computes, minus `brief`, `statement`, `seed`,
`created_utc`, `prompt_tokens`, `completion_tokens`, `wall_s`, `licence`, `root_entry_id`, `source`
and `qr`, plus:

- `responds` — the gate assertions of the form `responds(<what>)`, the `<what>` alone, in the
  order the assertions list them (`["click"]`, `["click", "drag"]`, `["audio"]`). **Absent** when
  there are none. The caption prints *responds to touch · hold to try* for `click` or `drag`
  (§4.4) and *makes sound · hold to hear it* for `audio`, and nothing for a sketch that has nothing
  to give. Today: 27 entries respond to a click, 25 to a drag, 7 to audio, of 910.
- `mic` — `true` when `_needs_mic(source)` is, as the card's `data-run-mic` is; absent otherwise.
  A listening sketch cannot reach the microphone inside the frame (gallery.js `startSketch` says
  why). The swipe page still frames it — the sketch runs, deaf, as it does on the kiosk — and the
  caption says *listens to the microphone · open the entry page to let it*. It is not skipped:
  skipping would make the feed lie about the gallery's size.
- `meta` — `e/<id>/meta.json`, the file the words sheet and the judge sheet fetch (§4.5). A path
  from the manifest, never assembled in the script, as `qr` is for the kiosk.

`_swipe_entry(conn, row, config, parent, children, scores)` builds one row from the same `_meta()`
call `_kiosk_entry` makes — factor the shared part out rather than compute `_meta` twice per entry,
and keep `_kiosk_entry`'s output byte-identical (`test_the_same_database_renders_the_same_kiosk_json`
or its equivalent must still pass). `swipe.json` goes through `guard()` with everything else.

## 3. The page: `sketchgen/templates/swipe.html`

The shell, with the mockup's markup inside `body.swipe`. Three substitutions, as the kiosk has:
the shared header bar with `swipe` current in the nav, `<script>window.SKETCHGEN_ROOT = "$root";</script>`,
and `<script src="${root}assets/swipe.js"></script>`. No entry data in the page. `_swipe_page(config)`
renders it and `render_index` writes `dest / "swipe.html"`. Two additions to the head that the other
shells do not have: `<meta name="theme-color" content="#050608">`, and `viewport-fit=cover` on the
viewport meta, so the page runs under a phone's notch and home bar (the CSS pads the caption and the
sheets with `env(safe-area-inset-bottom)`).

The header bar is present in the DOM for the same reasons the kiosk's is, and hidden the same way
the moment the start card is dismissed (`body.swipe.playing .bar { display: none }`).

**The DOM contract**, ids only, which the mockup, the CSS and the test harness all build from:

| id | what |
| --- | --- |
| `welcome`, `go` | the start card and its one button; the card's strings verbatim from the mockup |
| `stage` | holds the one iframe; `position: fixed; inset: 0` |
| `shield` | the transparent layer over the stage; every gesture starts here (§4.4) |
| `cue-like`, `cue-judge` | the two glyphs that grow under a horizontal drag |
| `status` | top left, a `<button>`: *3 of 910 · newest*; a tap opens `sheet-settings` |
| `heart` | top right; `body.swipe.liked` fills it |
| `caption` | prompt, revisions, authors, facts, *tap here for the words*; a tap opens `sheet-info` |
| `toast` | one-line answers: *liked*, *unliked*, *signed out* |
| `touching` | the pill: *the sketch has the touch · tap here to swipe again* |
| `dim` | behind a sheet; a tap closes it |
| `sheet-info`, `sheet-judge`, `sheet-settings`, `sheet-signin` | the four sheets, each with a `[data-close]` ✕ |

The stage layout, for the CSS (the mockup's `.phone` rules are the reference and are lifted into
`gallery.css` under `/* ---- swipe ---- */`, `.phone` renamed `body.swipe`, `position: absolute`
becoming `fixed`, tokens the kiosk's): the stage fills the viewport and centres the frame; the shield
is over it at `z-index: 3` with the caption under it at 2; the cues at 4; the status line and heart at
5; the toast and
the pill at 8; the dim at 9 and the sheets at 10; the start card at 20. The caption's scrim is deep
enough to read over a white canvas — a third of the gallery is light. `body.swipe` sets
`touch-action: none` and `overscroll-behavior: none`, so no swipe becomes a scroll or a
pull-to-refresh; the sheets alone allow `pan-y`.

## 4. The script: `sketchgen/assets/swipe.js`

Its own file. It shares more with `kiosk.js` than the kiosk shares with `gallery.js` — the
manifest loader, the seven comparators, `showEntry`, `fitFrame`, `promptParts`, `captionHtml` —
and still it is not a fork of `kiosk.js`: the kiosk's timer, menu, key table and overlay map are
most of that file and none of this one. Copy the functions named here, keep their names, and say
in a comment above each that it is the kiosk's; if the two ever need to be one module, the
matching names are what makes that a mechanical change.

### 4.1 What it fetches and what it sends

Reads: `config.json` (`cache: "no-store"`, for `write_path`), `swipe.json` (`no-store`),
`/counts?entries=…` in batches of `COUNTS_BATCH = 100`, `/me` once at start, `e/<id>/meta.json`
when a sheet needs it (cached per id for the page's life), `pairs.json` on the first judge, and
`GET /logout` on sign-out (as `gallery.js` does: the Worker's cookie goes with the token). Writes,
each with `authHeaders` (§4.6) and `credentials: "include"`: `POST /view` (§4.7), `POST /like`
(§4.6), `POST /vote` (§4.5). Nothing else, and the acceptance test pins the list (§6).

With no `write_path` in `config.json`: counts print `—`, the heart is hidden, a right swipe says
*the gallery write path is not deployed yet* in the toast, votes are noted on the sheet only, and
no view is sent — the entry page's rules, restated.

### 4.2 Settings, sequence and seat

- `readSettings()`: `order` from `?order=`, else the `sketchgen-swipe` blob `{ v: 1, order }`, else
  `newest`; `at` from `?at=` only. An unknown order is `newest`.
- `persist()` writes the blob and rebuilds the address bar with `history.replaceState` as the
  kiosk's does — `?order=<order>&at=<id>` — after every seat and every order change. `order=newest`
  is still written; a launch link that says what it does is better than one that relies on a default.
- `sequence(order)`: the kiosk's seven comparators over the manifest, ties newest first then by id,
  `random` a Fisher–Yates that reshuffles when the sequence wraps.
- `seat(i)` frames the entry, paints the caption and the status line, clears `state.viewed`, and
  persists. `?at=<id>` seats on that id in the current order; an id not in the manifest seats at 0.
- Changing the order in the settings sheet keeps the current sketch on screen and re-seats it in
  the new sequence (`reorder()`).
- Counts: one pass over the whole manifest at start, in batches, as the kiosk does — 910 ids is ten
  requests and the *liked* order needs all of them — and a refresh of the current entry's counts
  after a like lands.

### 4.3 The frame

`showEntry(entry)` and `fitFrame()` are the kiosk's, after `kiosk-fullscreen.md`: the frame is laid
out at the canvas's own size (`--frame-w/-h`) and scaled (`--frame-sx/-sy`, equal) to *contain* in
the stage; a row without `canvas` gets a frame that fills the stage at scale 1. Refit on `resize`
and on `orientationchange`. One iframe in the document at any time; the previous one is removed
before the next is appended.

The frame sits under the shield. `body.swipe.touching` sets `pointer-events: none` on the shield
and the frame has the touch until the pill is tapped or `Esc` is pressed.

### 4.4 Gestures

Pointer events on `#shield`, with `setPointerCapture` on `pointerdown` so a drag that leaves the
element still ends. The constants, from the mockup, and they are the spec:

| name | value | meaning |
| --- | --- | --- |
| `AXIS_LOCK` | 12 px | the first axis to move this far owns the gesture |
| `TAP_MOVE`, `TAP_MS` | 10 px, 300 ms | a release inside both is a tap |
| `HOLD_MS` | 450 ms | a press this long without an axis is a hold |
| `COMMIT_V` | 64 px, or > 0.5 px/ms | a vertical drag past either commits |
| `COMMIT_H` | 90 px | a horizontal drag past this commits |

While a vertical drag is live the stage follows the finger at 0.6; while a horizontal one is live
the stage follows at 0.35 and the cue on that side grows from 0 to 1 over `COMMIT_H`. On release:

- **no axis**, inside the tap bounds — if the words are up and the point is inside the caption's
  box (`getBoundingClientRect`, read at the tap), open `sheet-info`; otherwise toggle the words:
  `body.swipe.quiet` hides the caption, the status line and the heart, and shows them again. The
  caption sits **under** the shield with `pointer-events: none`, so the shield sees every tap and
  routes it, and a swipe that starts on the words is still a swipe. (The mockup's first draft had
  the caption above the shield; on a phone that ate the swipes a thumb starts in the bottom
  third, which is where the caption is.)
- **no axis**, held past `HOLD_MS` — the shield lifts (`body.swipe.touching`), the caption goes
  quiet, the pill appears, and `navigator.vibrate(12)` where it exists. A hold that never moved is
  never a tap: the hold timer clears the gesture before release can read it.
- **vertical**, past `COMMIT_V` — `go(+1)` for up, `go(−1)` for down: the stage leaves the way the
  finger went over 160 ms, the seat changes, the stage enters from the other edge over 220 ms.
  Short of the threshold the stage snaps back.
- **horizontal**, past `COMMIT_H` — right is `like()`, left is `judge()`; either way the stage
  snaps back first. Short of the threshold, nothing.

A gesture cannot start while a sheet is open or while the sketch has the touch; the dim and the
pill are what the finger finds then. Modifier chords on the courtesy keys are ignored. Every
transition is under `prefers-reduced-motion: reduce`.

The caption prints one fact the kiosk does not: *responds to touch · hold to try* when
`entry.responds` includes `click` or `drag`, *makes sound · hold to hear it* for `audio`, and
*listens to the microphone · open the entry page to let it* for `mic` (§2). Nobody should hold a
sketch that has nothing to give.

### 4.5 The sheets

Each sheet opens with `body.swipe` unchanged and `#dim` shown, closes on its ✕, a tap on the dim,
`Esc`, or a drag down past 90 px on the sheet itself, and only one is open at a time. Strings are
the mockup's, verbatim.

**Words** (`sheet-info`, from a tap on the caption). Fetches `entry.meta` once. Heading `#<id> · <root
prompt>`; the revisions line; Brief; Artist's statement · `<executor>`, unedited; Judgment, per
population, *1.57 over 1 pair* and the quadrant phrase (`_compass_title`'s words, computed in the
script from `judgment.<pop>.look.pct` and `.brief.pct` exactly as `kiosk.js` `quadText` does) or
*no pairs yet*; Provenance as a definition list (prompted by, planned by, written by, rules,
generation with its parent and critic, attempts, created, tokens, written in, canvas, licence);
two links, *open the entry page* (`entry.url`) and *sketch.js* (`entry.url + "sketch/sketch.js"`).

**Judge** (`sheet-judge`, from a left swipe or `J`). Half height, so the stage stays visible above
it. A is the current entry. B is chosen the way `gallery.js` `pickPair` chooses one, restated over
the manifest: an offered pair from `pairs.json` that contains A, at random among those, else a
published entry at random that is not A. Two tiles carry the two briefs (from two `meta` fetches)
**and nothing else** — no prompt, no authors, no counts, no verdicts — because the compare page
blinds the visitor until both answers are in and B's authors are the loudest anchor the sheet could
hand them. A is playing on the stage when the sheet opens; tapping a tile plays that side (one
frame, swapped, the caption stays hidden throughout). The two questions and their three buttons are
the compare page's, `aria-pressed` on the chosen one, the answered line saying *recorded: A* or
*noted here only: A*. Each answer POSTs `/vote` with `{entry_a, entry_b, question, choice}` when
there is a write path; a 401 drops the token and rewrites the line *noted here only: A — sign in
with GitHub to record it*, and the status line under the questions offers the sign-in sheet.

After both answers the reveal opens: *What the agents said* — from `pairs.json` `agents["<lo>-<hi>"]`,
one line per verdict, *`<judge>` · closer to its brief: A*, or *The agents have not judged this pair*
— the note about anchoring, *A is #827 · B is #702* linked to both entry pages, and two buttons:
*judge another pair* (a new B, A unchanged) and *keep swiping* (closes the sheet; if B was playing,
A is framed again).

Judging is blind on one side only, and deliberately: the visitor has just watched A with its caption
up. That is the entry page's precedent — its *Judge it against another* link is offered under the
whole page — and the alternative, hiding A's caption before a left swipe is allowed, would make the
gesture useless.

**This page** (`sheet-settings`, from the status line). *Sketch 3 of 910*; the seven orders as rows,
the current one marked; *signed in: profcarroll · sign out* or *not signed in · sign in with
GitHub*; the note *Likes and judgments are counted by GitHub login. A sketch counts as a view once
it has been on screen for ten seconds.*; the launch link for the current order and seat; links to
the gallery and the kiosk.

**Sign in** (`sheet-signin`). Opened by a like or a vote while signed out, never otherwise. Heading
*Sign in to like it* or *Sign in to record it* (*Sign in with GitHub* from the settings sheet's own
row); the sentence about GitHub login; one button, *Sign
in with GitHub*, which writes the return note (§5) and goes to `base() + "/login"`; one quiet
button, *not now*. Declined, a like does nothing and a vote stays noted on the sheet.

### 4.6 The session, and a like

Duplicate `gallery.js`'s session helpers — `readToken`, `writeToken`, `dropToken`,
`claimTokenFromHash`, `authHeaders`, `refused`, `loadMe` — with the comment the kiosk spec asked
for: the closure exports nothing, and forty lines copied is cheaper than a shared module across
every page. `STORAGE_KEY` is `"sketchgen_session"`, the same key, on purpose: it is the gallery's
session and this page is the gallery. `claimTokenFromHash()` runs first, as it does there, so that
a callback that one day returns here directly is already handled.

`loadMe()` at start paints the settings sheet's *You* row and decides nothing else: the page does
not ask the Worker whether this login has liked an entry, because `/counts` does not say and no
endpoint does. `state.liked` is a map of ids this page-load has toggled on, cleared by sign-out.

`like()`: no write path → toast; no username → `sheet-signin`; else POST `/like` with
`{ entry_id, on: !state.liked[id] }`, the heart filling optimistically and the caption's count
moving by one, undone on a failed response; a 401 → `refused`, the heart empties, `sheet-signin`.
`cue-like` plays its burst on a like that took and not on an unlike.

### 4.7 A view

The entry page counts a view on load. The swipe page counts one after the frame has been on screen
for `VIEW_AFTER_S = 10` seconds of un-paused, tab-visible time, once per seat — a boolean
`state.viewed` cleared in `seat()` and set **before** the fetch, as `kiosk-views.md` §3.1 has it
and for the same reason. Time is `requestAnimationFrame` deltas, so a backgrounded tab does not
count; time while the judge sheet has swapped B onto the stage counts for B's seat, not A's (the
swap is a `seat`-like call that clears the flag). Skipping past a sketch in under ten seconds costs
nothing. The POST is the entry page's: `{ entry_id }`, `authHeaders`, no `source`; the Worker
de-duplicates a signed-in viewer for 60 s and this page adds no de-duplication of its own.

### 4.8 What `swipe.js` must not do

Advance on its own. Count a view before ten seconds. Hold more than one iframe, `postMessage` to a
frame, or evaluate sketch source. Touch storage beyond `sketchgen-swipe`, the shared
`sketchgen_session`, and `sketchgen-swipe-return` (§5), which it only ever writes. Read
`document.cookie`. Show B's prompt, authors, counts or verdicts before both questions are answered.
Open the sign-in sheet before the visitor has done something that needs one. Send a request the list
in §4.1 does not name.

## 5. `gallery.js`: coming back from a sign-in

One function, `returnFromSignIn()`, called from `ready()` immediately after `claimTokenFromHash()`
and only when that call actually claimed a token (have it return a boolean). It reads
`localStorage["sketchgen-swipe-return"]`, removes it, and if the value matches
`/^swipe\.html(\?[A-Za-z0-9=&_.-]*)?$/` navigates with `location.replace(ROOT + value)`. Anything
else is dropped unread. The three rules, in a comment above it: consumed once, only in the same
load that claimed a token (so a stale note can never redirect somebody who typed the front page's
address), and only to this page's own `swipe.html` (so nothing in storage can send a visitor
anywhere else). `swipe.js` writes the note as `"swipe.html?order=<order>&at=<id>"` immediately
before it goes to `/login`, and nowhere else.

Also in `gallery.js`'s file header: the storage key it now reads, beside the one it owns.

## 6. Acceptance

`tests/test_gallery.py`:

- `render_index` writes `swipe.html` and `swipe.json`; `render_all` too; both pass `guard()`.
- `swipe.json` lists published entries only, in id order, with every key in §2 present for a root
  and a child alike; `parent_entry_id` and `critique_by` `null` for a root; `judgment.human` absent
  for an entry no human has judged; `brief` and `statement` **absent** from every row; `responds`
  present as `["click"]` for an entry whose assertions include `responds(click)` and absent for one
  with only `motion(idle)`; `mic` present and `true` only for a listening sketch; `meta` is
  `e/<id>/meta.json`.
- The same database renders the same `swipe.json` bytes twice, and the same `kiosk.json` bytes as
  before this change.
- Every page's nav carries `swipe.html` after `kiosk.html`; `swipe.html`'s own nav marks it and no
  other page's does.
- `swipe.html` contains no entry data, one `assets/swipe.js` script tag, the theme-color meta, and
  the start card's strings verbatim from the mockup.
- The grid at phone width offers the page: `grid.html` carries the `swipe-offer` line (§7, packet A)
  and `gallery.css` shows it only under `40rem`.

`tests/test_gallery_js.py`, node against `tests/js/dom.js` as the kiosk suite does (skipped where
node is absent). `tests/js/swipe.js` is the harness, built from `tests/js/kiosk.js`'s shape: the DOM
contract of §3 by ids, a stepped timer queue, a frame-by-frame rAF driver, and a `fetch` stub that
answers the reads from fixtures and records every write in `asked` with its `init`. `dom.js` gains
what the gestures need and nothing more — `setPointerCapture` and `releasePointerCapture` as no-ops,
`closest`, and a way for a test to dispatch a `pointerdown`/`pointermove`/`pointerup` sequence with
`clientX`, `clientY` and `pointerId` — as additive stubs that leave every existing test as it is.

- With a three-entry fixture, each of the seven orders yields the order the kiosk's comparator
  would; `random` a permutation; `?at=` seats on that id; `?order=liked` persists and the address
  bar reads `?order=liked&at=<id>` after the first seat and follows every seat.
- A drag of 0 → −80 px in y advances one seat and leaves exactly one iframe with
  `sandbox="allow-scripts"` and no other attribute; +80 px goes back; 40 px snaps back and changes
  nothing.
- A release inside 10 px and 300 ms toggles `quiet`; a press held 450 ms sets `touching` and does
  not toggle `quiet`; the pill clears it.
- A drag of +100 px in x signed out opens `sheet-signin` and posts nothing; signed in it posts
  `/like` once with `{entry_id, on: true}`, and a second time with `on: false`; a 401 empties the
  heart and opens the sign-in sheet. **Every `POST` in `asked` carries `Authorization: Bearer` when a
  token is stored, and no request carries `credentials` other than `"include"`.**
- A drag of −100 px in x opens `sheet-judge` with A's and B's briefs and **B's prompt, executor and
  `submitted_by` appear nowhere in the document**; tapping B's tile swaps the one iframe to B's
  `sketch`; each answer posts `/vote` with the pair's ids, the question and the choice; the reveal is
  hidden until both are answered and then shows the fixture's verdicts; *judge another pair* keeps
  A and changes B; *keep swiping* frames A again.
- 600 frames on one seat post exactly one `/view`, with `{entry_id}` and no `source`; a seat left in
  under ten seconds posts none; a hidden tab (`document.visibilityState` stubbed) posts none.
- With no `write_path`, no request to `base()` is made at all.
- The set of URLs in `asked` whose `init.method` is `POST` is a subset of `{/view, /like, /vote}`,
  asserted over the whole run; over the comment-stripped text of `swipe.js`, exactly three
  `fetch(` calls carry `method:` and their URLs are `base() + "/view"`, `"/like"`, `"/vote"`; and
  the set of every `base() + "/…"` literal in the file is exactly `/counts, /me, /logout, /login,
  /view, /like, /vote` — a request the page makes to the Worker that is not one of those is a bug
  whatever its method.
- `swipe.js`'s text names no `localStorage` key other than the three of §4.8, and never writes
  `sketchgen_session` except in `writeToken`.
- `gallery.js`: with `#session=tok` in the location and the return note set to
  `swipe.html?order=newest&at=5`, `ready()` stores the token, removes the note and calls
  `location.replace` with `ROOT + "swipe.html?order=newest&at=5"`; with the note set and no fragment,
  nothing is called and the note survives; with a note that does not match the pattern, it is
  removed and nothing is called.

## 7. The packets

Three agents in parallel worktrees, as the kiosk was built, then integration by hand on a worktree of
the lead's own (never the main checkout — other sessions switch its branch). Each packet is one
commit on its own branch off `feat/swipe`; the lead merges them into `feat/swipe` and opens one PR.
Deviations from this document go in the PR body, as the kiosk's did.

**Packet A — the generator** (Python, templates, CSS). `_swipe_entry`, `_swipe_manifest`,
`_swipe_page` in `gallery.py`, refactoring `_kiosk_entry`'s `_meta` call so the two share it;
`render_index` writing both files; `templates/swipe.html` from the mockup's markup and the DOM
contract; the `swipe` nav link after `kiosk` in the five shells; `grid.html` gaining
`<p class="swipe-offer"><a href="${root}swipe.html">On a phone? Swipe through the gallery →</a></p>`
under the sorts, and `entry.html`'s scanned strip gaining ` · <a href="../../swipe.html?at=$entry_id">Swipe on from here</a>`
inside a span that goes with the others (qr.md §6.2); the `/* ---- swipe ---- */` block in
`gallery.css` lifted from the mockup, plus `.swipe-offer { display: none }` shown under `40rem`;
`cli/gallery.py` help text; `tests/test_gallery.py`. Nothing in JavaScript.

**Packet B — the script** (JavaScript). `assets/swipe.js` per §4; `tests/js/swipe.js`; the additive
stubs in `tests/js/dom.js`; the cases in `tests/test_gallery_js.py`. The agent builds against the DOM
contract in §3 and the strings in the mockup, not against packet A's template, so the two can run at
once; the harness builds its page from the contract exactly as `tests/js/kiosk.js` does.

**Packet C — coming back** (JavaScript, small). `returnFromSignIn()` in `gallery.js` per §5, its
header comment, and its three cases in `tests/test_gallery_js.py` against the existing gallery
harness. Separate from B because it is the only change to a file every page loads and should be
reviewable on its own.

**Review.** One agent reads the three branches against this document and the mockup before the
lead integrates: the contract ids match, the strings match, every §6 case is present, and the
must-not list of §4.8 holds by inspection as well as by test.

## 8. Running it

1. Merge the PR. **Pull the gallery checkout on the node first** if anything was merged from
   outside the node.
2. `update.sh` on the node — pulls, restarts, runs `render-index`, which writes `swipe.html`,
   `swipe.json`, `assets/swipe.js`, `assets/gallery.js`, `assets/gallery.css`, and the nav and the
   phone-width offer on the index pages. Remember that a deploy which pulls a new `update.sh` runs
   the old copy.
3. **No Worker deploy** (§1.7).
4. The entry pages' nav and scanned strip land on the next `render-all` (the command is in
   `docs/plans/kiosk-views.md`'s neighbour, `sketchgen-deploy-order-gotchas`); nothing on the swipe
   page waits for it.
5. Verify on the live site, on a phone, not on `127.0.0.1` — a sandboxed frame cannot fetch a
   loopback address and the sketches render black locally. Open `swipe.html`, tap Start, swipe up
   three times, hold one that says it responds, like one, judge one, and watch the counts move on
   the grid.

## 9. Effort

Packet A is a day's packet, most of it tests; B is the kiosk's script packet again, minus the menu
and plus the gestures and three sheets — the largest of the three; C is an hour. The gesture engine
is under a hundred lines in the mockup and should stay so.
