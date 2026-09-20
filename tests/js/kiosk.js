/* kiosk.js, actually run (spec §5).
 *
 * The real sketchgen/assets/kiosk.js is loaded into the stub DOM (dom.js) on a
 * page built from the DOM contract kiosk.html is written to — ids only, no
 * structure the contract does not name — and then driven: the start card is
 * clicked, keys are pressed, and what the page says afterwards is read back
 * out of the caption, the status line and the menu.
 *
 * Two things are faked so the run is deterministic rather than slow. The
 * timers are a queue this file steps by hand, so the 420 ms fade between
 * sketches and the 8 s menu timeout cost nothing and never race; and fetch
 * answers the five requests the script is allowed to make — kiosk.json,
 * config.json, /counts, one sketch's source, and the one write it is allowed,
 * POST /view — from fixtures. Every one of them is kept in `asked`, with the
 * init it was called with, because what the script posted is as much of the
 * behaviour as what it drew.
 *
 * Prints one JSON object on the last line, which tests/test_gallery_js.py
 * asserts over; exits non-zero with a stack on a failure.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { makeWindow } = require("./dom.js");

const SCRIPT = path.join(__dirname, "..", "..", "sketchgen", "assets", "kiosk.js");

/* ---- the fixture ----------------------------------------------------------
 *
 * Three entries, chosen so that all six deterministic orders come out
 * differently — there are only six permutations of three things, and the seven
 * orders are three opposed pairs plus random, so this is as far apart as a
 * three-entry fixture can be pulled:
 *
 *   newest        33 22 11     published descending
 *   oldest        11 22 33
 *   liked         22 11 33     likes 9, 5, 2 from /counts
 *   reviewed      33 11 22     pairs 12, 8, 4 summed over judgment.*.*.n
 *   controversial 11 33 22     look-percentile gaps 0.8, 0.4, 0.1
 *   consensus     22 33 11
 */
const ENTRIES = [
  {
    id: 11,
    prompt: "A field of slow lines.\n\nRevise: let the lines thin as they near the edge.",
    brief: "Lines drawn across the canvas, thinning at the edges.",
    statement: "This sketch draws a field of lines.",
    submitted_by: "profcarroll",
    planner: "gemma4:e4b",
    executor: "qwen3-coder:30b-a3b-q4_K_M",
    rules_file: "control",
    attempts: 2,
    seed: 1,
    created_utc: "2026-09-01T10:00:00Z",
    published_utc: "2026-09-01T12:00:00Z",
    prompt_tokens: 1169,
    completion_tokens: 561,
    wall_s: 64.803,
    licence: "CC BY 4.0",
    generation: 2,
    parent_entry_id: 7,
    critique_by: "gemma4:e4b",
    root_entry_id: 7,
    sketch: "e/11/sketch/",
    source: "e/11/sketch/sketch.js",
    href: "e/11/",
    qr: "e/11/qr-kiosk.svg",
    url: "https://profcarroll.github.io/sketchgen-gallery/e/11/",
    canvas: [800, 600],
    judgment: {
      human: { look: { score: 2.1, n: 2, pct: 0.9 }, brief: { score: 1.8, n: 2, pct: 0.7 } },
      agent: { look: { score: 0.4, n: 2, pct: 0.1 }, brief: { score: 0.5, n: 2, pct: 0.2 } }
    }
  },
  {
    id: 22,
    prompt: "A window-sized drift of dots.",
    brief: "Dots drifting across the whole window.",
    statement: "This sketch drifts dots.",
    submitted_by: "profcarroll",
    planner: "gemma4:e4b",
    executor: "qwen3-coder:30b-a3b-q4_K_M",
    rules_file: "control",
    attempts: 1,
    seed: 2,
    created_utc: "2026-09-02T10:00:00Z",
    published_utc: "2026-09-02T12:00:00Z",
    prompt_tokens: 900,
    completion_tokens: 400,
    wall_s: 30.5,
    licence: "CC BY 4.0",
    generation: 1,
    parent_entry_id: null,
    critique_by: null,
    root_entry_id: 22,
    sketch: "e/22/sketch/",
    source: "e/22/sketch/sketch.js",
    href: "e/22/",
    qr: "e/22/qr-kiosk.svg",
    url: "https://profcarroll.github.io/sketchgen-gallery/e/22/",
    judgment: {
      human: { look: { score: 1.0, n: 1, pct: 0.5 }, brief: { score: 1.1, n: 1, pct: 0.55 } },
      agent: { look: { score: 0.9, n: 1, pct: 0.4 }, brief: { score: 0.8, n: 1, pct: 0.45 } }
    }
  },
  {
    id: 33,
    prompt: "A square of quiet colour.",
    brief: "One square, slowly changing colour.",
    statement: "This sketch holds a square.",
    submitted_by: "profcarroll",
    planner: "gemma4:e4b",
    executor: "qwen3-coder:30b-a3b-q4_K_M",
    rules_file: "control",
    attempts: 1,
    seed: 3,
    created_utc: "2026-09-03T10:00:00Z",
    published_utc: "2026-09-03T12:00:00Z",
    prompt_tokens: 700,
    completion_tokens: 300,
    wall_s: 12.25,
    licence: "CC BY 4.0",
    generation: 1,
    parent_entry_id: null,
    critique_by: null,
    root_entry_id: 33,
    sketch: "e/33/sketch/",
    source: "e/33/sketch/sketch.js",
    href: "e/33/",
    qr: "e/33/qr-kiosk.svg",
    url: "https://profcarroll.github.io/sketchgen-gallery/e/33/",
    canvas: [400, 400],
    judgment: {
      human: { look: { score: 1.5, n: 3, pct: 0.6 }, brief: { score: 1.4, n: 3, pct: 0.3 } },
      agent: { look: { score: 0.6, n: 3, pct: 0.2 }, brief: { score: 0.7, n: 3, pct: 0.25 } }
    }
  }
];

