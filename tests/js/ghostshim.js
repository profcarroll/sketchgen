/* The ghost pointer, actually run (docs/plans/auto-mouse.md §3.4).
 *
 * The real JavaScript out of sketchgen/ghostshim.py — not a transcription of
 * it — loaded into the stub DOM (dom.js) on a page with one canvas at a rect
 * this file chose, with a timer queue it steps by hand. Every MouseEvent the
 * shim dispatches is recorded where it lands, which is on the document: the
 * shim dispatches on the canvas and p5 listens on window, so the events have
 * to bubble or the sketch never hears them.
 *
 * tests/test_gallery_js.py writes the shim to a file, runs this with that path
 * and dom.js's, and asserts over the one JSON object printed on the last line.
 * It recomputes the expected coordinates from ghostshim.BUILTINS itself, so a
 * built-in that moves in Python and not in the rendered script fails here.
 *
 * Exits non-zero with a stack on a failure.
 */

"use strict";

const fs = require("fs");
const vm = require("vm");
const { makeWindow, MouseEvent } = require(process.argv[2]);

const SHIM = fs.readFileSync(process.argv[3], "utf8");

/* A canvas that is not at the origin and is not square: a shim that forgot
 * box.left, or swapped width and height, comes out wrong rather than right. */
const RECT = { left: 40, top: 20, width: 800, height: 600, right: 840, bottom: 620 };

const KINDS = ["mousemove", "mousedown", "mouseup", "click"];

function load(options) {
  const opts = options || {};
  const window = makeWindow();
  const document = window.document;
  window.location.pathname = "/e/11/sketch/";
  window.location.search = opts.search || "";
  if (opts.script) { window.__ghostScript = opts.script; }

  const canvas = document.createElement("canvas");
  canvas.rect = RECT;
  // A preload() sketch has no canvas when the shim runs, so one case seats it
  // late and the rest seat it before the script is loaded, as a sketch whose
  // setup() has already run would have.
  if (!opts.lateCanvas) { document.body.appendChild(canvas); }

  // Where the events land. Listening on the document rather than the canvas is
  // the assertion that they bubble.
  const seen = [];
  KINDS.forEach(function (kind) {
    document.addEventListener(kind, function (event) {
      seen.push({
        kind: kind, at: window.clock,
        x: event.clientX, y: event.clientY,
        button: event.button, buttons: event.buttons,
        trusted: event.isTrusted === true, bubbles: event.bubbles === true
      });
      if (opts.throwOn && opts.throwOn === kind) {
        throw new Error("the sketch's own handler threw");
      }
    });
  });

  // Every line the shim wrote, by level. console_clean in the gate is the
  // reason the level matters as much as the message.
  const said = { debug: [], log: [], warn: [], error: [] };
  const console = {
    debug: function (text) { said.debug.push(String(text)); },
    log: function (text) { said.log.push(String(text)); },
    warn: function (text) { said.warn.push(String(text)); },
    error: function (text) { said.error.push(String(text)); }
  };
  window.console = console;

  let pending = [];
  let ticket = 0;
  window.setTimeout = function (fn, ms) {
    ticket += 1;
    pending.push({ id: ticket, at: window.clock + (ms || 0), fn: fn });
    return ticket;
  };
  window.clearTimeout = function (id) {
    pending = pending.filter(function (one) { return one.id !== id; });
  };

  /* Advance the clock to `window.clock + ms`, running whatever fell due on the
   * way in the order it fell due — a timer set by one of those callbacks lands
   * in the same walk if its turn has already passed, which is how a script of
   * timeouts set all at once plays in order. */
  function tock(ms) {
    const until = window.clock + (ms || 0);
    let guard = 0;
    for (;;) {
      pending.sort(function (a, b) { return a.at - b.at; });
      if (!pending.length || pending[0].at > until) { break; }
      const next = pending.shift();
      window.clock = Math.max(window.clock, next.at);
      next.fn();
      guard += 1;
      if (guard > 20000) { throw new Error("the ghost's timers never settle"); }
    }
    window.clock = until;
  }

  function seatCanvas() { document.body.appendChild(canvas); }

  vm.runInContext(SHIM, vm.createContext({
    window: window,
    document: document,
    MouseEvent: MouseEvent,
    console: console,
    Math: Math,
    Number: Number,
    String: String,
    Object: Object,
    Array: Array,
    JSON: JSON,
    RegExp: RegExp,
    Error: Error,
    isFinite: isFinite,
    decodeURIComponent: decodeURIComponent
  }), { filename: "ghostshim.js" });

  return { window, document, seen, said, tock, seatCanvas };
}

