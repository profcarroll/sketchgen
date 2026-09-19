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
 * answers the four requests the script is allowed to make — kiosk.json,
 * config.json, /counts and one sketch's source — from fixtures.
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

const SOURCE = "function setup() {\n  createCanvas(800, 600);\n}\n";

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
      el(document, "p", { id: "m-note", text: "Views and likes are live from the write path; a play here is never counted as a view." })
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

  // Every request the script made, in order, so a test can assert on what it
  // asked for as well as on what it did with the answer.
  const asked = [];
  window.fetch = function (url, init) {
    asked.push({ url: String(url), init: init || null });
    if (String(url).indexOf("kiosk.json") !== -1) {
      const entries = JSON.parse(JSON.stringify(ENTRIES));
      // One run asks what an entry nobody has compared looks like.
      if (opts.unjudged) {
        entries.forEach(function (entry) { delete entry.judgment; });
      }
      return answer({ entries: entries });
    }
    if (String(url).indexOf("config.json") !== -1) {
      return answer({ write_path: opts.writePath === null ? "" : "https://write.example.invalid/api" });
    }
    if (String(url).indexOf("/counts") !== -1) { return answer({ counts: COUNTS }); }
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

  function tock(ms) {
    window.clock += ms || 0;
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
    encodeURIComponent: encodeURIComponent
  }), { filename: "kiosk.js" });

  return { window, document, asked, tock };
}

const FADE = 420;

function settle() {
  return new Promise(function (resolve) { setTimeout(resolve, 0); });
}

async function quiet() {
  // Enough turns for kiosk.json, config.json and /counts to land and for the
  // paints they each trigger to run.
  for (let turn = 0; turn < 8; turn += 1) { await settle(); }
}

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
  press(document, "z");
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
 * half of it: a parameter beats storage, storage beats the default (§1.8). */
async function precedence() {
  const stored = JSON.stringify({ every: 120, order: "oldest", show: { brief: true } });
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
  const { document } = await started({ search: "?show=judgment", unjudged: true });
  return {
    human: document.getElementById("j-human").textContent,
    humanQuad: document.getElementById("q-human").textContent,
    agent: document.getElementById("j-agent").textContent
  };
}

async function noWritePath() {
  const { document } = await started({ search: "?order=liked", writePath: null });
  const facts = Array.prototype.map.call(document.querySelectorAll(".facts li"), function (li) {
    return li.textContent;
  });
  return { entry: showing(document), facts: facts };
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
    noWritePath: await noWritePath()
  };
  console.log(JSON.stringify(report));
  process.exit(0);
}

main().catch(function (err) {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
