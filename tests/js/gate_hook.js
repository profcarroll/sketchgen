/* The gate's p5 hook, actually run (job 1542, entry 1531, 2026-09-24).
 *
 * The real INIT_JS out of gate/sketch_gate.py — not a transcription of it —
 * run in a vm context with the few browser globals it touches while it
 * installs, then handed a stand-in for p5 cut to the shape of p5 1.11.3 where
 * the hook meets it: the constructor runs _initializeInstanceVariables on the
 * new instance, isLooping() reads this._loop, frameCount sits on the
 * prototype at 0, and p5.Graphics — a p5.Element, not a p5 — copies the
 * prototype's members onto itself and then runs the same
 * p5.prototype._initializeInstanceVariables on itself. That last call is the
 * one the hook used to take for the sketch.
 *
 * tests/test_gate_fixtures.py writes INIT_JS to a file, runs this with that
 * path, and asserts over the one JSON object printed on the last line. The
 * fixtures good-graphics, good-webgl-2d-buffer and good-2d-webgl-buffer are
 * the same three cases against the real p5 in real Chromium, where Playwright
 * is installed.
 *
 * Exits non-zero with a stack on a failure.
 */

"use strict";

const fs = require("fs");
const vm = require("vm");

const INIT = fs.readFileSync(process.argv[2], "utf8");

/* p5 1.11.3, as far as the hook can see it. */
const P5_SHAPE = `
  function P(sketch) {
    this._loop = true;
    this._initializeInstanceVariables();
    if (typeof sketch === 'function') sketch(this);
  }
  P.prototype._initializeInstanceVariables = function () { this._styles = []; };
  P.prototype.isLooping = function () { return this._loop; };
  P.prototype.frameCount = 0;
  function Element() {}
  function Graphics(w, h, renderer) {
    Element.call(this);
    for (const key in P.prototype) {
      if (!this[key]) {
        this[key] = typeof P.prototype[key] === 'function'
          ? P.prototype[key].bind(this) : P.prototype[key];
      }
    }
    P.prototype._initializeInstanceVariables.apply(this);
    this.width = w;
    this.height = h;
    this._renderer = renderer;
  }
  Graphics.prototype = Object.create(Element.prototype);
  const GL = { GL: {}, drawingContext: {} };
  const TWO_D = { drawingContext: {} };
  window.p5 = P;
`;

/* One page: INIT_JS, then p5, then whatever the case does. */
function page(body) {
  const listeners = [];
  const sandbox = {
    console: console,
    setTimeout: setTimeout,
    performance: {},
    document: {
      addEventListener: function (kind, fn) { listeners.push(fn); },
      querySelectorAll: function () { return []; },
      createElement: function () { return {}; }
    },
    addEventListener: function (kind, fn) { listeners.push(fn); },
    HTMLCanvasElement: function () {}
  };
  sandbox.HTMLCanvasElement.prototype.getContext = function () { return null; };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(INIT, sandbox);
  vm.runInContext(P5_SHAPE, sandbox);
  return vm.runInContext("(() => {" + body + "})()", sandbox);
}

const report = {
  // Job 1542's shape, and good-webgl-2d-buffer's: the sketch's own canvas is
  // WEBGL and 640x480, and a 160x160 2D buffer is made after it.
  webgl_sketch_2d_buffer: page(`
    const sketch = new P();
    sketch.width = 640; sketch.height = 480; sketch.frameCount = 86;
    sketch._renderer = GL;
    const buffer = new Graphics(160, 160, TWO_D);
    const s = window.__gate.state();
    return { inst_is_sketch: window.__gate.inst === sketch,
             buffer_is_looping: !!buffer.isLooping(),
             isLooping: s.isLooping, frameCount: s.frameCount,
             width: s.width, height: s.height, webgl: s.webgl };
  `),
  // good-2d-webgl-buffer: the reverse, a 2D sketch with a WEBGL buffer.
  two_d_sketch_webgl_buffer: page(`
    const sketch = new P();
    sketch.width = 640; sketch.height = 480; sketch._renderer = TWO_D;
    new Graphics(200, 200, GL);
    const s = window.__gate.state();
    return { inst_is_sketch: window.__gate.inst === sketch,
             width: s.width, height: s.height, webgl: s.webgl };
  `),
  // Instance mode: the setup accessor is how the seeds get in before the
  // sketch's own setup() runs, and it belongs on the sketch, not the buffers
  // setup() makes.
  instance_mode: page(`
    let buffer = null;
    const sketch = new P(function (p) {
      p.setup = function () { buffer = new Graphics(64, 64, TWO_D); };
    });
    sketch.setup();
    return { inst_is_sketch: window.__gate.inst === sketch,
             seeded: window.__gate.seeded, hook: window.__gate.hook || null,
             accessor_on_sketch:
               typeof (Object.getOwnPropertyDescriptor(sketch, 'setup') || {}).get === 'function',
             accessor_on_buffer: Object.getOwnPropertyDescriptor(buffer, 'setup') !== undefined };
  `)
};

console.log(JSON.stringify(report));
