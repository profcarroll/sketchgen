# A gallery of its own: rendering locally, for review and for the wall

Every instance of sketchgen can render a gallery today, but only into the one place that is
pushed to GitHub Pages, and only of what has already been published. This plan adds the other
target: **a local render**, of any database, into any directory that is not a git checkout,
served from the machine that shows it. It is what the operator asked for on 2026-09-28 — a
gallery an instance generates for itself, viewable locally, that a kiosk can run from without
going through Pages — and it turns out to be the piece the other two publishing ideas were
each missing.

Two packets. Neither touches `publish.py`, the Worker or the public gallery.

Repositories and conventions as in `held-batch.md`: `profcarroll/sketchgen`, Python 3.12, stdlib
only, `python3 -m unittest discover -s tests`, no network in a test. One packet per branch.

## 0. What is true today

### 0.1 The renderer never needed Pages

`gallery.render_index` and `gallery.render_entry` write a static tree into whatever directory
they are handed. Every link in it is relative: `kiosk.js` starts from `ROOT = "./"`, the grid
links `e/<id>/`, an entry page links `../../assets/`. Git appears only in `publish.py`, which
renders *into* the checkout and then commits and pushes it. So a second target is not a second
renderer. What ties the tree to Pages is four things, and each is a line or a field:

| tie | where | what a local copy does with it today |
| --- | --- | --- |
| **which rows** | `_entries` (`gallery.py:497`) reads `published_utc IS NOT NULL`; `_public_rows`, `PUBLIC_STATES`, `_manifests` all go through it | a held entry cannot be rendered at all: `render` refuses it, `render-all` skips it, `kiosk.json` never lists it |
| **the QR code** | `render_index` writes `qr.svg` / `qr-kiosk.svg` from `config.gallery_url`; `kiosk.json` rows carry `url` | an unpublished entry's code would lead to a 404. `kiosk.js:1144` already shows no code when a row has no `url` |
| **the write path** | `config.write_path`; the Worker's CORS allows exactly one origin, `GALLERY_URL` (`writepath/worker.js` `corsHeaders`) | from `http://127.0.0.1` every `/counts`, `/view`, `/like` is refused by the browser. `kiosk_views: false` and an empty `write_path` switch the calls off cleanly |
| **the frames** | `webimg.web_or_png` (`gallery.py:873`) publishes `strip.webp` when a web copy exists, else the 2 MB PNG | a held entry has no web copy until something calls `webimg.ensure` for it |

p5 itself comes from cdnjs (`executor.py:102`), so a local gallery still needs the internet for
the sketches to run. Every room we have has it; §7 leaves vendoring p5 for the room that does
not.

### 0.2 The other two ideas, and what they lacked

- **The hub** (`gallery-hub.md`) was for several nodes publishing *continuously*. Today there
  is one: `sld-gpu` was terminated on 2026-09-27, and on the 25th the D12 pair became student
  compute with sketches paused (`mfadt/d12-compute`). The Pages tree is 349 MB of its 1 GB. The
  hub's reason is parked, not gone — §6 says what brings it back — and a local render is what
  a hub spoke would upload, so nothing here is thrown away.
- **The import** (`gpu-fold-in.md`) is still needed, but it chooses *before anyone looks*: its
  §1.2 selects about 200 of the 3,255 by filter because Pages cannot hold the rest. A local
  render removes that reason. The archive can be looked at whole, and the import takes only
  what a person picked (§4).
- **A public gallery per instance** would multiply the size problem and keep every pair inside
  one node. What it offered — each instance can see its own work — is what a local render
  gives, without the publishing.

### 0.3 The spike, 2026-09-28

The one thing nobody knew: whether a sketch in the gallery's sandboxed frame runs when the
gallery is served from loopback. On 2026-09-19 frames went black in the Browser pane on
`127.0.0.1`, and the cause was never found (it was noted and the frames were checked on Pages
instead).

Run on the D12 Mac (Chrome 154, macOS 15.8) in a second, headless Chrome with its own profile
and DevTools port, so the wall's Chrome (port 9222, which the watchdog reads) never saw it; the
wall counted straight through. The tree was 20 published entries copied from the gallery
checkout — the newest eight, plus a WebGL one, a `p5.sound` one, a `loads(image)` one, a
`createGraphics` one, several that answer the ghost pointer — with `config.json` set to
`kiosk_views: false` and no write path, served by `python3 -m http.server --bind 127.0.0.1`.

Each entry was loaded exactly as the kiosk loads it — one `<iframe sandbox="allow-scripts">` —
and asked, from inside its own frame, whether p5 was there and `frameCount` was advancing:

| served from | frames running | the one that was not |
| --- | --- | --- |
| `http://127.0.0.1:8090` | 19 of 20 | e/20, which calls `noLoop()` and drew its scene |
| `http://localhost:8090` | 19 of 20 | the same |
| `https://profcarroll.github.io` (control) | 19 of 20 | the same |

