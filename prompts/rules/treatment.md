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

## What not to do

- Do not write setup or installation instructions into source files. The project
  exists; you are editing it, not describing how to create it.
- Do not add a package.json, bundler, TypeScript, or a framework.
- Do not print the whole file back after you have already written it.

## Verifying

There is no test runner. A sketch is correct when it runs in a browser with no
console errors and does what was asked. Say plainly which parts you could not
verify by reading the code.
