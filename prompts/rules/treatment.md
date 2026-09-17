# Working in this repo

Single-page p5.js sketches. No build step, no npm, no bundler, no framework.
You open `index.html` in a browser and that is the whole toolchain.

## Files

- `index.html` — already exists and is correct. Edit it **only** to add a library
  `<script>` tag. Do not restructure it, re-style it, or regenerate it.
- `sketch.js` — your work goes here. Usually the only file you change.
- Do not create additional files unless asked.

## The library trap — check this before you write a line

`index.html` loads **p5.js only**. The p5 *sound* API (`p5.AudioIn`, `p5.FFT`,
`p5.Oscillator`, `loadSound`) lives in a **separate library that is not loaded**.
If your sketch needs it, add the tag to `index.html` in the same edit as the code
that uses it:

```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/addons/p5.sound.min.js"></script>
```

Same rule for any other addon. Calling a function no loaded library provides is
the most common way a sketch here fails, and it fails silently in a console you
cannot see.

## Conventions

- Global mode: top-level `setup()` and `draw()`. Not instance mode.
- `createCanvas(windowWidth, windowHeight)`, plus a `windowResized()` that calls
  `resizeCanvas(windowWidth, windowHeight)`.
- Microphone or file loading must start from a user gesture — browsers block
  audio until a click. Put it behind `mousePressed()`.
- Keep `sketch.js` under ~150 lines unless asked. Comment the *why* of a
  technique, not the syntax.

## The frame budget — a brief is a picture, not a particle count

A sketch here runs unattended, in a headless browser with no GPU, on the same
machine as the model, and later in a viewer's browser tab. Most sketches draw a
frame in a few milliseconds. One that takes seconds per frame does not "run
slowly": the gate never finishes, the machine runs out of memory, and the
viewer's tab crashes. The budget is fixed and does not grow with the adjectives
in the brief. "Thousands of glowing particles", "volumetric", "highly detailed",
"overwhelming scale" describe what the viewer should *feel*; they are not
instructions to allocate thousands of objects. Fill the canvas with the effect,
not the count.

Per frame, stay inside all of these:

- **At most ~2,000 shape calls in 2D, ~300 in WEBGL.** `sphere()`, `box()`,
  `torus()` and friends are each a full mesh with lighting; a particle is a
  point, an `ellipse()`, or one `vertex()` inside a single
  `beginShape(POINTS)` … `endShape()`. Hundreds of `push()`/`translate()`/
  `sphere()`/`pop()` blocks per frame is the single most common way a 3D
  sketch here dies.
- **No all-pairs loops.** A nested `for i … for j` over the same array is
  N²: 2,000 particles is two million distance checks per frame, 5,000 is
  twelve million. Connect neighbours through a grid or spatial hash, compare
  squared distances, and cap the total connections drawn per frame
  (a few hundred is plenty). If you cannot bound it, do not draw it.
- **Batch lines and points.** In WEBGL every `line()` and `point()` call
  is its own draw with its own buffers. Put them in one
  `beginShape(LINES)` / `beginShape(POINTS)` … `endShape()` per frame. For a
  static complex shape, build it once in `setup()` (`buildGeometry()`) and draw
  the result.
- **No full-canvas passes inside loops.** `filter()`, `loadPixels()`,
  `get()`, `image()` of a canvas-sized buffer, `createGraphics()` — at most
  once per frame, never per object, and `createGraphics()`/`loadImage()`
  belong in `setup()`, not `draw()`.
- **Allocate in `setup()`, not `draw()`.** Do not create arrays, vectors,
  colours or graphics buffers per particle per frame; reuse them.
- **Keep the count fixed.** Any array that grows every frame must also
  shrink every frame (a hard cap and `splice`/`shift`), or the sketch leaks
  until the tab dies.
- **Detail is a cost.** Default `sphere()` detail (24×16) is fine for a
  few; for many, lower it (`sphere(r, 6, 4)`) or use points.

A safe reading of "thousands of particles" in WEBGL: ~1,000–2,000 positions in
one point cloud, a spatial hash for neighbours, a few hundred connecting
segments in one `LINES` shape, `frameRate(30)`. That fills the screen and the
viewer sees a field; the machine sees one draw call.

## What not to do

- Do not write setup or installation instructions into source files. The project
  exists; you are editing it, not describing how to create it.
- Do not add a package.json, bundler, TypeScript, or a framework.
- Do not print the whole file back after you have already written it.

## Verifying

There is no test runner. A sketch is correct when it runs in a browser with no
console errors and does what was asked. Say plainly which parts you could not
verify by reading the code.