Every frame's origin was opaque (`null`), as on Pages. The console was the same on all three:
p5 asking for the accelerometer and the sandbox refusing it, nothing else. The `loads(image)`
sketch fetched its pictures from a remote host and drew them. Then the real `kiosk.html` from
loopback, `?unattended=1&every=15&views=0`, for 50 s: it advanced through four entries, every
frame ran, the QR code on a published entry pointed at its github.io page, and the title read
`sketchgen kiosk · #1788 · not counting: no write path · 0a362d` — the kiosk already says what a
local wall is.

So whatever blacked out the frames on 2026-09-19 was not loopback as such; the Browser pane
itself is the suspect, and it was not tried again. **A local gallery runs in the Chrome a wall
uses, unchanged**, and nothing in §1 is there to work around the serving. One caveat: this was
headless Chrome, not the wall's full-screen window. The first thing Packet 2's session on a Mac
does is the same page in a headed kiosk window, by eye.

## 1. Decisions

### 1.1 One renderer, two targets

`sketchgen render-local` calls the same `render_index` and `render_entry` the publisher calls,
with a different row set and a different config. There is no second template, no second asset
set, no fork of `kiosk.js`. A template change reaches both targets by the same render.

### 1.2 A local render is private, and cannot be pushed

It refuses, exit 3 and nothing written, a destination that is inside a git work tree, and a
destination that is the gallery checkout (`publish.DEFAULT_GALLERY_DIR`, which
`$SKETCHGEN_GALLERY` sets). The publisher's destination is always a checkout, so the two can
never be the same directory. Every page it writes carries
`<meta name="robots" content="noindex">` and a banner:

> Local render of **sld-gpu**'s archive · 3,255 held · not published · 2026-09-28

The banner is one line in `grid.html`, `entry.html` and the kiosk's menu footer, present only
when `config.local` is set. The kiosk's wall shows nothing of it while a sketch plays.

### 1.3 Which rows

`--include` takes `published` (the default: the public gallery, rendered locally), `held`, or
both. `--ids FILE` narrows to a list, one id per line — the shape `import list --ids` prints
(`gpu-fold-in.md` §2.2), so an archive can be pre-filtered before it is rendered. The row set is
threaded through `_entries` as one argument; `_manifests` gets the same rows, so `kiosk.json`
and `swipe.json` list exactly what the grid lists.

`failed-kept` and `rejected` stay on `rejections.html` as they are: a held archive has none that
anyone has looked at, and the local render is for looking.

### 1.4 The personal-data scan runs here too

`publish.scan_for_personal_data` has only ever seen entries on their way out. A held entry has
not. The kiosk shows a sketch's source in its *source* overlay (`kiosk.js` fetches
`entry.source`), and a wall is a public place even when its server is not. So `render-local`
scans every held entry before rendering it, and one that fails is **left out**, not refused
whole: `skipped e/412: hostname-shaped string in sketch.js`. The count of skipped entries is the
last line.

### 1.5 QR codes only where there is a page to land on

A row with `published_utc` gets its code and its `url`, pointing at the public page, as now. A
row without gets neither, and `kiosk.js` already draws no code for it. No code on a wall ever
points at a local address: the phone in the visitor's hand is not on the kiosk's loopback.

### 1.6 No counting, until the Worker is asked

The local config writes `kiosk_views: false` and `write_path: ""` unless `--write-path` is given.
A wall on a local render therefore counts nothing, and its title already says so (`not
counting: no write path`, §0.3), which the watchdog reads. Counting from a local origin is a
Worker change — `GALLERY_URL` becomes a list of allowed origins — deployed by hand, D1 first
(AGENTS.md, *Deploying*). It is not in this plan; §8 asks whether it should be the next one.

### 1.7 Picks: the one thing the local render adds to a page

When `config.local` is set, every card and entry page gets a **Pick** toggle. Picks live in the
browser's `localStorage`, keyed by the render's origin label, and the banner has **Copy picked
ids**: one id per line, the file `import run --ids` reads. Nothing is posted anywhere and there
is no server to post to. A person on one laptop, in one browser, sifting — which is how the
operator judges (portfolio-first, strongest first) — and the list leaves the page by the
clipboard. `gallery.js` gains the toggle behind the flag; the kiosk does not, since nobody picks
from a wall.

### 1.8 Web frames for held entries

`render-local` calls `webimg.ensure` for every entry it renders, as `publish` does. The first
render of the archive makes 3,255 WebP strips with the gate's own Chromium, so it runs `nice`d
and **with the generator paused**: a gate sharing the CPU with it reads slower frame times, and
`frame_budget` is a verdict. The copies are cached for every render after — beside the PNG for
a node's own jobs, in `--web-cache` for a read-only archive (§2.1). `--no-web` skips them and
copies PNGs, for a quick look at a handful.