/* One built-in, played to the end and no further: the loop is a run of its
 * own, below. */
function builtin(name) {
  const world = load({ search: "?ghost=" + name });
  world.tock(5000);
  return world.seen.map(function (one) {
    return [one.kind, one.x, one.y, one.buttons];
  });
}

/* The frame the kiosk actually seats for an entry that responds to both. */
function pair() {
  const world = load({ search: "?ghost=click,drag&ghost_loop=6000" });
  world.tock(8000);
  return {
    count: world.seen.length,
    first: world.seen[0].at,
    last: world.seen[world.seen.length - 1].at,
    kinds: world.seen.map(function (one) { return one.kind; })
  };
}

/* The whole guarantee that the entry page, swipe and the preview are
 * unchanged: no parameter, no DOM touched at all. */
function inert() {
  const bare = load({ search: "" });
  bare.tock(30000);
  const off = load({ search: "?ghost=0" });
  off.tock(30000);
  const unknown = load({ search: "?ghost=nonsense" });
  unknown.tock(30000);
  const withOne = load({ search: "?ghost=nonsense,click" });
  withOne.tock(5000);
  return {
    noParam: bare.seen.length,
    // The shim returns before it touches the DOM, so it has not even
    // registered the pointerdown it would yield to a hand on. That listener
    // is the only mark it leaves on a page it is inert in, so its absence is
    // how "before touching the DOM" is checked rather than asserted.
    noParamYield: bare.document.listeners.pointerdown === undefined,
    zero: off.seen.length,
    zeroYield: off.document.listeners.pointerdown === undefined,
    // And where it does run, it registers it.
    knownYield: withOne.document.listeners.pointerdown !== undefined,
    unknownName: unknown.seen.length,
    // An unknown name in a list is dropped and the rest is still played.
    unknownThenKnown: withOne.seen.length
  };
}

/* A hand always wins (DECIDE[ghost-yields]). */
function yields() {
  const world = load({ search: "?ghost=wander&ghost_loop=1000" });
  world.tock(1000);
  const before = world.seen.length;

  // isTrusted false is what the ghost's own events carry, so it must not be
  // what stops it: a run that stopped itself would stop after one event. The
  // recorder counts this one too, so the comparison below is against the
  // count taken straight after it and not against `before`.
  const fake = new MouseEvent("mousemove", { bubbles: true, clientX: 1, clientY: 1 });
  world.document.dispatchEvent(fake);
  const withTheFake = world.seen.length;
  world.tock(500);
  const afterSynthetic = world.seen.length;

  // pointerdown is not one of the four the recorder listens for, so this adds
  // nothing to the count and the numbers on either side of it are comparable.
  const hand = new MouseEvent("pointerdown", { bubbles: true, clientX: 1, clientY: 1 });
  hand.isTrusted = true;
  world.document.dispatchEvent(hand);
  const atTheHand = world.seen.length;
  // Long enough for the rest of this run and several loops after it.
  world.tock(60000);
  return {
    before: before,
    afterSynthetic: afterSynthetic,
    atTheHand: atTheHand,
    afterTheHand: world.seen.length,
    grewBeforeTheHand: afterSynthetic > withTheFake
  };
}