const COUNTS = {
  11: { views: 400, likes: 5 },
  22: { views: 100, likes: 9 },
  33: { views: 900, likes: 2 }
};

// The em dash is the point: the code heading says bytes, and this source is
// three bytes longer than it is characters long.
const SOURCE = "function setup() {\n  // a field — of slow lines\n  createCanvas(800, 600);\n}\n";

/* ---- the page, from the DOM contract -------------------------------------- */

function el(document, tag, attrs, kids) {
  const node = document.createElement(tag);
  Object.keys(attrs || {}).forEach(function (name) {
    if (name === "hidden") { node.hidden = attrs[name]; return; }
    if (name === "text") { node.textContent = attrs[name]; return; }
    node.setAttribute(name, attrs[name]);
  });
  (kids || []).forEach(function (kid) { node.appendChild(kid); });
  return node;
}

function kioskPage(document) {
  const body = document.body;
  body.className = "kiosk";

  body.appendChild(el(document, "header", { class: "bar" }, [
    el(document, "p", { class: "wordmark", text: "sketchgen" }),
    el(document, "p", { class: "nav", text: "gallery · compare · kiosk" }),
    el(document, "p", { class: "session", "data-session": "", hidden: true })
  ]));

  body.appendChild(el(document, "div", { class: "welcome", id: "welcome" }, [
    el(document, "div", { class: "card" }, [
      el(document, "h1", { text: "sketchgen · kiosk" }),
      el(document, "p", { text: "Sketches from the gallery play one after another, full screen." }),
      el(document, "p", { text: "Press any key at any time for the controls." }),
      el(document, "button", { class: "go", id: "go", type: "button", text: "Start" }),
      el(document, "p", { class: "note", text: "This first press is the one gesture the browser needs." })
    ])
  ]));

  body.appendChild(el(document, "div", { class: "progress", id: "progress" }));
  body.appendChild(el(document, "p", { class: "status", id: "status" }));

  body.appendChild(el(document, "div", { class: "room" }, [
    el(document, "aside", { class: "column code", id: "codecol", hidden: true }, [
      el(document, "section", {}, [
        el(document, "h2", {}, [
          el(document, "span", { text: "Source" }),
          el(document, "span", { class: "file", id: "codefile" })
        ]),
        el(document, "div", { class: "codewrap" }, [el(document, "pre", { id: "code" })])
      ])
    ]),
    el(document, "main", { class: "stage", id: "stage" }, [
      el(document, "div", { class: "caption", id: "caption" })
    ]),
    el(document, "aside", { class: "column words", id: "wordscol", hidden: true }, [
      el(document, "section", { id: "sec-brief", hidden: true }, [
        el(document, "h2", { text: "Brief" }),
        el(document, "p", { id: "brief" }),
        el(document, "p", { class: "note", text: "The planner's expansion of the prompt." })
      ]),
      el(document, "section", { id: "sec-statement", hidden: true }, [
        el(document, "h2", {}, [el(document, "span", { id: "stmt-by" })]),
        el(document, "p", { id: "statement" }),
        el(document, "p", { class: "note", text: "The executor's own account of what it built." })
      ]),
      el(document, "section", { id: "sec-judgment", class: "judgment", hidden: true }, [
        el(document, "h2", { text: "Judgment" }),
        el(document, "div", { class: "pop human" }, [
          el(document, "span", { class: "pop-label", text: "Humans" }),
          el(document, "span", { class: "score", id: "j-human" }),
          el(document, "span", { class: "quad", id: "q-human" })
        ]),
        el(document, "div", { class: "pop agent" }, [
          el(document, "span", { class: "pop-label", text: "Agents" }),
          el(document, "span", { class: "score", id: "j-agent" }),
          el(document, "span", { class: "quad", id: "q-agent" })
        ]),
        el(document, "p", { class: "note", text: "Bradley–Terry per population." })
      ])
    ])
  ]));

  const rows = el(document, "ul", { class: "rows" }, [
    el(document, "li", { id: "m-pause" }, [
      el(document, "span", { class: "keys" }, [el(document, "kbd", { text: "space" })]),
      el(document, "span", { text: "pause / play" }),
      el(document, "span", { class: "state", id: "m-pause-state" })
    ]),
    el(document, "li", {}, [
      el(document, "span", { class: "keys" }, [el(document, "kbd", { text: "]" })]),
      el(document, "span", { text: "longer / shorter, 15 s steps" }),
      el(document, "span", { class: "state", id: "m-every2" })
    ]),
    el(document, "li", {}, [
      el(document, "span", { class: "keys" }, [el(document, "kbd", { text: "H" })]),
      el(document, "span", { text: "hide every overlay" }),
      el(document, "span", { class: "state", id: "m-hide-state" })
    ]),
    el(document, "li", { id: "m-size" }, [
      el(document, "span", { class: "keys" }, [el(document, "kbd", { text: "Z" })]),
      el(document, "span", {}, [
        el(document, "span", { class: "note", id: "m-size-note" })
      ]),
      el(document, "span", { class: "state", id: "m-size-state" })
    ])
  ]);

  body.appendChild(el(document, "div", { class: "menu", id: "menu", role: "dialog", hidden: true }, [
    el(document, "section", {}, [
      el(document, "h2", {}, [
        el(document, "span", { text: "Playback" }),
        el(document, "span", { class: "val", id: "m-every" })
      ]),
      rows
    ]),
    el(document, "section", {}, [
      el(document, "h2", { text: "Order" }),
      el(document, "ul", { class: "rows", id: "m-orders" })
    ]),
    el(document, "section", {}, [
      el(document, "h2", {}, [
        el(document, "span", { text: "Overlays" }),
        el(document, "span", { class: "val", id: "m-count" })
      ]),
      el(document, "ul", { class: "rows", id: "m-overlays" })
    ]),
    el(document, "footer", {}, [
      el(document, "p", {}, [el(document, "code", { id: "launch" })]),
      el(document, "p", { id: "m-note", text: "Views and likes are live from the write path; a sketch counts as a view once it has been on screen for ten seconds." })
    ])
  ]));

  body.appendChild(el(document, "footer", { class: "bar" }, [
    el(document, "p", { class: "note", text: "AI Disclosure" })
  ]));
}

