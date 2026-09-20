# Kiosk: forcing a sketch to fill the screen

An amendment to `docs/plans/kiosk.md` §4.3. That section says a manifest row with a `canvas` gets
"a frame of that aspect ratio scaled to fit, and one without gets a frame that fills the stage".
The first half of that sentence does not describe what the page does, and could not: the thing it
scales is the frame, and the frame is not the sketch. This document says what was actually on the
projector, what replaces it, and the one key that drives it.

One packet, one branch `feat/kiosk-fullscreen`, one PR. Generator only: no database change, no
migration, nothing in `worker.py`, `lineage.py`, `sync.py` or `writepath/`.

## 0. What was on the projector

`fitFrame()` sized the frame to the canvas's aspect ratio scaled to fit the stage, and left the
frame's contents alone. But a p5 canvas is exactly as big as `createCanvas()` made it. It does not
grow with the frame it is in, and the kiosk cannot tell it to: the frame is `allow-scripts`
without `allow-same-origin`, which is opaque by design and has no channel to speak over — the
reason `_canvas_size()` reads the size out of the source at render time in the first place.

So a fitted frame fitted nothing. Measured in Chrome against the real gallery, entry 167
(`createCanvas(400, 400)`) on a 1440×900 stage:

| | frame | canvas | of the stage |
| --- | --- | --- | --- |
| before | 900×900, centred | 400×400, in the frame's **top-left corner** | 8.5 % |
| after, as prompted | 400×400, centred | 400×400 | 8.5 %, centred |
| after, fit | 400×400 scaled 2.25 | 900×900 | 62 % |
| after, fill | 400×400 scaled 3.6 | 1440×1440, cropped | 100 % |

The rest of that 900×900 frame was the frame's own black. On a 1920×1080 projector a 400×400
sketch was a postage stamp with an off-centre black surround, and the bigger the projector the
smaller it got. **72 of the 222 published entries — a third of the gallery — call `createCanvas()`
with two literals**, most often `800×600` (42) or `600×600` (19), because that is what the prompt
asked for. The other 150 size themselves to the window and were always full screen.

A canvas *larger* than the stage was cropped by the same mechanism, silently: entry 215
(`1024×1024`) lost everything past 900 px.

## 1. The fix: scale the frame, do not resize it

Lay the frame out at the canvas's own size and apply a CSS `transform` to it. The transform
magnifies what the sketch actually drew, so the composition is exactly preserved — a sketch that
centres on `(200, 200)` goes on centring on `(200, 200)` — and only the scale changes.

`--frame-w`/`--frame-h` now carry the canvas's own size rather than a fitted box, and
`--frame-sx`/`--frame-sy` carry the scale. Two numbers rather than one so that `stretch` can pull
the axes apart. A row with no `canvas` sets none of the four and gets the CSS default: a frame
that fills the stage at scale 1.

### 1.1 Why not re-run the sketch at the projector's size

The crisp alternative is to make the sketch page wrap `createCanvas` so p5 renders at the stage's
size, and scale p5's drawing matrix to match. It was rejected:

- it is a change to `e/<id>/sketch/index.html`, so it costs a `render-all` over every entry rather
  than a `render-index`;
- `WEBGL` sketches, sketches that call `resetMatrix()`, and anything reading `width`/`height` or
  the pixel array would each break differently, and there is no way to check 72 sketches;
- it can turn a small sketch that is *right* into a big sketch that is *wrong* — art composed for
  400×400 re-run at 1920×1080 is not the same work.

An upscaled 400×400 is soft. That is the honest cost of putting a 400-pixel artwork on a two-metre
wall, and it is never *wrong* the way the alternative can be.

## 2. The key: `Z` walks four sizes

`Z`, because `F` is the browser's own full screen and `S` already cycles the order. One key, four
states, in this cycle:

| state | scale | what it costs |
| --- | --- | --- |
| `native` — *as prompted* | `min(1, contain)` | nothing; the default |
| `fit` — *fit to screen* | `contain` | bars on the short axis |
| `fill` — *fill screen* | `cover` | the edges, cropped |
| `stretch` — *stretch to screen* | both ratios, separately | the shape, distorted |

A walk rather than a toggle, because "full screen" has two defensible meanings — all of the sketch
with bars, or none of the bars with the edges gone — and the person aiming the projector is the one
who gets to pick. The menu names the state it is in and what that state costs; the status line
names it only when it is not `native`.

`native` is the default: a gallery shows a work at the size it was made, and overruling every
artist in the room is a decision an operator makes, not a default. Its one exception is a canvas
bigger than the stage, which comes down to fit rather than being cropped — shrinking is not
overruling the sketch, it is the only way to show all of it.

A window-sized sketch is the stage in all four states. The menu row says so rather than leaving
somebody pressing `Z` at a screen that never moves.

## 3. Settings

`size` joins `order`, `every` and `show` in the `sketchgen-kiosk` blob and in the launch
link, with the same precedence: URL parameter, then storage, then the default. A size nobody
defined is not a size — the stored one stands. No `SETTINGS_VERSION` bump: a blob written before
this packet has no `size`, and no `size` is `native`, which is what those projectors are already
doing.

## 4. What this does not touch

Not the sandbox, not the one-frame rule, not the fetch list, not `/view`, not the fade, not the
overlays. `Z` is an acting key like any other: it persists, repaints the menu and restarts its
timer. The frame is refitted on the press rather than at the next sketch, so the key is visibly
the thing that did it, and on every `paint()` and `resize`, so turning the code column on
recomputes the scale against the narrower stage.

## 5. Tests

`tests/js/kiosk.js` drives the real `kiosk.js`; `tests/test_gallery_js.py` asserts over what it
prints. Added: the frame is the canvas and the scale does the fitting; `Z` walks all four and the
fourth press is as good as none; a window-sized sketch is untouched by all four; `native` shrinks
an oversized canvas; a size persists and a link beats it.

One existing hazard, fixed here: several cases needed the menu open before the key they were
actually testing, and used `z` as the harmless opener. `z` is no longer harmless. The opener is
now `y`, which is bound to nothing, and `press()` says why — an opener that quietly changed the
frame is exactly the bug that comment is there to stop coming back.

## 6. Deploy

`update.sh` on the node runs `render-index`, which rewrites `kiosk.html`, `assets/kiosk.js` and
`assets/gallery.css`. Pull the gallery checkout first if this is merged from outside the node.
Nothing under `e/` changes, so no `render-all`. Then open `kiosk.html` on the projector and press
`Z`.
