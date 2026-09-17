prompt_version: executor-v2
---
You write one p5.js sketch, in one shot. There is no conversation after this
message and no second chance to revise: what you emit now is what runs.

## The rules of the repository you are writing for

${rules}

## The brief

${brief}

## The gate your sketch must pass

A headless browser loads your sketch on its own, seeds p5 with ${seed}, freezes
the clock, watches the canvas for about two seconds and then pokes it. It fails
the sketch for any uncaught exception or console error, for a draw loop that has
stopped advancing unless the sketch called `noLoop()` itself, and for using the
p5 sound API without loading the p5.sound addon. It then checks these, and only
these:

${assertions}

The gate cannot see what the sketch depicts, only whether the canvas changed when
it was meant to. Make each change above real and visible across the canvas rather
than a one-pixel flicker in a corner. It is a program that has to run, unattended,
the first time.

## The frame budget

The gate's browser has no GPU and shares the machine with the model; a viewer
later opens the sketch in an ordinary browser tab. A frame that takes seconds
does not run slowly — the gate never finishes and the viewer's tab runs out of
memory. The budget is fixed and does not grow with the adjectives in the brief:
"thousands", "volumetric", "highly detailed", "overwhelming scale" describe what
a viewer should feel, not a number of objects to allocate. Per frame:

- at most ~2,000 shape calls in 2D and ~300 in WEBGL; a particle is a point,
  an `ellipse()`, or one `vertex()` in a single `beginShape(POINTS)`, never a
  `push()`/`translate()`/`sphere()`/`pop()` block each;
- no all-pairs loop over the same array (2,000 items is two million checks a
  frame): use a grid or spatial hash, compare squared distances, and cap the
  connections drawn per frame at a few hundred;
- in WEBGL, batch lines and points into one `beginShape(LINES|POINTS)` per
  frame; build a static complex shape once in `setup()`;
- no `filter()`, `loadPixels()`, `get()`, `createGraphics()` or canvas-sized
  `image()` inside a loop; allocate in `setup()`, not in `draw()`;
- any array that grows every frame must have a hard cap and shrink every frame.

## What to emit, exactly

1. A fenced code block tagged `js` holding the COMPLETE contents of `sketch.js` —
   not a diff, not an excerpt, no line numbers, no elided bodies.

2. ONLY if the sketch needs an addon library that plain p5 does not provide
   (`p5.sound` is the usual one), a fenced code block tagged `html` holding the
   COMPLETE contents of `index.html`, with the addon's `<script>` tag added. If no
   addon is needed, omit this block entirely: a correct `index.html` loading p5
   1.11.3 and your `sketch.js` already exists and will be used.

3. A heading, exactly `## Statement`, then one paragraph, in your own words, about
   what you made: the idea, the technique, what a viewer is looking at. It is
   published beside the work under your model name, unedited, as your statement.
   Do not narrate the code line by line, and do not claim to have tested it.

Nothing else. No preamble, no explanation of the blocks, no closing offer of
further help. The blocks in that order.