/* ---- the world the script runs in ------------------------------------------ */

function answer(payload, text) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: function () { return Promise.resolve(payload); },
    text: function () { return Promise.resolve(text); }
  });
}

function load(options) {
  const opts = options || {};
  const window = makeWindow();
  const document = window.document;
  window.location.pathname = "/kiosk.html";
  window.location.search = opts.search || "";
  kioskPage(document);
  window.SKETCHGEN_ROOT = "./";
  if (opts.stored) { window.localStorage.setItem("sketchgen-kiosk", opts.stored); }
  // Node lays nothing out, so a test that wants the frame fitted says how big
  // the stage is.
  if (opts.stage) {
    document.getElementById("stage").clientWidth = opts.stage[0];
    document.getElementById("stage").clientHeight = opts.stage[1];
  }

  // Every request the script made, in order, so a test can assert on what it
  // asked for as well as on what it did with the answer.
  const asked = [];
  window.fetch = function (url, init) {
    asked.push({ url: String(url), init: init || null });
    if (String(url).indexOf("kiosk.json") !== -1) {
      // A manifest that never arrives is the case the start card exists for.
      if (opts.manifest === "reject") { return Promise.reject(new Error("offline")); }
      if (opts.manifest === "empty") { return answer({ entries: [] }); }
      const entries = JSON.parse(JSON.stringify(ENTRIES));
      // One run asks what a row the database has less of looks like: nobody
      // has compared it, and nothing timed the executor that wrote it.
      if (opts.sparse) {
        entries.forEach(function (entry) {
          delete entry.judgment;
          entry.wall_s = null;
        });
      }
      return answer({ entries: entries });
    }
    if (String(url).indexOf("config.json") !== -1) {
      const config = { write_path: opts.writePath === null ? "" : "https://write.example.invalid/api" };
      // Absent is on, which is what a gallery that predates the kiosk sends.
      if (opts.kioskViews === false) { config.kiosk_views = false; }
      return answer(config);
    }
    if (String(url).indexOf("/counts") !== -1) { return answer({ counts: COUNTS }); }
    if (String(url).indexOf("/view") !== -1) { return answer({ ok: true, counted: true }); }
    if (/sketch\.js$/.test(String(url))) { return answer(null, SOURCE); }
    return Promise.reject(new Error("offline: " + url));
  };

  // A timer queue rather than node's: the fade, the menu's eight seconds and
  // the cursor's three are all stepped by hand below.
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
  // Refreshing the counts every ten minutes is registered and never fired: no
  // test runs the kiosk for ten minutes.
  window.setInterval = function () { return 0; };
  window.clearInterval = function () {};

  // One clock for both queues. window.step advances it and runs the animation
  // frames dom.js is holding — a frame registered by one of those callbacks
  // lands in the next batch, as a browser's would — and then whatever timeouts
  // that took us past. So a test that wants N frames calls this N times, and
  // the playback timer, which counts rAF deltas, can be driven a frame at a
  // time without any wall clock being involved.
  function tock(ms) {
    window.step(ms || 0);
    const due = pending
      .filter(function (one) { return one.at <= window.clock; })
      .sort(function (a, b) { return a.at - b.at; });
    pending = pending.filter(function (one) { return one.at > window.clock; });
    due.forEach(function (one) { one.fn(); });
  }

  vm.runInContext(fs.readFileSync(SCRIPT, "utf8"), vm.createContext({
    window: window,
    document: document,
    fetch: window.fetch,
    URLSearchParams: URLSearchParams,
    Promise: Promise,
    console: console,
    Date: Date,
    Math: Math,
    Number: Number,
    String: String,
    Object: Object,
    Array: Array,
    JSON: JSON,
    parseInt: parseInt,
    parseFloat: parseFloat,
    isNaN: isNaN,
    encodeURIComponent: encodeURIComponent,
    unescape: unescape
  }), { filename: "kiosk.js" });

  return { window, document, asked, tock };
}