/* The loop (§3.1 step 4): the script again, ghost_loop after it ended. */
function loops() {
  const world = load({ search: "?ghost=click&ghost_loop=1000" });
  world.tock(3320);                      // the click script's last event
  const once = world.seen.length;
  world.tock(1000);                      // the gap, and not a millisecond more
  const inTheGap = world.seen.length;
  world.tock(3320);
  const twice = world.seen.length;
  world.tock(1000 + 3320);
  return { once: once, inTheGap: inTheGap, twice: twice, thrice: world.seen.length };
}

/* The default gap, when the kiosk passes none. */
function defaultLoop() {
  const world = load({ search: "?ghost=click" });
  world.tock(3320);
  const once = world.seen.length;
  world.tock(5999);
  const beforeSix = world.seen.length;
  world.tock(1 + 3320);
  return { once: once, beforeSix: beforeSix, after: world.seen.length };
}

/* Packet 14's inline script beats the built-in the parameter names, and goes
 * through the same caps on the way in. */
function own() {
  const world = load({
    search: "?ghost=click",
    script: [
      { t: 100, type: "move", x: 0, y: 0 },
      { t: 200, type: "click", x: 1, y: 1 },
      { t: 300, type: "flick", x: 0.5, y: 0.5 },     // not a type: dropped
      { t: 400, type: "move", x: 1.5, y: 0.5 },      // off the canvas: dropped
      { t: 9000, type: "move", x: 0.5, y: 0.5 }      // past MAX_MS: dropped
    ]
  });
  // Its last surviving event is at 200 ms; stop well before the default loop
  // would play the whole thing again.
  world.tock(1000);
  return world.seen.map(function (one) { return [one.kind, one.x, one.y]; });
}

/* A sketch whose own handler throws is the sketch's problem, not the ghost's:
 * the shim says so at debug and keeps playing. An error here would fail
 * console_clean in the gate for a sketch that passed it before. */
function throwing() {
  const world = load({ search: "?ghost=click", throwOn: "mousedown" });
  world.tock(5000);
  return {
    events: world.seen.length,
    debug: world.said.debug.length,
    error: world.said.error.length,
    warn: world.said.warn.length,
    log: world.said.log.length,
    // It carried on: the three clicks after the first press still landed.
    kinds: world.seen.map(function (one) { return one.kind; })
  };
}

/* A preload() sketch has no canvas for a while; the shim polls for one. */
function late() {
  const world = load({ search: "?ghost=click", lateCanvas: true });
  world.tock(1000);
  const whileWaiting = world.seen.length;
  world.seatCanvas();
  world.tock(100);                        // the next poll finds it
  world.tock(3320);
  const afterItArrives = world.seen.length;

  // And one that never arrives: ten seconds of polling, then a debug line.
  const never = load({ search: "?ghost=click", lateCanvas: true });
  never.tock(30000);
  return {
    whileWaiting: whileWaiting,
    afterItArrives: afterItArrives,
    neverEvents: never.seen.length,
    neverDebug: never.said.debug,
    neverError: never.said.error.length
  };
}

function main() {
  const report = {
    rect: RECT,
    click: builtin("click"),
    drag: builtin("drag"),
    wander: builtin("wander"),
    pair: pair(),
    inert: inert(),
    yields: yields(),
    loops: loops(),
    defaultLoop: defaultLoop(),
    own: own(),
    throwing: throwing(),
    late: late(),
    // Every event the shim dispatches is synthetic, and every one bubbles:
    // p5 attaches its handlers to window, not to the canvas.
    trusted: builtinTrusted(),
    bubbled: builtinBubbles()
  };
  console.log(JSON.stringify(report));
  process.exit(0);
}

function builtinTrusted() {
  const world = load({ search: "?ghost=click" });
  world.tock(5000);
  return world.seen.filter(function (one) { return one.trusted; }).length;
}

function builtinBubbles() {
  const world = load({ search: "?ghost=click" });
  world.tock(5000);
  return world.seen.every(function (one) { return one.bubbles; });
}

try {
  main();
} catch (err) {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
}
