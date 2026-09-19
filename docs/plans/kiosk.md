# Kiosk mode

A new page on the published gallery, `kiosk.html`, that plays sketches one after another, full
screen, on a monitor or a projector in a room. Any key press brings up a menu of key commands:
playback, order, and which words to show beside the sketch. Nothing on it writes anywhere.

Mockup (the visual spec; the copy in it is final): https://claude.ai/artifact/9guavhWHus2m3sa1hH1dcn
— and the same page as source, runnable offline, at `docs/plans/kiosk-mockup/kiosk.html`. The
mockup runs four real sketches in-page with p5 in global mode; the real kiosk runs them in the
sandboxed iframe the gallery already uses (§4.3). Everything else — layout, keys, strings, timing,
the launch link — is to be taken from it as written.

One packet, one agent, one branch `feat/kiosk`. It touches the **generator only**:
`sketchgen/gallery.py`, `sketchgen/templates/kiosk.html` (new), `sketchgen/templates/grid.html`,
`sketchgen/templates/entry.html`, `sketchgen/templates/compare.html`, `sketchgen/assets/kiosk.js`
(new), `sketchgen/assets/gallery.css`, `sketchgen/cli/gallery.py` (help text only),
`tests/test_gallery.py`, `tests/test_gallery_js.py`, `tests/js/`. **No database, no migration, no
write-path change, nothing in `worker.py`, `lineage.py`, `sync.py` or `writepath/`.**

Repositories:

- Pipeline: `profcarroll/sketchgen`, local clone `/home/dave/sketchgen`, on the node at
  `~/sketchgen/app`. Python 3.12, stdlib only, `python3 -m unittest discover -s tests` (no pytest).
  Templates are `string.Template` files in `sketchgen/templates/`; `$` in a template is a
  substitution, so JavaScript in a template escapes it as `$$`. Assets in `sketchgen/assets/` are
  copied verbatim by `render_index` and are not templates.
- Gallery: `profcarroll/sketchgen-gallery`, generated output only. This packet changes the
  generator; the gallery repository receives the result on the next `render-index` (§6). Do not
  hand-edit it.

Conventions: one PR, commit messages in the repo's existing voice (see `git log`), tests with the
change, no new dependency in either language. `gallery.js` is the house style for the script:
an IIFE, `"use strict"`, ES5 syntax, no build step, comments that say why.

## 1. Decisions already made

1. **A new page, not a mode of the grid.** `kiosk.html` is a shell like `compare.html`: one
   static page whose data arrive as a JSON file, rendered by the browser. The grid page stays
   what it is. The header nav on every page gains one link, `kiosk`, after `compare`.
2. **A manifest, not 222 fetches.** The kiosk needs the prompt, brief, statement, judgment and
   provenance of every published entry before it can order them, and `meta.json` per entry is
   one request per entry. `render_index` writes `kiosk.json` (§2) beside `pairs.json`, from
   the same rows and the same Bradley–Terry tables the cards are drawn from, so the two never
   disagree. Views and likes are not in it: they come live from `/counts`, as they do on every
   other page.
3. **Published entries only.** Not kept rejections. A projector in a lobby shows the gallery,
   and the gallery is the published set. `_entries(conn, "published")`, the same rows the grid
   page gets.
4. **The sketch runs in the same sandboxed iframe as everywhere else** — `sandbox="allow-scripts"`,
   `src="e/<id>/sketch/"`, nothing more (gallery.js `runInPlace` is the reference). The kiosk
   never evaluates sketch source in its own document; the mockup does that only because an
   artifact cannot frame the gallery. A sketch's own key handlers therefore never see a kiosk key
   and a kiosk key never reaches a sketch, which is the behaviour the mockup fakes by dropping
   `keyPressed`.