const FADE = 420;

/* How many presses of Z walk the whole cycle and come back to where it
 * started, which is the real assertion: a fourth press is as good as none. */
const SIZES_ROUND = 4;

function settle() {
  return new Promise(function (resolve) { setTimeout(resolve, 0); });
}

async function quiet() {
  // Enough turns for kiosk.json, config.json and /counts to land and for the
  // paints they each trigger to run.
  for (let turn = 0; turn < 8; turn += 1) { await settle(); }
}

/* Several cases below need the menu open before the key they are actually
 * testing, because the first press only opens the controls. That opener has to
 * be a key the page does not bind — it used to be "z", which is now the size
 * cycle (kiosk-fullscreen.md §2), and an opener that quietly changed the frame
 * is exactly the bug this comment is here to stop coming back. "y" is bound to
 * nothing: onKey falls through to handled = false and the press does nothing
 * but open the menu. */
function press(document, key) {
  (document.listeners.keydown || []).forEach(function (fn) {
    fn({ key: key, metaKey: false, ctrlKey: false, altKey: false, preventDefault: function () {} });
  });
}

function showing(document) {
  const num = document.querySelector(".prompt .num");
  return num ? Number(num.textContent.replace("#", "")) : null;
}

function frames(document) { return document.querySelectorAll("iframe.sketch"); }

/* Every write the script made, in order, unpacked. A POST that is not a view
 * would land here too, which is the point: the run asserts on the whole list,
 * not on the views in it. */
function posted(asked) {
  return asked
    .filter(function (one) { return one.init && one.init.method; })
    .map(function (one) {
      return {
        url: one.url,
        method: one.init.method,
        body: JSON.parse(one.init.body),
        // The tests read this back to prove it stayed absent: a projector
        // that presented an identity would be filing every sketch it played
        // under whoever last signed in on that machine.
        keys: Object.keys(one.init).sort()
      };
    });
}

async function started(options) {
  const world = await load(options);
  await quiet();
  world.document.getElementById("go").click();
  await quiet();
  return world;
}

/* ---- the runs -------------------------------------------------------------- */

async function orderIs(order) {
  const { document, tock } = await started({ search: "?order=" + order });
  const seen = [showing(document)];
  for (let step = 0; step < 2; step += 1) {
    press(document, "ArrowRight");       // opens the menu
    press(document, "ArrowRight");       // and now it acts
    tock(FADE);
    seen.push(showing(document));
    press(document, "Escape");
  }
  return seen;
}

async function orders() {
  const out = {};
  const names = ["newest", "oldest", "random", "liked", "reviewed", "controversial", "consensus"];
  for (const name of names) { out[name] = await orderIs(name); }
  return out;
}

async function keys() {
  const { document, tock } = await started({});
  const menu = document.getElementById("menu");
  const before = { menu: menu.hidden, entry: showing(document) };

  press(document, "n");                  // the first press only opens the menu
  const first = {
    menuOpen: menu.hidden === false,
    entry: showing(document),
    pauseState: document.getElementById("m-pause-state").textContent
  };

  press(document, "ArrowRight");         // with it open, the same key acts
  tock(FADE);
  const advanced = showing(document);

  press(document, "Escape");
  const closed = menu.hidden === true;

  // The status line carries the hint only while the menu is shut.
  const status = document.getElementById("status").textContent;

  press(document, "x");                  // reopen, x is not bound to anything
  let longer = "";
  for (let step = 0; step < 50; step += 1) { press(document, "]"); }
  longer = document.getElementById("m-every2").textContent;
  let shorter = "";
  for (let step = 0; step < 60; step += 1) { press(document, "["); }
  shorter = document.getElementById("m-every2").textContent;

  // Eight seconds without a key and it goes away by itself.
  tock(8000);
  const timedOut = menu.hidden === true;

  return {
    menuShutBefore: before.menu === true,
    entryBefore: before.entry,
    firstPressOpens: first.menuOpen,
    firstPressChangesNothing: first.entry === before.entry && first.pauseState === "playing",
    advancedTo: advanced,
    escapeCloses: closed,
    statusAfterEscape: status,
    longer: longer,
    shorter: shorter,
    timedOut: timedOut
  };
}

