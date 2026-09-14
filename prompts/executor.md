prompt_version: executor-v1
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
