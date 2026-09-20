# A QR code per sketch

Every entry gets two QR codes of its own URL, generated at render time and written into the
entry's directory: `e/<id>/qr.svg`, which holds the canonical URL, and `e/<id>/qr-kiosk.svg`,
which holds the same URL with `?kiosk` on it. The kiosk shows the second beside the sketch, as a
fourteenth overlay on the `Q` key, so that someone standing in front of a projection can take out
a phone, scan what is on the wall, and land on the entry page — where the page knows they arrived
by scanning and says what they can do about it: judge it, like it, ask for a revision. The entry
page shows the first code small, for the same reason on a smaller screen.

There is no mockup. The QR card is a white tile, a line of copy and a URL; §5.2 and §6 give its
layout in words and §5.3 fixes the strings.

One packet, one agent, one branch `feat/qr`. It touches the **generator only**: `sketchgen/qr.py`
(new), `sketchgen/gallery.py`, `sketchgen/templates/entry.html`, `sketchgen/assets/kiosk.js`,
`sketchgen/assets/gallery.js`, `sketchgen/assets/gallery.css`, `docs/plans/kiosk.md` (one
sentence, §5.1), `tests/test_qr.py` (new), `tests/test_gallery.py`, `tests/test_gallery_js.py`.
**No database, no migration, no write-path change, nothing in `worker.py`, `publish.py`,
`lineage.py`, `sync.py` or `writepath/`.**

Repositories:

- Pipeline: `profcarroll/sketchgen`, local clone `/home/dave/sketchgen`, on the node at
  `~/sketchgen/app`. Python 3.12, **stdlib only**, `python3 -m unittest discover -s tests` (there
  is no pytest here). Templates are `string.Template` files in `sketchgen/templates/`; `$` in a
  template is a substitution, so JavaScript and CSS in a template escape it as `$$`. Assets in
  `sketchgen/assets/` are copied verbatim by `render_index` and are not templates.
- Gallery: `profcarroll/sketchgen-gallery`, generated output only. This packet changes the
  generator; the gallery repository receives the result on the next render (§8). Do not hand-edit
  it.

Conventions: one PR, commit messages in the repo's existing voice (see `git log`), tests with the
change, **no new dependency in either language**. `kiosk.js` is the house style for the script: an
IIFE, `"use strict"`, ES5 syntax, no build step, comments that say why.