5. **A kiosk play is not a view.** ~~`sendView()` is scoped to `main.entry` and stays so. One
   screen left running would add a view a minute to whatever sorts first and bend the "most
   liked" and "most reviewed" neighbourhoods without a person choosing anything. The kiosk reads
   `/counts`; it never POSTs.~~

   **Reversed 19 September 2026 by the instructor, after using it. A kiosk play is a view**, on
   three conditions, and `docs/plans/kiosk-views.md` is the packet. The reason given above does
   not hold — no sort and no judge reads views, so nothing about the measurement or the
   neighbourhoods moves. The reason that does hold is elsewhere: `/view` de-duplicates a
   signed-in viewer and nothing else, and the kiosk signs nobody in, so the only restraint on it
   is the one `kiosk.js` keeps for itself.
6. **The first key only opens the menu.** A bumped keyboard must not skip a sketch. While the
   menu is open, keys act; it closes on `Esc` or after 8 s without a key. The status line's hint
   `any key · controls` is the only affordance and it is deliberately faint.
7. **One gesture up front.** A start card with one button precedes play, because a browser will
   neither go full screen nor resume an AudioContext without a gesture, and sketches now carry
   audio (`prompts/executor.md` §"Sound needs the one gesture"). The card's copy is in the mockup.
   Inside the sandboxed frame, however, that gesture does not carry: the frame gets its own
   gesture only when the viewer clicks *inside* it. A sketch with `responds(audio)` or a
   microphone will run silent in the kiosk until someone clicks the sketch, and that is accepted
   for this packet; the kiosk does not try to forward a click. Full screen (`F`) is on the kiosk
   document and works from the card's click onward.
8. **Settings live in the URL and in `localStorage`, in that order.** `?order=random&every=45
   &show=prompt,authors,code` sets the kiosk up with no keyboard at all, which is how a projector
   gets bookmarked; the menu footer prints the link for the current setup. Keys update both the
   address bar (`history.replaceState`, as `remember()` in gallery.js does for `?sort=`) and
   storage; a URL parameter beats a stored one; a stored one beats the default.
9. **Defaults.** 60 s each, `newest`, overlays `prompt, authors, generation, views, likes` on and
   the other eight off. `every` is clamped to 15–600 in 15 s steps.