async function frame() {
  const { document, tock, asked } = await started({});
  const first = frames(document);
  const attrs = first.length === 1 ? Object.keys(first[0].attributes).sort() : null;

  press(document, "c");                  // opens the menu
  press(document, "c");                  // and now toggles the code column
  await quiet();
  const withCode = {
    columnShown: document.getElementById("codecol").hidden === false,
    heading: document.getElementById("codefile").textContent,
    lines: document.querySelectorAll("#code .ln").length,
    frames: frames(document).length
  };

  press(document, "ArrowRight");
  tock(FADE);
  await quiet();

  return {
    framesAtStart: first.length,
    sandbox: first.length ? first[0].getAttribute("sandbox") : null,
    src: first.length ? first[0].getAttribute("src") : null,
    attributes: attrs,
    codeColumnShown: withCode.columnShown,
    codeHeading: withCode.heading,
    codeLines: withCode.lines,
    framesWithCode: withCode.frames,
    framesAfterAdvance: frames(document).length,
    srcAfterAdvance: frames(document).length ? frames(document)[0].getAttribute("src") : null,
    asked: asked.map(function (one) { return one.url; }),
    inits: asked.map(function (one) {
      return one.init ? Object.keys(one.init).sort() : [];
    })
  };
}

async function settings() {
  const { document, window } = await started({ search: "?order=liked&every=45&show=prompt,code" });
  const launchBeforeAnyKey = document.getElementById("launch").textContent;
  const overlays = document.querySelectorAll("#m-overlays li.on").length;
  // The first press only opens the menu; the two after it act and so persist.
  // Toggling one overlay off and on again leaves the setup exactly as found,
  // which is what makes the stored copy and the address bar comparable to the
  // launch link the page came up with.
  press(document, "y");
  press(document, "p");
  press(document, "p");
  return {
    launch: launchBeforeAnyKey,
    launchAfterKeys: document.getElementById("launch").textContent,
    every: document.getElementById("m-every2").textContent,
    order: document.querySelector("#m-orders li.current").textContent,
    overlaysOn: overlays,
    count: document.getElementById("m-count").textContent,
    stored: JSON.parse(window.localStorage.getItem("sketchgen-kiosk") || "null"),
    address: window.history.lastUrl
  };
}

/* A stored setup with no URL to override it, and then a URL that overrides
 * half of it: a parameter beats storage, storage beats the default (§1.8).
 * Stored at the current settings version, so this run is precedence alone and
 * the migration below is tested where it belongs. */
async function precedence() {
  const stored = JSON.stringify({ v: 2, every: 120, order: "oldest", show: { brief: true } });
  const first = await load({ search: "", stored: stored });
  await quiet();
  const fromStorage = first.document.getElementById("launch").textContent;
  const second = await load({ search: "?every=30", stored: stored });
  await quiet();
  return { fromStorage: fromStorage, urlWins: second.document.getElementById("launch").textContent };
}

async function words() {
  const { document } = await started({ search: "?show=prompt,brief,statement,judgment,generation,views,likes,date,tokens,seconds,licence,authors" });
  return {
    entry: showing(document),
    prompt: document.querySelector(".prompt").textContent,
    revisions: document.querySelector(".revisions") ? document.querySelector(".revisions").textContent : null,
    authors: document.querySelector(".authors").textContent,
    facts: Array.prototype.map.call(document.querySelectorAll(".facts li"), function (li) {
      return li.textContent;
    }),
    brief: document.getElementById("brief").textContent,
    human: document.getElementById("j-human").textContent,
    humanQuad: document.getElementById("q-human").textContent,
    agent: document.getElementById("j-agent").textContent,
    agentQuad: document.getElementById("q-agent").textContent
  };
}

/* The oldest entry is the revised one, so this run is the revisions line. */
async function revised() {
  const { document } = await started({ search: "?order=oldest&show=prompt,authors" });
  return {
    entry: showing(document),
    prompt: document.querySelector(".prompt").textContent,
    revisions: document.querySelector(".revisions").textContent,
    authors: document.querySelector(".authors").textContent
  };
}

async function unjudged() {
  const { document } = await started({ search: "?show=judgment,seconds", sparse: true });
  return {
    human: document.getElementById("j-human").textContent,
    humanQuad: document.getElementById("q-human").textContent,
    agent: document.getElementById("j-agent").textContent,
    facts: Array.prototype.map.call(document.querySelectorAll(".facts li"), function (li) {
      return li.textContent;
    })
  };
}

async function noWritePath() {
  const { document, window } = await started({ search: "?order=liked", writePath: null });
  const facts = Array.prototype.map.call(document.querySelectorAll(".facts li"), function (li) {
    return li.textContent;
  });
  return {
    entry: showing(document),
    facts: facts,
    // What it plays and what it says it is playing must be the same word.
    status: document.getElementById("status").textContent,
    launch: document.getElementById("launch").textContent,
    current: document.querySelector("#m-orders li.current").textContent,
    stored: JSON.parse(window.localStorage.getItem("sketchgen-kiosk") || "null")
  };
}

/* ---- the QR overlay (qr.md §5) --------------------------------------------- */

function codes(document) { return document.querySelectorAll(".caption .qr img"); }

function qrBlock(document) {
  const image = codes(document);
  return {
    images: image.length,
    src: image.length ? image[0].getAttribute("src") : null,
    alt: image.length ? image[0].getAttribute("alt") : null,
    lines: Array.prototype.map.call(
      document.querySelectorAll(".caption .qr p"),
      function (p) { return p.textContent; }
    ),
    // The markup order, which is not the order on screen: the code is at the
    // lower left by CSS (`order: -1`), and the words come first here because
    // they are the content and the tile is decorative.
    order: Array.prototype.filter.call(
      document.getElementById("caption").childNodes,
      function (part) { return part.tagName; }
    ).map(function (part) { return part.className; })
  };
}