This packet builds on the kiosk (PR #101, merged) and amends two of its decisions: the overlay
defaults in `docs/plans/kiosk.md` §1.9 and the key table in its §4.1.

## 1. Decisions already made

1. **The generator draws the code, at render time, in Python.** Not a JavaScript encoder in the
   browser, and emphatically **not** a third-party image service. A `<img
   src="https://api.qrserver.com/…?data=…">` or a Google Chart URL would put one request per
   published entry — carrying the entry's identity — onto a machine neither we nor the viewer
   controls, on a page whose whole claim is that it fetches nothing but its own files
   (`kiosk.md` §1.11). One encoder, in one language, is also one thing to test.
2. **No dependency.** `qrcode`, `segno` and `qrcodegen` are not installed on the node and will not
   be. The pipeline is stdlib-only by design: `requirements.txt` exists for Playwright alone,
   because the gate is the referee. A QR encoder for one fixed case — a short ASCII URL — is
   about 400 lines and is specified in full in §2, so this is not a research task.
3. **SVG, not PNG.** A projector scales it without a second thought, it is text so the guard reads
   it (`TEXT_SUFFIXES` already lists `.svg`), it diffs in the gallery repository, and it is
   ~2 KB against a PNG's ~1 KB of opaque bytes. No `PIL`, which the renderer does not import.
4. **Two files per entry, `e/<id>/qr.svg` and `e/<id>/qr-kiosk.svg`, written by `render_index`.**
   The codes depend on the entry's id and `config.gallery_url` and on nothing else about the
   entry, so they do not need the entry re-rendered to be correct. `render_index` already writes
   `e/`-adjacent things nobody thinks of as the index (`lines/*.html`), `publish_index` stages
   the checkout with `git add -A`, and `update.sh` runs `render-index` on every deploy. That is
   what makes this feature deployable without a `render-all` for the codes themselves — the one
   `render-all` in §8 is for the entry *page*, which does change.
5. **Byte mode, error correction level M, versions 1–6, and nothing else.** Today's URLs are 52–55
   bytes (`https://profcarroll.github.io/sketchgen-gallery/e/222/` is 54) and version 4 at level M
   holds 62, so **both** codes — 52–55 bytes and 58–61 with `?kiosk` — land in version 4, a 33×33
   grid, the same size for every code in the gallery and the same size in both places it is shown.
   That 62 is the budget the rest of this spec spends: an entry id of six digits, or a longer
   `gallery_url`, would tip the kiosk code to version 5 and cost about a tenth of the distance it
   scans from, so §7 asserts the version rather than leaving it to be noticed on a wall. Level M
   holds 106 bytes at version 6, and stopping at 6 means the encoder never writes a
   version-information block (that starts at 7). A payload over 106 bytes raises (§2.5) rather
   than silently growing the table.
6. **M, not L, Q or H.** The failure mode for a code on a wall is not damage, it is angular size:
   a phone wants the code to be roughly a tenth of its distance across. Higher correction means
   more modules in the same tile, so smaller modules and a *worse* scan from the back of the room
   — Q puts this URL in version 5 (37×37, modules 9% smaller) and H in version 6 (41×41, 16%
   smaller) to buy redundancy against damage that a backlit white tile cannot suffer. L is the
   other way and is worse for a different reason: its version boundary falls at 53 bytes, inside
   our id range, so entries 1–99 would get a 29×29 code and entries 100 and up a 33×33 one, and
   codes that change size as the slideshow advances read as a bug. M tolerates 15%, is what every
   URL shortener prints, and is the only level that gives the whole gallery one code size.
7. **The code is on by default in the kiosk.** It is the reason this packet exists: a projection
   nobody can act on is a screensaver. It is the sixth overlay on and it is the only one that
   invites a stranger to do something.
8. **The kiosk's code says it came from a projection; the entry page's does not.** `?kiosk`, six
   bytes, which is what the budget in decision 5 affords — `?from=kiosk` is eleven and would cost
   every code in the gallery a version. It earns those six bytes twice over: the entry page can
   greet someone who scanned a wall with the three things they can do (§6.2), which a stranger
   landing on a long page otherwise has to go looking for, and when the write path later learns to
   record where a view came from (§9) the signal is already in the URL. The param is read and then
   stripped from the address bar with `replaceState`, as `claimTokenFromHash()` already strips the
   session fragment, so a scanner who texts the link to a friend does not pass a projection's
   provenance along with it. The **printed** URL under both codes is always the clean one: the
   param means *this view arrived by scanning a projection*, and someone who typed what they read
   did not.
9. **A kiosk QR is `<img src>`, not drawn.** `kiosk.js` never encodes anything: the file exists
   beside the sketch it frames, the browser caches it, and swapping `src` on entry change is the
   whole of the code path. Nothing about the QR is fetched with `fetch`.
10. **Decorative in the accessibility tree.** The URL is printed as text next to the code in both
    places, so the image carries `alt=""`. A screen reader that announced "QR code" and stopped
    would be worse than one that reads the link.

Still the instructor's to overrule before work starts: decision 6 (M rather than Q), decision 7
(on by default), and decision 8 (no `?from=kiosk`, and so no way to measure whether any of this
works).

## 2. The encoder: `sketchgen/qr.py`

A new module, standing alone, importing nothing but the stdlib. It knows one job: an ASCII string
in, a matrix of modules out. It does not know what a gallery is.

```python
class TooLong(ValueError):
    """The text does not fit in version 6 at level M."""

def encode(text: str) -> list[list[bool]]:
    """The modules, row-major, ``True`` for dark, no quiet zone."""

def svg(text: str, quiet: int = 4) -> str:
    """``encode()`` as an SVG document (§3)."""

def version_for(length: int) -> int:
    """The smallest version 1–6 whose level-M byte capacity holds ``length``."""
```

`encode` is deterministic: the same string gives the same matrix, every time, on every machine.
There is no randomness anywhere in it, and it reads no clock.

### 2.1 The data

1. The text is encoded UTF-8; every byte is data. (Our URLs are ASCII. UTF-8 is what a scanner
   assumes for byte mode in the absence of an ECI block, and we write no ECI block.)
2. Bit stream: mode indicator `0100`, then the byte count in **8 bits** (versions 1–9 are all the
   same in byte mode), then the bytes, most significant bit first.
3. Terminator: up to four `0` bits, stopping at the data capacity.
4. Pad to a byte boundary with `0`, then alternate the pad bytes `0xEC`, `0x11` to fill the
   version's data capacity exactly.

### 2.2 Error correction

Reed–Solomon over GF(256), primitive polynomial `0x11D`, generator element 2 — the standard field,
built once at import into `exp`/`log` tables.

For each block: the generator polynomial of degree *k* is the product of `(x − 2^i)` for
`i` in `0..k-1`; the remainder of the data polynomial shifted by *k* is the block's EC codewords.

The whole of the table this packet needs — level M, versions 1 to 6:

| version | size | total codewords | EC per block | blocks | data codewords | byte capacity | remainder bits |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 21×21 | 26 | 10 | 1 | 16 | 14 | 0 |
| 2 | 25×25 | 44 | 16 | 1 | 28 | 26 | 7 |
| 3 | 29×29 | 70 | 26 | 1 | 44 | 42 | 7 |
| 4 | 33×33 | 100 | 18 | 2 | 64 (32+32) | 62 | 7 |
| 5 | 37×37 | 134 | 24 | 2 | 86 (43+43) | 84 | 7 |
| 6 | 41×41 | 172 | 16 | 4 | 108 (27×4) | 106 | 7 |

Every block in versions 1–6 at level M is the same length as its fellows, so the "short block /
long block" split of the standard does not arise here. Say so in a comment, and have
`version_for` raise `TooLong` above 106 rather than let a caller reach a row that is not in the
table.

Interleaving, after both halves exist: data codeword *i* of block 0, of block 1, …, then *i+1*,
and so on; then the same over the EC codewords; then the version's remainder bits as `0`.

### 2.3 The matrix

Size is `17 + 4 * version`. In order:

1. Three finder patterns (7×7) at the top-left, top-right and bottom-left corners, each with its
   one-module light separator on the inner sides.
2. Timing patterns: row 6 and column 6, alternating dark from the finder edge inward.
3. Alignment pattern (5×5) at the single centre the version names — (18,18) for v2, (22,22) v3,
   (26,26) v4, (30,30) v5, (34,34) v6 — never placed where it would collide with a finder, which
   for one-centre versions means it is placed exactly once.
4. The dark module, always, at `(row = 4 * version + 9, column = 8)`.
5. Format-information areas reserved (both copies) before placement.
6. Data: the zigzag, two columns at a time from the bottom-right, **skipping column 6** entirely,
   upward then downward, writing into every module not already reserved.

### 2.4 Mask and format

All eight masks are applied to a copy and scored; the lowest penalty wins, ties to the lowest mask
number, so the choice is total and the output is reproducible. The four penalty rules, unchanged
from the standard: N1 = 3 + (run − 5) for every run of five or more same-colour modules in a row
or column; N2 = 3 for every 2×2 block of one colour; N3 = 40 for each `1:1:3:1:1` finder-like
pattern with four light modules on one side, in either direction; N4 = 10 for each 5% by which the
proportion of dark modules departs from 50%.

The format bits are the 2-bit level (M = `00`) and the 3-bit mask, extended by BCH(15,5) and
XORed with `0b101010000010010`, written into both copies.

### 2.5 Refusing

`TooLong` names the number and the fix: `"the URL is 118 bytes; version 6 at level M holds 106.
Extend the table in qr.py to version 7 (which needs the version-information block) or shorten
gallery_url."` It is a `ValueError`, it propagates out of `render_index`, and `_Written.undo()`
puts the checkout back — the same shape of refusal as `Unsafe`.

## 3. The SVG

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 41 41" width="41" height="41" shape-rendering="crispEdges" role="img" aria-hidden="true"><rect width="41" height="41" fill="#fff"/><path fill="#000" d="M4 4h7v1h-7z …"/></svg>
```

with a trailing newline. Fixed:

- The viewBox is `modules + 2 * quiet` on a side; quiet is 4 modules, as the standard requires,
  **baked into the file** so that no stylesheet can crop it off. One module is one user unit;
  `width`/`height` in units keep it sensible if anything ever loads it bare.
- **A white background rectangle, always.** The kiosk's ground is black and a transparent QR on
  black is an inverted QR; scanners vary in whether they will read one and a projector's contrast
  makes the gamble worse. Dark modules are `#000` on `#fff`, literal, not tokens — this file is
  read by a camera, not a theme.
- **One `<path>` of horizontal runs**, not one `<rect>` per module: 33×33 is 1,089 modules and a
  rect apiece is a 20 KB file the browser lays out 222 times. Runs bring a version-4 code to
  about 4 KB.
- No `<script>`, no `<style>`, no external reference, no `<!DOCTYPE>`, no comment holding the URL.
  The one `http://` in the file is `xmlns="http://www.w3.org/2000/svg"`, which is mandatory and is
  a namespace name rather than a thing anything fetches; there must be no second one, and the
  encoded URL must appear nowhere as text. The guard reads `.svg` as text; there is nothing
  personal in it, and the URL the code holds is the public one the entry page already prints.
- `aria-hidden="true"`, per §1.10.

## 4. The generator

In `gallery.py`:

- `from . import qr` at the top with the other module imports.
- In `render_index`, after the line pages and before the `except`, for every **public** entry —
  the `by_id` mapping it already has, which is `_public_rows`: published entries *and* kept
  rejections, because both have entry pages that will link the file:

  ```python
  for entry_id in sorted(by_id):
      url = config.entry_url(entry_id)
      written.write_text(dest / "e" / str(entry_id) / "qr.svg", qr.svg(url))
      written.write_text(dest / "e" / str(entry_id) / "qr-kiosk.svg", qr.svg(url + "?kiosk"))
  ```

  `_Written.write_text` makes the parent directory and undoes both on a refusal. Held entries and
  entries that were never public get neither file, as they get no page. `config.entry_url` ends in
  a slash, so the payload is `…/e/93/?kiosk` — a query on the directory, which GitHub Pages serves
  exactly as it serves the bare directory.
- `_kiosk_entry` gains two keys beside `sketch`, `source` and `href`:

  ```json
  "qr": "e/93/qr-kiosk.svg",
  "url": "https://profcarroll.github.io/sketchgen-gallery/e/93/"
  ```

  `qr` is the **kiosk** code, the one with the param: the manifest is read by one page and that
  page is the projection. `url` is `config.entry_url(id)` without the param — the same string
  `meta.json` already publishes as `source.entry` — because it is what the kiosk *prints* under
  the code (§5.3), not what the code holds. Both go through `guard()` with the rest of
  `kiosk.json`.
- Nothing else in `gallery.py` changes. `render_all` picks the codes up through `render_index`,
  which it already calls first.

Determinism is a property of this packet, not an aspiration: no clock, no randomness, sorted
iteration, so two renders of one database give two identical trees. The existing determinism test
covers `kiosk.json`; §7 extends it to the codes.

## 5. The kiosk

### 5.1 The overlay

`OVERLAYS` in `kiosk.js` gains one row, placed after `likes` so that the defaults stay contiguous:

```js
{ key: "qr", k: "Q", label: "QR code: scan to open this entry" }
```

`Q` is unused today. `DEFAULT_SHOW` gains `"qr"` (§1.7), the comment above it becomes *six on,
eight off*, and the comment above `OVERLAYS` becomes *the fourteen overlays*. `H` hides it with
everything else, because `on()` already gates on `state.hideAll`.

A projector that has been running since before this change has a stored settings blob whose
`show` map cannot mention a key that did not exist, so it would come back with the code off and
nobody at the keyboard. Store a version in the blob and migrate on read. The loader already walks
`saved.show` into `list`, the truthy keys, before calling `showFrom(list)`; the migration is one
line before that call — if `saved.v` is absent or below 2, `list.push("qr")` — and `save()` writes
`v: 2` beside `every`, `order` and `show`. One `if`, one comment saying why. A `?show=` in the URL
is untouched by this: an explicit list is an explicit list (`kiosk.md` §1.8).

`docs/plans/kiosk.md` gains one sentence at the end of §4.1: *Amended by `docs/plans/qr.md`: `Q`
toggles a QR code of the entry's URL, on by default.*

### 5.2 Where it sits

The caption is the right home: it already has the scrim, it is already `pointer-events: none`, and
it is already the thing that hides with `H`. Make it a two-part row rather than overlay a second
box onto the stage, so nothing can ever sit on top of anything:

- `#caption` becomes `display: flex; align-items: flex-end; gap: 2rem;`.
- `paint()` wraps the existing output: `<div class="words">` + `captionHtml(entry)` + `</div>`,
  then the QR block. `captionHtml` itself is unchanged — it still returns the words and only the
  words.
- The code sits at the lower left and the words to the right of it, but the markup keeps emitting
  the words first: the words are the content, and the code is an `aria-hidden` tile with three
  short lines beside it, which is not what a screen reader should meet first. The move is
  `order: -1` on the QR block and nothing in the script. With the code off, `qrHtml()` returns
  nothing and the words are flush left again with no rule for the empty case.
- `.caption .words { flex: 1 1 auto; min-width: 0 }`, `.caption .qr { flex: 0 0 auto; order: -1 }`.
- Inside `.qr`, the image sits on a white tile — `background: #fff; padding: 0.6rem;
  border-radius: 0.4rem` — and is `width: clamp(7rem, 13vh, 12rem); height: auto; display: block`.
  The lines of copy — three, or two with no write path (§5.3) — go under the tile, in the
  caption's small type, on the dark ground.
- Below 760 px, where the kiosk stacks, the caption becomes `flex-direction: column` so the code
  stays above the fold of the words rather than being pushed off a phone-shaped screen — plain
  `column`, because the `order: -1` above already puts the tile first and reversing as well would
  drop it back to the bottom. The two go together: change one and the other is wrong.

An honest note for the CSS comment and for anyone aiming a projector: a phone reads a code from
roughly ten times its width. At 13vh on a two-metre projection the tile is about 25 cm, so this
works from two or three metres and not from the back of a lecture hall. That is why the URL is
printed as text as well.

### 5.3 The strings

Three lines, fixed:

```
scan to open #93
profcarroll.github.io/sketchgen-gallery/e/93/
judge it · like it · ask for a revision
```

The middle line is `entry.url` minus a leading `https://`, for width — stripped for display only.
It is the clean URL, not the payload: the code beside it holds `?kiosk` and the line does not,
because the line is for someone typing it into a phone and typing is not scanning (§1.8). The
block is a `<div class="qr">` holding `<img class="code" src="<ROOT><entry.qr>" alt="">` and the
two lines; `entry.qr` is the kiosk code's path, from the manifest, never assembled in the script.

The third line is dropped when `config.json` has no write path, as the views and likes overlays
print `—` in that case (`kiosk.md` §4.2): all three of those verbs are the write path, and a
projection that invites a stranger to do something the gallery cannot accept is worse than a
projection that only shows the URL.

The QR swaps with the entry, like every other overlay: one `<img>` in the document at a time,
which the caption's `innerHTML` replacement already guarantees.

### 5.4 What it must not do

Encode anything. `fetch` the SVG. POST anything. Add a parameter to the URL. Count a scan.

## 6. The entry page

### 6.1 The code

`entry.html` gains, at the end of the Source panel and before the `</section>`:

```html
<figure class="qr">
  <img src="qr.svg" alt="" width="132" height="132">
  <figcaption>Scan to open this entry on a phone · <a href="$entry_url">$entry_url</a></figcaption>
</figure>
```

`$entry_url` is `config.entry_url(entry_id)`, added to the substitution dict `_write_entry`
builds (there is no `_entry_page`; the entry page's dict is assembled there).
The CSS is four lines in `gallery.css`: the figure is a flex row, the image sits on a white tile
(as in §5.2 — the entry page has a dark theme too), the caption is the panel's `.note` type.

This code is `qr.svg`, the clean one: someone scanning a laptop on a lectern did not scan a
projection, and the param would be a lie in the only place the difference is measurable. This is
the change that makes a `render-all` necessary (§8). It is worth it: the file is now a permanent,
citable artifact of the entry beside `meta.json`.

### 6.2 Arriving by scan

`gallery.js`, on an entry page only (`main.entry[data-entry]`, the element `sendView()` already
looks for), reads `?kiosk` and reveals one strip above the `.engagement` section:

```
You scanned this from a projection. Judge it against another · Like it · Ask for a revision.
```

The three are the page's own controls — a link to `$compare_href`, and two in-page anchors to the
like button and the critique form — not new behaviour, which is the point: the strip is a table of
contents for a page a stranger has thirty seconds of patience for.

The rules:

- `params.has("kiosk")`, not `.get()`: a bare key parses to `""`, which is falsy, and a version of
  this that silently never fires is worse than one that does not exist.
- Strip it immediately with `history.replaceState`, keeping the path and any other query —
  `claimTokenFromHash()` is the precedent, and the comment above it says why an address bar people
  copy from should not carry provenance.
- The strip is markup in `entry.html`, `hidden`, revealed by the script; nothing is built with
  `innerHTML` from a URL parameter.
- With no write path in `config.json` the generator writes no critique panel, so the third verb
  would link to nothing. Wrap it and its separator in one span and drop the span when its target
  is absent — the same rule §5.3 gives the kiosk's third line, for the same reason: a verb the
  gallery cannot honour is worse than one it never offered.
- The view POST is unchanged. `sendView()` sends `{ entry_id }` and nothing else, and this packet
  does not teach the write path a new field (§9).
- With no `?kiosk`, the entry page is byte-for-byte the page it is today plus §6.1's figure.

### 6.3 Coming back from sign-in

The strip offers a like, and a like wants a sign-in; but `<write_path>/callback` returns to the
gallery's front page and only there, so a stranger who scanned a wall, tapped *sign in with
GitHub* and came back through OAuth landed on the index and had to find the sketch again. Most
did not. So `gallery.js` does on an entry page what `swipe.js` already does on `swipe.html`
(`swipe.md` §5): before the sign-in anchors navigate, it writes `e/<id>/` — from
`main.entry[data-entry]`, never from the address bar — into `localStorage["sketchgen-return"]`,
with `?kiosk` on it when the strip is up so the greeting they arrived with is still there when
they land. `returnFromSignIn()` reads that note once, on the load that actually claimed a token,
and acts on it only if it matches one anchored pattern over a closed alphabet, now
`/^(?:swipe\.html|e\/[0-9]{1,6}\/)(?:\?[A-Za-z0-9=&_.-]*)?$/`. The key loses its `swipe-` because
it is no longer swipe's; one note written by the deploy before is orphaned and unread, which
costs at most one visitor one return.

Client-side because the alternative is a `return_to` column on `oauth_state`, a D1 migration and
a Worker deploy, to carry a string the browser already has — and swipe set this precedent for the
same reason. The honest limitation: the note lives in this origin's `localStorage`, so a mobile
in-app browser that hands the OAuth leg to a different browser profile still comes back to the
index. That is the failure this trade buys, and it is the one it had before.

## 7. Acceptance

`tests/test_qr.py` (new). The suite has no QR library to check against, so the tests check the
encoder against the *standard's* invariants and against a decoder written in the test file, which
is the same thing a scanner does:

- **Version selection.** A 14-byte string is version 1; 15 bytes is version 2; 52, 54 and 55 bytes
  are all version 4, and so are the same three with `?kiosk` appended — 58, 60 and 61 — which is
  the budget of §1.5 asserted as a number, so a longer `gallery_url` or a six-digit entry id fails
  here rather than on a wall; 106 bytes is version 6; 107 raises `TooLong` and the message names
  106.
- **Structure.** Three finder patterns and their separators; timing rows alternating; the
  alignment centre present for v2–6 and absent for v1; the dark module at `(4v+9, 8)`; the matrix
  square and of size `17+4v`.
- **Format information.** Both copies are identical, and decoding them (unmask, BCH check) gives
  level M and the mask the encoder says it used.
- **Error correction.** A decoder in the test file de-interleaves the codewords and computes the
  Reed–Solomon syndromes of every block; all are zero. This is an independent check: it fails if
  the generator polynomial, the block split or the interleave is wrong.
- **Round trip.** That same decoder — unmask, read the zigzag skipping column 6, de-interleave,
  strip mode, count, terminator and padding — recovers the exact input for strings of length 1,
  13, 14, 15, 54, 84 and 106, which crosses every version boundary in the table and both the
  two-block and four-block layouts.
- **Mask choice.** For a sample string, recompute the penalty of all eight masks in the test and
  assert the encoder picked the minimum, lowest index on a tie.
- **Determinism.** `svg(url)` twice gives identical bytes; and two committed golden fixtures,
  `tests/data/qr-entry-222.svg` and `tests/data/qr-entry-222-kiosk.svg`, match
  `svg(config.entry_url(222))` and `svg(config.entry_url(222) + "?kiosk")` byte for byte, so a
  change to the encoder can never be accidental. The two fixtures differ, which is also the test
  that the param reaches the payload.
- **The document.** viewBox `0 0 41 41` for a version-4 code; exactly one `<rect>` and one
  `<path>`; no `script` and no `style`; exactly one `http://` — the mandatory SVG namespace — and
  no `https://`, no `profcarroll`, no `kiosk` anywhere as text, since the URL lives in the modules
  and nowhere else; ends with `\n`.

`tests/test_gallery.py`:

- `render_index` writes **both** `e/<id>/qr.svg` and `e/<id>/qr-kiosk.svg` for every published
  entry **and** every kept rejection, and neither for a held entry; `render_all` too; both pass
  `guard()`.
- The two written files decode (reusing `test_qr.py`'s decoder) to `config.entry_url(id)` and to
  that URL with `?kiosk`, and to nothing else — in particular `qr.svg` has no param.
- A `config.json` with a different `gallery_url` produces different files, decoding to the URLs
  under that base.
- Two renders of the same database write identical bytes for both files.
- `kiosk.json` rows carry `qr` — the **`-kiosk`** path — and `url` without the param, with the
  values in §4, for a published root and a child alike.
- `entry.html` output holds `src="qr.svg"` once, never `qr-kiosk.svg`, and the clean entry URL as
  both href and text; and the §6.2 strip is present and `hidden`.

`tests/test_gallery_js.py`, node against `tests/js/dom.js` as the kiosk tests do (skipped where
node is absent):

- `Q` with the menu open toggles the code; the menu lists fourteen overlays and `m-count` says so.
- The default state shows the code; `?show=prompt` alone does not; the launch link for the default
  setup contains `qr` in `show=`.
- A stored blob with no `v` gains `qr` and comes back as `v: 2`; a stored blob at `v: 2` without
  `qr` stays without it.
- With the code on there is exactly one `.caption .qr img` in the document, its `src` is
  `ROOT + entry.qr` from the manifest, and advancing to the next entry leaves exactly one, with
  the next entry's src.
- `H` removes it with everything else.
- No `fetch` in `kiosk.js` mentions `qr` (assert on the text of the file, as the suite already
  reads scripts as text).

And for `gallery.js` on an entry page (§6.2):

- `?kiosk` reveals the strip; no param leaves it `hidden`; `?kiosk=` and `?kiosk=1` both count,
  which is what `params.has` gives and what a QR reader that normalises a bare key might send.
- After the strip is revealed the address bar no longer carries `kiosk`, and any other parameter
  on the URL survives the `replaceState`.
- The view POST's body is `{ entry_id }` with or without the param — this packet adds no field to
  the write path.

## 8. Running it

One branch from `main`, one PR, merged by the instructor. Then, on the node, **in this order**:

1. Pull the gallery checkout first if any PR was merged into `sketchgen-gallery` from outside the
   node, or the next publish is refused for a checkout behind its remote.
2. `update.sh` — pulls, restarts, runs `render-index`. That writes both codes for every public
   entry, the two assets and `kiosk.json`. Remember that a deploy which pulls a new `update.sh`
   runs the old copy; this packet does not change `update.sh`, so that only matters if something
   else in the same deploy does.
3. **`render-all` once**, because `entry.html` changed (§6) and `update.sh` never re-renders entry
   pages. About 222 pages, a few minutes, one commit. The codes themselves do not need it — step 2
   already wrote them — so if the entry-page figure has to wait for a quiet hour, the kiosk is
   already correct without it.
4. Expect roughly 1.9 MB of new files in the gallery repository — two codes of about 4 KB for
   each of about 222 entries — in one commit. It is text, it compresses, and it is written once
   per entry for the life of the entry.
5. Open `https://profcarroll.github.io/sketchgen-gallery/kiosk.html` on the projector, press Start,
   press `F`, and **scan the code with a phone** before walking away. No test in this repository
   can do that, and it is the only check that matters.

## 9. Out of scope, and worth a packet later

- **Knowing whether it works.** The signal is in the URL as of this packet; recording it is not.
  That needs a `source` on the write path's `/view`, a column in D1, a migration, `/pull` and
  `sync` carrying it, and a number in the operator console — a full-stack packet, where this one
  is generator-only. Until it lands, the evidence that a projection converts anyone is the shape
  of the views curve during an exhibition and nothing better.
- **A short URL.** Angular size is the binding constraint (§5.2) and a custom domain would cut the
  payload from 60 bytes to about 26, which is version 2 — a 25×25 grid whose modules are 24%
  larger in the same tile, and room for a readable param again. That is a DNS decision, not a code
  one.
- **A code on the grid cards, on `compare.html`, or on the line pages.** Nothing asked for it.
- **A code in the printed strip or `gate.png`.** Those are evidence images; leave them alone.