10. **Order names are the grid's seven and mean the same thing.** `newest, oldest, random,
    liked, reviewed, controversial, consensus`, ordered by the same rules `gallery.js` uses
    (`byNewest` … `byConsensus`) but over the manifest's numbers instead of the cards' DOM. The
    `1`–`7` keys pick them in that order; `S` cycles. Changing the order keeps the current
    sketch on screen and re-seats it in the new sequence (`reorder()` in the mockup).
11. **No web font, no CDN, nothing fetched but `kiosk.json`, `config.json`, `/counts` and the
    frames.** The page is `gallery.css` plus one kiosk block appended to it, in the gallery's
    dark palette with the ground pushed to black. The kiosk is single-theme: a projector has no
    light mode. `body.kiosk` sets the ground explicitly and every colour is a token.

Still the instructor's to overrule before work starts: the eight-second menu timeout in 6, and
whether a kiosk play should count as a view in 5. *(The second of those was confirmed as written,
then reversed after the kiosk had been used in a room — see 5.)*

## 2. `kiosk.json`

Written by `render_index`, after `pairs.json`, indented like it (a person may open it), sorted
keys, trailing newline. Published entries only. The same database gives the same bytes: no clock
is read, and lists are in entry-id order.

```json
{
  "entries": [
    {
      "id": 93,
      "prompt": "A slow tide of overlapping translucent circles…\n\nRevise: …\n\nRevise: …",
      "brief": "The canvas displays a slow, continuous cosmic environment…",
      "statement": "This sketch creates a serene cosmic scene…",
      "submitted_by": "profcarroll",
      "planner": "gemma4:e4b",
      "executor": "qwen3-coder:30b-a3b-q4_K_M",
      "rules_file": "control",
      "attempts": 1,
      "seed": 1,
      "created_utc": "2026-09-14T19:05:45Z",
      "published_utc": "2026-09-14T19:10:25Z",
      "prompt_tokens": 1169,
      "completion_tokens": 561,
      "wall_s": 64.803,
      "licence": "CC BY 4.0",
      "generation": 7,
      "parent_entry_id": 70,
      "critique_by": "gemma4:e4b",
      "root_entry_id": 1,
      "sketch": "e/93/sketch/",
      "source": "e/93/sketch/sketch.js",
      "href": "e/93/",
      "canvas": [800, 600],
      "judgment": {
        "human": { "look": { "score": 1.57, "n": 1, "pct": 0.83 },
                   "brief": { "score": 1.4, "n": 1, "pct": 0.7 } },
        "agent": { "look": { "score": 2.16, "n": 4, "pct": 0.91 },
                   "brief": { "score": 1.9, "n": 4, "pct": 0.88 } }
      }
    }
  ]
}
```

Field by field, and where each comes from — every one is already computed for `meta.json` or a
card, so this is a second serialisation of existing values, not new logic:

- `prompt, brief, statement, submitted_by, planner, executor, rules_file, attempts, seed,
  created_utc, published_utc, prompt_tokens, completion_tokens, wall_s, licence` — the same
  values, same names, as `_meta()` writes to `meta.json`. `statement` verbatim and unedited, as the
  entry page's panel is.
- `generation, parent_entry_id, critique_by, root_entry_id` — the entry's `lineage` block in
  `meta.json`, flattened.
- `judgment.<population>.<question>` — `_standing(table, entry_id)` over `_all_scores(conn)`,
  keeping `score`, `n`, `pct` and dropping `rank` and `pool`; **absent** (the key is missing, not
  `null`, not `0`) when that population has not judged the entry on that question. A score nobody
  voted on is not a low score; the browser prints *no pairs yet* for a missing key, as the entry
  page does.
- `sketch, source, href` — relative to the gallery root, the same strings `_compare_page`
  builds.
- `canvas` — `[width, height]` when `createCanvas` is called with two integer literals; absent
  otherwise (§4.3).

`kiosk.json` goes through `guard()` with everything else `render_index` writes: no email-shaped
string, no `instance-`. `submitted_by` is a GitHub login, which is what the entry page already
prints.

## 3. The page: `sketchgen/templates/kiosk.html`

The shell. The mockup's markup, with three substitutions: the header bar the other templates share
(`$root`, the nav with `kiosk` current), `<script>window.SKETCHGEN_ROOT = "$root";</script>`, and
`<script src="${root}assets/kiosk.js"></script>` in place of the inline script. No entry data in
the page: `kiosk.js` fetches `kiosk.json`. `_kiosk_page(config)` in `gallery.py` renders it and
`render_index` writes `dest / "kiosk.html"`.

The header bar is present in the DOM so that the page is still the gallery with JavaScript off and
so a viewer knows where they are, but the kiosk hides it (`body.kiosk .bar { display: none }`)
the moment the start card is dismissed. Before that, the card sits over it.

The stage layout, in words, for the CSS (the mockup's stylesheet is the reference and may be
lifted into `gallery.css` under a `/* ---- kiosk ---- */` heading, tokens renamed to the gallery's):

- A three-column grid pinned to the viewport: the **code column** (left, `clamp(20rem, 30vw,
  40rem)`), the **stage** (`minmax(0, 1fr)`), the **words column** (right, `clamp(18rem, 26vw,
  34rem)`). Each region is pinned to its column with `grid-column`; a hidden column is
  `display: none` and the stage takes its width. (The mockup had this wrong for one version and
  fixed-size sketches sat left; do not repeat it.)
- The stage centres the frame. A fixed-size sketch (`createCanvas(800, 600)`) is scaled to fit the
  stage and letterboxed on black; a window-sized one fills it. See §4.3 for how the kiosk learns
  the size.
- The **caption** overlays the bottom of the stage under a soft scrim: prompt, revisions line,
  authors line, facts row. `pointer-events: none`, so a click lands on the sketch.
- The **progress line**, 2 px, top edge, accent; grey while paused.
- The **status line**, top right, small caps: `paused · 3 of 222 · newest · any key controls`.
- The **menu**, bottom centre, three columns Playback / Order / Overlays and a footer with the
  launch link and the views-and-likes note. Below 760 px the grid becomes a single column, the
  stage 62dvh, columns under it, the menu one column.
- The cursor hides after 3 s without movement.

## 4. The script: `sketchgen/assets/kiosk.js`

Its own file, not a block in `gallery.js`: the grid's script is already long, and the kiosk shares
only three helpers with it. Load both — `gallery.js` for `base()`, `paintCounts`-style count
fetching and the session line, then `kiosk.js` — and have `kiosk.js` read the write-path base from
`config.json` the way `gallery.js` does (`ROOT + "config.json"`, `cache: "no-store"`). If reaching
into `gallery.js`'s closure proves awkward, duplicate the twenty lines rather than export anything
from it; say so in a comment.

### 4.1 Keys

| key | while the menu is open |
| --- | --- |
| any key | opens the menu; the first press does nothing else |
| `Esc` | closes it (or 8 s idle) |
| `space` | pause / play |
| `→`, `N` | next sketch |
| `←` | previous sketch |
| `]` / `[` | 15 s longer / shorter, 15–600 |
| `F` | full screen on / off |
| `H` | hide every overlay / show them again |
| `S` | cycle the order |
| `1`–`7` | pick an order: newest, oldest, random, liked, reviewed, controversial, consensus |
| `P A G V L B T J C I D K W` | toggle prompt, authors, generation, views, likes, brief, statement, judgment, code, licence, date, tokens, seconds |

Modifier chords are ignored, so a browser shortcut stays a browser shortcut. Every acting key
repaints the menu, restarts its 8 s timer, and persists (§1.8).

### 4.2 Sequence and timer

- `sequence(order)` returns entry ids from the manifest: the seven comparators from `gallery.js`
  restated over manifest fields — `liked` over the live counts (an entry whose count has not
  arrived sorts as zero), `reviewed` over the sum of `judgment.*.*.n`, `controversial` and
  `consensus` over `|human.look.pct − agent.look.pct|` with either missing sorting last, `random`
  a Fisher–Yates shuffle. Ties break newest first, then by id, as the grid does.
- `every` seconds per sketch, counted by `requestAnimationFrame` deltas so a paused tab does not
  skip ahead; the progress line is `elapsed / every`. At the end, a 420 ms fade to black, the
  next frame, a fade in. Manual `next` and `previous` do the same fade.
- `random` reshuffles when the sequence wraps, so a day-long run is not one permutation.
- Counts: one `GET /counts?entries=…` for the whole manifest at start, in chunks of the size
  `gallery.js` uses, and again every ten minutes; the caption reads from the latest answer. With
  no write path in `config.json`, views and likes print `—` and `liked` falls back to `newest`.

### 4.3 The frame

`showEntry(entry)` removes the previous iframe, creates a new one with the sandbox from
gallery.js's `runInPlace`, and appends it to the stage. There is no `postMessage` channel to the
sketch — `allow-scripts` without `allow-same-origin` is opaque by design — so the kiosk cannot ask
the frame how big its canvas is, and a fixed-size canvas inside a stage-sized frame sits top-left,
where p5 puts it. The generator knows, so the generator says:

- At render time, `_kiosk_entry()` reads the sketch's source for the literal pair
  `createCanvas\(\s*(\d+)\s*,\s*(\d+)` and writes `"canvas": [800, 600]` into the entry's
  manifest row. No literal pair (`windowWidth`, a variable, an expression) means no `canvas` key.
- A row with `canvas` gets a frame of that aspect ratio, scaled to fit the stage and centred by the
  stage's grid — a frame the size of the canvas has nothing to be off-centre in. A row without one
  gets a frame that fills the stage, which is what a window-sized sketch wants.
- The frame is resized with the window; the sketch's own `windowResized` handles the rest.

One regex, one key, one test. A sketch that declares 800×600 and then resizes itself is shown at
800×600 and clipped; that is rare, and the entry page shows it the same way.

### 4.4 Overlays

The caption's contents follow the mockup's `paint()` exactly, string for string:

- `prompt`: `#<id>` faint, then the root prompt at display size. A revised entry (`\n\nRevise:`
  in the prompt, split as `_title_parts` splits it) adds one line: **revised N times, latest:**
  and the last revision, clamped to three lines.
- `authors`: *Prompted by X · planned by Y · written by Z under the control rules*, and *, gate
  passed on attempt N* when N > 1.
- `generation`: **generation N** · *revised from #P after a critique by C* or *· a root*.
- `views`, `likes`, `date` (*created 14 Sep 2026 19:05 UTC*), `tokens` (*1,169 prompt + 561
  completion tokens*), `seconds` (*written in 64.8 s*), `licence` — one fact each in the facts row.
- `brief`, `statement`, `judgment` open the words column with the panels and notes the mockup
  shows, the statement heading naming the executor and *unedited*. Judgment prints per
  population *1.57 over 1 pair* and the quadrant phrase the entry page uses (`_compass_title`'s
  words: *looks good, on brief* etc.), or *no pairs yet*.
- `code` opens the code column, fetches `source` once per entry, prints it with line numbers,
  and scrolls it past at 18 px/s after a 4 s hold, resting at the end. The heading names the
  file and its size in bytes and says *unedited*.

### 4.5 What `kiosk.js` must not do

~~POST anything.~~ Write anything but the one view of §1.5 as amended — one `POST <base>/view`,
after ten playing seconds on a seat, once per seat, and not at all once the room has gone eight
hours without a key or a mouse. Read `document.cookie`. Present an identity of any kind: `/counts`
is a public read and `/view` takes an anonymous write, and a projector that presented one would
file every sketch it played under whoever last signed in on that machine. Evaluate sketch source.
Touch `localStorage` keys other than `sketchgen-kiosk`. Hold more than one iframe at a time.

## 5. Acceptance

`tests/test_gallery.py`:

- `render_index` writes `kiosk.html` and `kiosk.json`; `render_all` too; both pass `guard()`.
- `kiosk.json` lists published entries only — a kept rejection and a held entry are absent — in
  id order, with every key in §2 present for a root and a child alike, `parent_entry_id` and
  `critique_by` `null` for a root, `judgment.human` absent for an entry no human has judged and
  present with `score`, `n`, `pct` for one they have.
- The same database renders the same `kiosk.json` bytes twice.
- Every page's nav carries `kiosk.html` after `compare.html`; `kiosk.html`'s own nav marks it.
- `kiosk.html` contains no entry data, one `assets/kiosk.js` script tag, and the start card's
  strings verbatim from the mockup.
- An 800×600 sketch gets `"canvas": [800, 600]`, a `windowWidth` one gets no key, and
  `createCanvas(w, h)` with variables gets no key.

`tests/test_gallery_js.py`, node against `tests/js/dom.js` as the run-in-place test does (skipped
where node is absent):

- With a three-entry manifest fixture, each of the seven orders yields the order the grid's
  comparator would; `random` yields a permutation.
- The first key press opens the menu and changes nothing; `→` with the menu open advances;
  `Esc` closes; `]` and `[` clamp at 600 and 15.
- Toggling `C` adds one iframe with `sandbox="allow-scripts"` and no other attribute; advancing
  leaves exactly one iframe in the document.
- `?order=liked&every=45&show=prompt,code` on load sets the state, and the launch link in the
  menu footer prints that string back.
- ~~No `fetch` call in the script is a POST~~ Exactly one `fetch` call in the script is a POST and
  its URL is `base() + "/view"` (assert on the text of the file, as the suite already reads
  scripts as text), and none of them carries a credential. The behaviour behind it is
  `docs/plans/kiosk-views.md` §4.

## 6. Running it

One branch from `main`, one PR. It is merged by the instructor, and deployed in the usual order:

1. `update.sh` on the node — pulls, restarts, and runs `render-index`, which writes `kiosk.html`,
   `kiosk.json`, the two assets and every page's nav. **Pull the gallery checkout first** if a PR
   was merged from outside the node, and remember that a deploy which pulls a new `update.sh`
   runs the old copy.
2. Nothing under `e/` changes: the canvas size lives in the manifest and the entry pages are
   untouched, so `render-all` is not needed.
3. Open `https://profcarroll.github.io/sketchgen-gallery/kiosk.html` on the projector, press
   Start, press `F`.