/* On by default, one image at a time, and it swaps with the entry. */
async function qr() {
  const { document, tock } = await started({});
  const onByDefault = qrBlock(document);
  const launch = document.getElementById("launch").textContent;
  const menuCount = document.getElementById("m-count").textContent;
  const menuRows = document.querySelectorAll("#m-overlays li").length;

  press(document, "ArrowRight");            // opens the menu
  press(document, "ArrowRight");            // and now advances
  tock(FADE);
  const afterAdvance = qrBlock(document);
  press(document, "Escape");

  press(document, "y");                     // opens the menu
  press(document, "Q");                     // Q toggles it off
  const toggledOff = qrBlock(document);
  press(document, "q");                     // and the lowercase key back on
  const toggledOn = qrBlock(document);

  press(document, "H");                     // H hides every overlay
  const hidden = {
    images: codes(document).length,
    caption: document.getElementById("caption").innerHTML
  };

  return {
    onByDefault: onByDefault,
    launch: launch,
    menuCount: menuCount,
    menuRows: menuRows,
    afterAdvance: afterAdvance,
    toggledOff: toggledOff,
    toggledOn: toggledOn,
    hidden: hidden
  };
}

/* An explicit ?show= is an explicit list (§1.8): naming one overlay turns the
 * others off, the default included. And with no write path the third line —
 * three verbs that are all the write path — is not printed at all (§5.3). */
async function qrElsewhere() {
  const explicit = await started({ search: "?show=prompt" });
  const alone = qrBlock(explicit.document);
  const offline = await started({ search: "?show=qr", writePath: null });
  return { explicit: alone, offline: qrBlock(offline.document) };
}

/* A projector configured before the QR overlay existed has a stored blob that
 * cannot mention it. It comes back with the code on, and stamped (§5.1). */
async function migration() {
  const old = JSON.stringify({ every: 60, order: "newest", show: { prompt: true } });
  const before = await load({ stored: old });
  await quiet();
  const migrated = before.document.getElementById("launch").textContent;
  before.document.getElementById("go").click();
  await quiet();
  press(before.document, "z");
  press(before.document, "p");
  press(before.document, "p");
  const stamped = JSON.parse(before.window.localStorage.getItem("sketchgen-kiosk"));

  // And somebody who turned it off after the migration keeps it off: the
  // stamp is what stops the migration running twice.
  const current = JSON.stringify({ v: 2, every: 60, order: "newest", show: { prompt: true } });
  const after = await load({ stored: current });
  await quiet();
  return {
    migrated: migrated,
    stamped: stamped,
    stays: after.document.getElementById("launch").textContent
  };
}

/* ---- the playback timer, a frame at a time --------------------------------- */

function progress(document) {
  const bar = document.getElementById("progress");
  return { width: bar.style.width, className: bar.className };
}

/* every=60: two fifteen-second frames are a quarter and then a half. */
async function timer() {
  const { document, tock } = await started({ search: "?order=newest&every=60" });
  const start = { entry: showing(document), progress: progress(document) };
  tock(15000);
  const quarter = progress(document);
  tock(15000);
  const half = progress(document);

  // Thirty more seconds is the whole sixty: it advances on its own, with the
  // same fade a keypress gets, and still only one frame on the stage.
  tock(30000);
  const atTheEnd = { entry: showing(document), frames: frames(document).length };
  tock(FADE);
  const afterTheFade = {
    entry: showing(document),
    frames: frames(document).length,
    src: frames(document)[0].getAttribute("src"),
    progress: progress(document)
  };
  return { start, quarter, half, atTheEnd, afterTheFade };
}

async function paused() {
  const { document, tock } = await started({ search: "?order=newest&every=60" });
  const before = showing(document);
  tock(15000);                           // a quarter of the way in
  press(document, "y");                  // opens the menu
  press(document, " ");                  // and now pauses
  tock(0);                               // one frame, so the bar is painted paused
  const bar = progress(document);
  // Four minutes at sixty seconds each would be four sketches if it were running.
  for (let step = 0; step < 4; step += 1) { tock(60000); tock(FADE); }
  return {
    before: before,
    after: showing(document),
    frames: frames(document).length,
    pausedClass: progress(document).className,
    widthHeld: progress(document).width === bar.width,
    pauseState: document.getElementById("m-pause-state").textContent,
    status: document.getElementById("status").textContent
  };
}

/* A random sequence reshuffles when it wraps, and never wraps onto the sketch
 * that is already on the stage. Forty advances over three entries is a dozen
 * wraps, which is enough for a repeat to show up if one can. */
async function random() {
  const { document, tock } = await started({ search: "?order=random" });
  const seen = [showing(document)];
  for (let step = 0; step < 40; step += 1) {
    press(document, "y");
    press(document, "ArrowRight");
    tock(FADE);
    seen.push(showing(document));
    press(document, "Escape");
  }
  const repeats = seen.filter(function (id, at) { return at > 0 && id === seen[at - 1]; });
  // Every three advances is one lap; two different laps prove it reshuffles.
  const laps = {};
  for (let at = 0; at + 3 <= seen.length; at += 3) { laps[seen.slice(at, at + 3).join(",")] = true; }
  return { seen: seen, repeats: repeats.length, laps: Object.keys(laps).length };
}