### 1.9 Served from the machine that shows it

A wall does not reach back to a node for its gallery. The tree is rendered on the node, copied
to the kiosk Mac, and served there on `127.0.0.1`:

- the node's operator UI binds loopback only (`web.check_bind`), and a sketch server on the
  node would put model-written code on a port of the machine that runs the models;
- the wall keeps playing when the node, the tunnel or the lab's route to OCI is down;
- `launch-kiosk.sh` already waits for its gallery with `curl` before it starts Chrome, and
  loopback answers at boot before Wi-Fi does.

The copy is by hand for the first use (`rsync` from the laptop, which already reaches both).
A standing pull — the Mac fetching from the node on a timer — needs a key on the wall that can
read the node, and is not needed until a wall runs local for good.

## 2. Packet 1 — `feat/render-local`

### 2.1 The verb

```
$ sketchgen render-local --db ~/sketchgen-backups/sld-gpu/sketchgen/sketchgen.db \
      --jobs ~/sketchgen-backups/sld-gpu/sketchgen/jobs \
      --out ~/sketchgen-local/sld-gpu --include held --origin sld-gpu
rendering 3255 entries from a read-only snapshot (schema 17)
skipped e/412: hostname-shaped string in sketch.js
…
rendered 3251 of 3255 → ~/sketchgen-local/sld-gpu (~750 MB); 4 skipped by the scan
3251 web frames made, 0 cached
```

- `--db` is opened **read-only** (`file:…?mode=ro`, `uri=True`). The archive on sld-cloud is the
  only copy of the rental's frames (`gpu-fold-in.md` §0); nothing about looking at it may write
  to it. `webimg.ensure` writes its WebP beside the PNG, so it gains a cache directory, and
  for a read-only archive `--web-cache DIR` puts them there, keyed by the PNG's path and mtime.
  The numbers in the example are illustrative; the size is `gpu-fold-in.md` §1.2's estimate.
- `--jobs` is where the rows' `jobs/<n>` paths resolve, since a snapshot's paths name the node it
  came from. The paths are resolved, never rewritten.
- `--origin` is the banner's name and the picks' key. Required: no table records which node a
  database belongs to (`meta` holds only worker and build stamps), and a guessed name on a
  page is worse than a refusal.
- `--write-path URL` for §1.6, when the Worker allows it. Absent, counting is off.
- Refusals (exit 3, nothing written): the destination is in a git work tree or is the gallery
  checkout (§1.2); the schema is newer than this code's; `--out` exists and holds something
  that is not a previous local render (it must carry the `.sketchgen-local` stamp this verb
  writes).

A second run into the same directory re-renders it, and removes entry folders for rows no longer
in the set, as `render_index` already removes stale `page-*.html`.

### 2.2 In the renderer

- `Config` gains `local: str` (the origin label, empty for the public gallery) and `robots`,
  and round-trips them. `publish-index` never sets them; a checkout's `config.json` never has
  them.
- `_entries(conn, state)` gains a `rows=` parameter (`"public"` — today's clause — or
  `"local"`, with an id set). `render_index` and `render_entry` take it and pass it down. The
  public call sites change by nothing but a keyword with today's value.
- `_kiosk_entry` omits `url` and `qr` for a row without `published_utc` (§1.5), and
  `render_index` writes no `qr*.svg` for it.
- `grid.html`, `entry.html`, `kiosk.html`: the banner, the `noindex` meta, the Pick toggle
  (§1.7), each behind `config.local`.

### 2.3 Tests

`test_gallery.py`, against a scratch database built in `setUp`, as the existing ones are:

- a held entry renders under `rows="local"` and is refused under `"public"`, as today;
- the public render of the same database is byte-identical before and after this packet — the
  proof that the publisher's output did not move;
- no `qr.svg`, no `url` in `kiosk.json` for an unpublished row; both for a published one;
- the banner, `noindex` and Pick toggle present under `local`, absent otherwise;
- every refusal in §2.1 refuses before any file exists; the read-only open refuses a write;
- an entry the scan catches is skipped and named, and the rest render.

`tests/js/picks.js`, a new standalone driver beside `tests/js/kiosk.js`: the Pick toggle
writes and clears `localStorage`, and *Copy picked ids* produces one id per line in id order.

## 3. Packet 2 — `feat/kiosk-local`

The kiosk kit (`kiosk-mac/`) learns to serve a local gallery.