/* The frame is laid out at the canvas's own size and scaled from there, so an
 * 800x600 sketch on a 1600x900 stage is an 800x600 frame at whatever --frame-sx
 * the size mode asks for; a sketch that sizes itself to the window leaves the
 * stage to say how big it is, in every mode. */
function frameOf(document) {
  const stage = document.getElementById("stage");
  return {
    entry: showing(document),
    w: stage.style.getPropertyValue("--frame-w"),
    h: stage.style.getPropertyValue("--frame-h"),
    sx: stage.style.getPropertyValue("--frame-sx"),
    sy: stage.style.getPropertyValue("--frame-sy")
  };
}

async function fitting() {
  const { document, tock } = await started({ search: "?order=oldest", stage: [1600, 900] });
  const fixed = frameOf(document);
  press(document, "y");
  press(document, "ArrowRight");
  tock(FADE);
  return {
    fixed: fixed,
    windowSized: Object.assign(frameOf(document), {
      style: document.getElementById("stage").getAttribute("style")
    })
  };
}

/* The Z key, walked all the way round, over both a sketch that asked for a
 * size and one that did not.
 *
 * 800x600 on 1600x900: as prompted is 1, because the sketch fits and asking
 * for 800x600 is asking for 800x600; fit is min(2, 1.5); fill is max(2, 1.5),
 * which runs 1600 of stage under a 1600-wide sketch and crops the rest; and
 * stretch is the two ratios, separately, which is the only mode where sx and
 * sy differ. Then it comes back round to as prompted. */
async function sizing() {
  const { document, tock } = await started({ search: "?order=oldest", stage: [1600, 900] });
  const walk = [frameOf(document)];
  const labels = [];
  const notes = [];
  press(document, "y");                  // opens the menu
  for (let at = 0; at < SIZES_ROUND; at += 1) {
    press(document, "z");
    walk.push(frameOf(document));
    labels.push(document.getElementById("m-size-state").textContent);
    notes.push(document.getElementById("m-size-note").textContent);
  }
  // …and a window-sized sketch, which none of the four can move.
  press(document, "ArrowRight");
  tock(FADE);
  const windowSized = [];
  for (let at = 0; at < SIZES_ROUND; at += 1) {
    press(document, "z");
    windowSized.push(frameOf(document));
  }
  return {
    walk: walk,
    labels: labels,
    notes: notes,
    windowSized: windowSized,
    windowSizedRow: document.getElementById("m-size").className,
    windowSizedNote: document.getElementById("m-size-note").textContent
  };
}

/* A canvas bigger than the stage is the one case "as prompted" cannot honour:
 * 800x600 on a 400x300 stage comes down by half rather than being cropped. */
async function sizingDown() {
  const { document } = await started({ search: "?order=oldest", stage: [400, 300] });
  return frameOf(document);
}

/* The size the projector was left in survives a reload, and a link that names
 * one beats it — the same precedence order and every other setting. */
async function sizingPersists() {
  const stored = JSON.stringify({ v: 2, every: 60, order: "oldest", size: "fill", show: { prompt: true } });
  const fromStorage = await started({ stored: stored, stage: [1600, 900] });
  const urlWins = await started({
    stored: stored, search: "?size=stretch", stage: [1600, 900]
  });
  const nonsense = await started({
    stored: stored, search: "?size=enormous", stage: [1600, 900]
  });
  return {
    fromStorage: frameOf(fromStorage.document),
    fromStorageLink: fromStorage.document.getElementById("launch").textContent,
    urlWins: frameOf(urlWins.document),
    // A size nobody defined is not a size: the stored one stands.
    nonsense: frameOf(nonsense.document)
  };
}

/* ---- the start card holds the click until there is something to play ------- */

function card(document) {
  const button = document.getElementById("go");
  return {
    welcomeUp: document.getElementById("welcome").hidden === false,
    disabled: button.disabled === true,
    label: button.textContent,
    note: document.querySelector(".welcome .note").textContent,
    frames: frames(document).length,
    playing: document.body.className.indexOf("playing") !== -1
  };
}

async function starting() {
  const waiting = await load({});
  const beforeAnything = card(waiting.document);
  await quiet();
  const loaded = card(waiting.document);

  const broken = await load({ manifest: "reject" });
  await quiet();
  const failed = card(broken.document);
  // The click must do nothing at all: no hidden card, no black screen.
  broken.document.getElementById("go").click();
  await quiet();
  const afterAClick = card(broken.document);
  // And the keyboard must still be inert rather than half-alive.
  press(broken.document, "ArrowRight");
  const menuAfterAKey = broken.document.getElementById("menu").hidden;

  const none = await load({ manifest: "empty" });
  await quiet();

  return {
    beforeAnything: beforeAnything,
    loaded: loaded,
    failed: failed,
    afterAClick: afterAClick,
    menuStillShut: menuAfterAKey === true,
    empty: card(none.document)
  };
}

/* The write (docs/plans/kiosk-views.md §3), which is the only one this script
 * is allowed to make and the one with nothing downstream to catch a mistake:
 * /view de-duplicates a signed-in viewer and the projector signs nobody in,
 * so a view posted per frame would be a view posted per frame. */
async function views() {
  /* Ten seconds of a sixty-second slot, and not a frame before. */
  const dwelled = await started({ search: "?order=newest&every=60" });
  const seat = showing(dwelled.document);
  dwelled.tock(9000);
  const atNine = posted(dwelled.asked).length;
  dwelled.tock(1000);
  const atTen = posted(dwelled.asked);

  /* Six hundred more frames on the same seat. One view, still: the flag is
   * set before the request goes out, so no frame can post a second. */
  for (let step = 0; step < 600; step += 1) { dwelled.tock(16); }
  const afterSixHundredFrames = posted(dwelled.asked).length;

  /* A sketch somebody skipped past was never on screen for ten seconds. */
  const skipped = await started({ search: "?order=newest&every=60" });
  const passedBy = showing(skipped.document);
  skipped.tock(4000);
  press(skipped.document, "ArrowRight");   // opens the menu
  press(skipped.document, "ArrowRight");   // and now it acts
  skipped.tock(FADE);
  press(skipped.document, "Escape");
  const afterASkip = { entry: passedBy, now: showing(skipped.document), posts: posted(skipped.asked).length };

  /* Ten seconds of playing, not ten seconds of wall clock. */
  const held = await started({ search: "?order=newest&every=60" });
  held.tock(9000);
  press(held.document, "z");               // opens the menu
  press(held.document, " ");               // and now pauses
  held.tock(600000);                       // ten minutes of a paused room
  const whilePaused = posted(held.asked).length;
  // The menu timed out somewhere in those ten minutes, so this is two presses
  // again: one to bring it back, one to play.
  press(held.document, "z");
  press(held.document, " ");
  held.tock(1000);                         // the tenth second, at last
  const afterResuming = posted(held.asked).length;

  /* A fifteen-second slot is shorter than the threshold and still counts. */
  const quick = await started({ search: "?order=newest&every=15" });
  quick.tock(15000);
  const shortSlot = posted(quick.asked).length;

  /* Eight hours of playing with nobody in the room. The sketches keep going;
   * the counting does not, until somebody turns up. */
  const empty = await started({ search: "?order=newest&every=600" });
  for (let step = 0; step < 60; step += 1) { empty.tock(600000); empty.tock(FADE); }
  const unattended = posted(empty.asked).length;
  const stillPlaying = frames(empty.document).length;
  press(empty.document, "z");              // somebody is here
  press(empty.document, "Escape");
  empty.tock(600000);
  empty.tock(FADE);
  const afterSomebodyArrives = posted(empty.asked).length;

  /* The two switches, and a gallery with nowhere to post. */
  const byUrl = await started({ search: "?order=newest&every=60&views=0" });
  byUrl.tock(60000);
  byUrl.tock(FADE);
  byUrl.tock(60000);
  press(byUrl.document, "z");              // an acting key rewrites the address bar
  press(byUrl.document, "]");
  const off = {
    posts: posted(byUrl.asked).length,
    link: byUrl.document.getElementById("launch").textContent,
    // persist() rewrote the address bar when ] acted. If views=0 is not in
    // what it wrote, the next acting key turns the counting back on.
    bar: byUrl.window.history.lastUrl
  };

  const byConfig = await started({ search: "?order=newest&every=60", kioskViews: false });
  byConfig.tock(60000);
  byConfig.tock(FADE);
  const configOff = posted(byConfig.asked).length;

  const nowhere = await started({ search: "?order=newest&every=60", writePath: null });
  nowhere.tock(60000);
  nowhere.tock(FADE);
  const noWritePath = posted(nowhere.asked).length;

  return {
    seat: seat,
    atNine: atNine,
    atTen: atTen,
    afterSixHundredFrames: afterSixHundredFrames,
    afterASkip: afterASkip,
    whilePaused: whilePaused,
    afterResuming: afterResuming,
    shortSlot: shortSlot,
    unattended: unattended,
    stillPlaying: stillPlaying,
    afterSomebodyArrives: afterSomebodyArrives,
    off: off,
    configOff: configOff,
    noWritePathPosts: noWritePath
  };
}

async function main() {
  const report = {
    orders: await orders(),
    keys: await keys(),
    frame: await frame(),
    settings: await settings(),
    precedence: await precedence(),
    words: await words(),
    revised: await revised(),
    unjudged: await unjudged(),
    noWritePath: await noWritePath(),
    timer: await timer(),
    paused: await paused(),
    random: await random(),
    fitting: await fitting(),
    sizing: await sizing(),
    sizingDown: await sizingDown(),
    sizingPersists: await sizingPersists(),
    qr: await qr(),
    qrElsewhere: await qrElsewhere(),
    migration: await migration(),
    starting: await starting(),
    views: await views(),
    source: { chars: SOURCE.length, bytes: Buffer.byteLength(SOURCE, "utf8") }
  };
  console.log(JSON.stringify(report));
  process.exit(0);
}

main().catch(function (err) {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