- `org.sketchgen.gallery.plist`, a LaunchAgent with `KeepAlive`: serves
  `~/Library/sketchgen-kiosk/gallery` on `127.0.0.1:8090`, nothing else. The server is
  `python3 -m http.server --bind 127.0.0.1`, **only where `xcode-select -p` succeeds**: on a Mac
  without the Command Line Tools, `/usr/bin/python3` is a stub that puts an install dialog over
  the wall. `probe.sh` already reports the CLT; `install.sh` refuses the agent without them
  and says how to install them. (The D12 mini has them; the two M4 minis are unknown.)
- `launch-kiosk.sh` reads its URL from `~/Library/sketchgen-kiosk/url` when that file exists,
  so switching a wall between Pages and its local copy is one line and a `kickstart`, and its
  existing `curl` wait covers both.
- `kiosk-mac/sync-local.sh HOST DIR`, run **from the laptop**: `rsync` a local render from the
  node to the Mac's gallery directory, into a sibling and renamed into place, so the wall never
  reads a half-copied tree. It prints the kiosk's new `build` from `kiosk.json`, which is what
  the page's own reload check (`kiosk-mac.md`, Packet A) watches.

The watchdog does not change: it reads the title of whatever page has `/kiosk.html` in its URL.

## 4. Using it: the sld-gpu archive

1. Back the archive up somewhere that is not sld-cloud. It is 8.5 GB on one instance and none of
   this needs it moved, but all of it depends on it (`gpu-fold-in.md` §7, decision 5).
2. On sld-cloud, between batches, generator paused:
   `render-local … --include held --origin sld-gpu --out ~/sketchgen-local/sld-gpu`.
   Resume.
3. `rsync` the tree to the laptop. The operator sifts it in a browser from
   `python3 -m http.server`, picking; *Copy picked ids* → `picks.txt`.
4. Optionally, the same tree on one of the new M4 minis, as a wall in the room, uncounted, while
   the D12 wall stays on Pages and keeps counting. Whether GPU-made work earns more attention is
   something to watch, not to measure yet (the 2026-09-24 refocus: the shortage is people
   looking, not sketches).
5. `gpu-fold-in.md` Packet 1's `import run --ids picks.txt`, then publish from Held as for any
   batch.

This changes `gpu-fold-in.md` in two places, and this PR edits them: §1.2's "a selection,
because of Pages" becomes the limit on what is *published*, not on what is *seen*; and §4's
first import takes its ids from the picks rather than from `import list --clean
--one-per-prompt`. The filters stay useful as `--ids` for a first render narrower than all
3,255.

## 5. Acceptance

1. `render-local` of sld-cloud's own database with `--include published` gives a tree whose
   entry folders match the public gallery's, and `render-local` into the gallery checkout is
   refused.
2. The public `render-all` of a scratch database is byte-identical before and after Packet 1.
3. `render-local` of the sld-gpu snapshot with `--include held` leaves the snapshot's sha256
   unchanged and renders every entry the scan passes; its `kiosk.json` has no `url` on any row.
4. A Mac with the kit serves that tree on `127.0.0.1:8090`, and `kiosk.html` from it plays
   sketches, with no QR code and no counting, and its title says so.
5. Five entries picked in the browser come out of *Copy picked ids* as five lines that
   `import run --dry-run --ids` accepts.

## 6. What is parked, and what brings it back

- **The hub** (`gallery-hub.md` Steps 1–3): when a second node generates continuously again, or
  the Pages tree passes about 700 MB, or the 25-minute re-render after each judgment is what
  stops the operator from judging. Step 1 (mutable data to D1) first in every case.
- **A public gallery per instance:** not planned. A local render gives each instance its own
  gallery; making one public is the hub's job, not a second Pages repository's.
- **A web server at `davidcarroll.org`:** if it is ever built, model-written sketches are served
  from another site (`gallery-hub.md` §1.6), and a local render is the tree it would serve.

## 7. Out of scope

- **Vendoring p5** into the tree, for a room with no internet. A local render still loads p5
  from cdnjs. It would be a rewrite of every `sketch/index.html`'s `<script>` tag at render
  time, and it makes the local copy differ from what the gate ran.
- **Picks on the wall.** Nobody picks from a kiosk.
- **Serving a local render from a node** to other machines. §1.9.
- **Judging a local render.** Blind pairs and verdicts stay on the node and the Worker; a local
  gallery shows standings a published entry already has, and none for a held one.

## 8. Decisions for the operator

1. **The origin label on the page**: the fleet name (`sld-gpu`, recommended — already public in
   this repository), or a description (*rented A10, Sept 2026*).
2. **Where the picks live**: `localStorage` in one browser (recommended; nothing to build on a
   server), or a file the local server writes (needs a server that accepts a POST, which
   `http.server` does not).
3. **The first local wall**: an M4 mini beside the D12 wall (recommended), or the D12 wall
   itself switched to local, which stops its counting until the Worker is changed (§1.6).
4. **Whether the Worker should allow a second origin** so a local wall can count views. A
   separate plan if yes.
