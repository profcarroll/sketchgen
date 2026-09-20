/* swipe.js, actually run (spec §6).
 *
 * The real sketchgen/assets/swipe.js is loaded into the stub DOM (dom.js) on a
 * page built from the DOM contract swipe.html is written to — the ids of §3 and
 * the markup the mockup hangs off them, and nothing else — and then driven with
 * the only input that page takes: pointer events. Drags are dispatched at the
 * shield with clientX, clientY and a pointerId, the six courtesy keys are
 * pressed at the document, and what the page says afterwards is read back out
 * of the caption, the status line and the four sheets.
 *
 * Two things are faked so the run is deterministic rather than slow. The timers
 * are a queue this file steps by hand, so the 160 ms the stage takes to leave
 * and the 450 ms a hold waits cost nothing and never race; and fetch answers
 * the seven requests the script is allowed to make — swipe.json, config.json,
 * /counts, /me, one entry's meta.json, pairs.json, and the three writes: POST
 * /view, /like and /vote — from fixtures. Every one of them is kept in `asked`
 * with the init it was called with, and in `EVERY` across every run in the
 * file, because the acceptance test pins the whole list of writes the page ever
 * makes and not just the ones a single run happened to look at.
 *
 * Prints one JSON object on the last line, which tests/test_gallery_js.py
 * asserts over; exits non-zero with a stack on a failure.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { makeWindow } = require("./dom.js");

const SCRIPT = path.join(__dirname, "..", "..", "sketchgen", "assets", "swipe.js");

/* ---- the fixture ----------------------------------------------------------
 *
 * Three entries with the kiosk fixture's numbers, so that all six
 * deterministic orders come out differently — there are only six permutations
 * of three things, and the seven orders are three opposed pairs plus random:
 *
 *   newest        33 22 11     published descending
 *   oldest        11 22 33
 *   liked         22 11 33     likes 9, 5, 2 from /counts
 *   reviewed      33 11 22     pairs 12, 8, 4 summed over judgment.*.*.n
 *   controversial 11 33 22     look-percentile gaps 0.8, 0.4, 0.1
 *   consensus     22 33 11
 *
 * Their prompts, authors and executors are all different, which the judge run
 * needs: B's prompt, executor and submitted_by must appear nowhere in the
 * document while a pair is being judged, and three entries that shared a
 * planner would let that pass by accident.
 *
 * The rows are swipe.json's (§2), not kiosk.json's: no brief, no statement, no
 * created_utc, no tokens, no licence — those live in meta.json and are fetched
 * only when a sheet asks for them.
 */
const ENTRIES = [
  {
    id: 11,
    prompt: "A field of slow lines.\n\nRevise: let the lines thin as they near the edge.",
    submitted_by: "hanne",
    planner: "gemma4:e4b",
    executor: "qwen3-coder:30b-a3b-q4_K_M",
    rules_file: "control",
    attempts: 2,
    published_utc: "2026-09-01T12:00:00Z",
    generation: 2,
    parent_entry_id: 7,
    critique_by: "gemma4:e4b",
    responds: ["click"],
    canvas: [800, 600],
    sketch: "e/11/sketch/",
    meta: "e/11/meta.json",
    href: "e/11/",
    url: "https://profcarroll.github.io/sketchgen-gallery/e/11/",
    judgment: {
      human: { look: { score: 2.1, n: 2, pct: 0.9 }, brief: { score: 1.8, n: 2, pct: 0.7 } },
      agent: { look: { score: 0.4, n: 2, pct: 0.1 }, brief: { score: 0.5, n: 2, pct: 0.2 } }
    }
  },
  {
    // No canvas: a sketch that sized itself to its window is already the
    // screen. And a listening one, which the caption has a sentence for.
    id: 22,
    prompt: "A window-sized drift of dots.",
    submitted_by: "cosima",
    planner: "llama4:16x17b",
    executor: "starcoder2:15b",
    rules_file: "treatment",
    attempts: 1,
    published_utc: "2026-09-02T12:00:00Z",
    generation: 1,
    parent_entry_id: null,
    critique_by: null,
    mic: true,
    sketch: "e/22/sketch/",
    meta: "e/22/meta.json",
    href: "e/22/",
    url: "https://profcarroll.github.io/sketchgen-gallery/e/22/",
    judgment: {
      human: { look: { score: 1.0, n: 1, pct: 0.5 }, brief: { score: 1.1, n: 1, pct: 0.55 } },
      agent: { look: { score: 0.9, n: 1, pct: 0.4 }, brief: { score: 0.8, n: 1, pct: 0.45 } }
    }
  },
  {
    id: 33,
    prompt: "A square of quiet colour.",
    submitted_by: "maret",
    planner: "gemma4:e4b",
    executor: "deepseek-coder:6.7b",
    rules_file: "control",
    attempts: 1,
    published_utc: "2026-09-03T12:00:00Z",
    generation: 1,
    parent_entry_id: null,
    critique_by: null,
    responds: ["click", "audio"],
    canvas: [400, 400],
    sketch: "e/33/sketch/",
    meta: "e/33/meta.json",
    href: "e/33/",
    url: "https://profcarroll.github.io/sketchgen-gallery/e/33/",
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

/* One meta.json per entry: what swipe.json leaves out. The briefs carry none
 * of the three strings the judge run says must not leak, because a brief is
 * shown on the sheet and the leak check reads the whole document. */
const METAS = {
  11: {
    brief: "Lines drawn across the canvas, thinning at the edges.",
    statement: "This sketch draws a field of lines.",
    submitted_by: "hanne",
    planner: "gemma4:e4b",
    executor: "qwen3-coder:30b-a3b-q4_K_M",
    rules_file: "control",
    attempts: 2,
    prompt_tokens: 1169,
    completion_tokens: 561,
    wall_s: 64.803,
    created_utc: "2026-09-01T10:00:00Z",
    licence: "CC BY 4.0",
    lineage: { parent_entry_id: 7, generation: 2, critique_by: "gemma4:e4b" }
  },
  22: {
    brief: "Dots drifting across the whole window.",
    statement: "This sketch drifts dots.",
    submitted_by: "cosima",
    planner: "llama4:16x17b",
    executor: "starcoder2:15b",
    rules_file: "treatment",
    attempts: 1,
    prompt_tokens: 900,
    completion_tokens: 400,
    wall_s: 30.5,
    created_utc: "2026-09-02T10:00:00Z",
    licence: "CC BY 4.0",
    lineage: { parent_entry_id: null, generation: 1, critique_by: null }
  },
  33: {
    brief: "One square, slowly changing colour.",
    statement: "This sketch holds a square.",
    submitted_by: "maret",
    planner: "gemma4:e4b",
    executor: "deepseek-coder:6.7b",
    rules_file: "control",
    attempts: 1,
    prompt_tokens: 700,
    completion_tokens: 300,
    wall_s: 12.25,
    created_utc: "2026-09-03T10:00:00Z",
    licence: "CC BY 4.0",
    lineage: { parent_entry_id: null, generation: 1, critique_by: null }
  }
};

/* pairs.json. Both offered pairs contain 33, which is the newest and so the
 * seat every judge run starts from, so B is 11 or 22 and "judge another pair"
 * has somewhere to go.
 *
 * The verdicts are stored against the pair in (low, high) order, as
 * pairs.agent_verdicts writes them, and the sheet is showing both of them the
 * other way round — A is 33, the higher id. Both are stored brief: A, look: B
 * and must therefore both render brief: B, look: A, which is the flip, and it
 * is the same pair of lines whichever partner came up. */
const PAIRS = {
  pairs: [{ a: 11, b: 33 }, { a: 22, b: 33 }],
  agents: {
    "11-33": [
      { judge: "gemma4:e4b", question: "brief", choice: "A", prompt_version: "v3" },
      { judge: "gemma4:e4b", question: "look", choice: "B", prompt_version: "v3" }
    ],
    "22-33": [
      { judge: "gemma4:e4b", question: "brief", choice: "A", prompt_version: "v3" },
      { judge: "gemma4:e4b", question: "look", choice: "B", prompt_version: "v3" }
    ]
  }
};

/* The three strings the judge sheet must not leak about B before both
 * questions are answered (§4.8). */
function secretsOf(id) {
  const entry = ENTRIES.filter(function (one) { return one.id === id; })[0];
  return [entry.prompt.split("\n")[0], entry.executor, entry.submitted_by];
}

/* ---- the page, from the DOM contract (§3) ---------------------------------- */

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

function sheetHead(document, kids) {
  return [el(document, "div", { class: "grip" }),
    el(document, "button", { class: "close", type: "button", "data-close": "", text: "✕" })]
    .concat(kids);
}

function question(document, key, ask) {
  return el(document, "div", { class: "question", "data-question": key }, [
    el(document, "p", { class: "ask", text: ask }),
    el(document, "p", { class: "choices" }, [
      el(document, "button", { type: "button", "data-vote": "A", text: "A" }),
      el(document, "button", { type: "button", "data-vote": "tie", text: "tie" }),
      el(document, "button", { type: "button", "data-vote": "B", text: "B" })
    ]),
    el(document, "p", { class: "answered" })
  ]);
}

function tile(document, side, label) {
  return el(document, "button", {
    class: "tile", type: "button", "data-side": side,
    "aria-pressed": side === "A" ? "true" : "false"
  }, [
    el(document, "p", { class: "side-label" }, [
      el(document, "span", { text: side + " " }),
      el(document, "small", { text: label })
    ]),
    el(document, "p", { class: "side-brief", id: "j-brief-" + side.toLowerCase() })
  ]);
}

function swipePage(document) {
  const body = document.body;
  body.className = "swipe";

  // The header bar is in the DOM for the reasons the kiosk's is, and goes the
  // moment the start card is dismissed.
  body.appendChild(el(document, "header", { class: "bar" }, [
    el(document, "p", { class: "wordmark", text: "sketchgen" }),
    el(document, "p", { class: "nav", text: "gallery · compare · kiosk · swipe" })
  ]));

  body.appendChild(el(document, "div", { class: "welcome", id: "welcome" }, [
    el(document, "div", { class: "card" }, [
      el(document, "h1", { text: "sketchgen · swipe" }),
      el(document, "p", { text: "Sketches from the gallery, one at a time, full screen." }),
      el(document, "ul", { class: "how" }, [
        el(document, "li", { text: "swipe up · the next sketch" }),
        el(document, "li", { text: "swipe right · like it" }),
        el(document, "li", { text: "swipe left · judge it against another" }),
        el(document, "li", { text: "tap · the words, on and off" }),
        el(document, "li", { text: "hold · touch the sketch" })
      ]),
      el(document, "button", { class: "go", id: "go", type: "button", text: "Start" }),
      el(document, "p", {
        class: "note",
        text: "This first tap is the one gesture the browser needs before a sketch can make sound."
      })
    ])
  ]));

  body.appendChild(el(document, "div", { class: "stage", id: "stage" }));
  body.appendChild(el(document, "div", { class: "shield", id: "shield", "aria-hidden": "true" }));
  body.appendChild(el(document, "div", { class: "cue like", id: "cue-like" }, [
    el(document, "span", { text: "like" })
  ]));
  body.appendChild(el(document, "div", { class: "cue judge", id: "cue-judge" }, [
    el(document, "span", { text: "judge" })
  ]));
  body.appendChild(el(document, "button", {
    class: "status", id: "status", type: "button", "aria-label": "this page"
  }));
  body.appendChild(el(document, "span", { class: "heart", id: "heart", "aria-hidden": "true" }));
  body.appendChild(el(document, "div", { class: "caption", id: "caption" }));
  body.appendChild(el(document, "p", { class: "toast", id: "toast", role: "status" }));
  body.appendChild(el(document, "button", {
    class: "touching-pill", id: "touching", type: "button",
    text: "the sketch has the touch · tap here to swipe again"
  }));
  body.appendChild(el(document, "div", { class: "dim", id: "dim" }));

  body.appendChild(el(document, "section", {
    class: "sheet", id: "sheet-info", "aria-label": "the words"
  }, sheetHead(document, [
    el(document, "h2", { id: "i-title" }),
    el(document, "p", { class: "note", id: "i-sub" }),
    el(document, "h3", { text: "Brief" }),
    el(document, "p", { id: "i-brief" }),
    el(document, "h3", {}, [
      el(document, "span", { text: "Artist's statement · " }),
      el(document, "span", { id: "i-by" })
    ]),
    el(document, "p", { id: "i-statement" }),
    el(document, "h3", { text: "Judgment" }),
    el(document, "div", { class: "pop human" }, [
      el(document, "span", { class: "pop-label", text: "Humans" }),
      el(document, "span", { class: "score", id: "i-jh" }),
      el(document, "span", { class: "quad", id: "i-qh" })
    ]),
    el(document, "div", { class: "pop agent" }, [
      el(document, "span", { class: "pop-label", text: "Agents" }),
      el(document, "span", { class: "score", id: "i-ja" }),
      el(document, "span", { class: "quad", id: "i-qa" })
    ]),
    el(document, "h3", { text: "Provenance" }),
    el(document, "dl", { class: "fields", id: "i-fields" }),
    el(document, "p", { class: "links" }, [
      el(document, "a", { id: "i-entry", href: "#", text: "open the entry page" }),
      el(document, "a", { id: "i-source", href: "#", text: "sketch.js" })
    ])
  ])));

  body.appendChild(el(document, "section", {
    class: "sheet judge", id: "sheet-judge", "aria-label": "judge"
  }, sheetHead(document, [
    el(document, "h2", { text: "Judge it against another" }),
    el(document, "p", {
      class: "note",
      text: "A is the sketch you were watching. B is another from the gallery. Tap either to play it above."
    }),
    el(document, "div", { class: "pair" }, [
      tile(document, "A", "playing"),
      tile(document, "B", "tap to play")
    ]),
    question(document, "brief", "Which is closer to its brief?"),
    question(document, "look", "Which would you rather look at?"),
    el(document, "p", { class: "note", id: "j-status" }),
    el(document, "div", { class: "reveal", id: "j-reveal", hidden: true }, [
      el(document, "h3", { text: "What the agents said" }),
      el(document, "div", { id: "j-agents" }),
      el(document, "p", {
        class: "note",
        text: "Shown after your answers, never before: being shown one first would anchor the other."
      }),
      el(document, "p", { class: "note", id: "j-both" }),
      el(document, "div", { class: "after" }, [
        el(document, "button", { type: "button", id: "j-another", text: "judge another pair" }),
        el(document, "button", { type: "button", id: "j-back", text: "keep swiping" })
      ])
    ])
  ])));

  body.appendChild(el(document, "section", {
    class: "sheet", id: "sheet-settings", "aria-label": "this page"
  }, sheetHead(document, [
    el(document, "h2", { text: "This page" }),
    el(document, "p", { class: "note", id: "s-place" }),
    el(document, "h3", { text: "Order" }),
    el(document, "ul", { class: "orders", id: "s-orders" }),
    el(document, "h3", { text: "You" }),
    el(document, "p", { class: "session-row", id: "s-session" }),
    el(document, "p", {
      class: "note",
      text: "Likes and judgments are counted by GitHub login. A sketch counts as a view once it has been on screen for ten seconds."
    }),
    el(document, "h3", { text: "Launch link for this setup" }),
    el(document, "p", { class: "launch", id: "s-launch" }),
    el(document, "p", { class: "links" }, [
      el(document, "a", { href: "./index.html", text: "the gallery" }),
      el(document, "a", { href: "./kiosk.html", text: "the kiosk" })
    ])
  ])));

  body.appendChild(el(document, "section", {
    class: "sheet", id: "sheet-signin", "aria-label": "sign in"
  }, sheetHead(document, [
    el(document, "h2", { id: "si-title", text: "Sign in to like it" }),
    el(document, "p", {
      text: "Likes and judgments are counted by GitHub login, so the gallery can tell one person from many. You come straight back here afterwards."
    }),
    el(document, "button", { class: "big", type: "button", id: "si-go", text: "Sign in with GitHub" }),
    el(document, "button", { class: "quiet-btn", type: "button", "data-close": "", text: "not now" })
  ])));
}

/* ---- the world the script runs in ------------------------------------------ */

/* Every request every run in this file made, so the acceptance test can pin
 * the whole list of writes rather than the ones one run looked at. */
const EVERY = [];

function answer(payload, status) {
  const code = status || 200;
  return Promise.resolve({
    ok: code < 400,
    status: code,
    json: function () { return Promise.resolve(payload); },
    text: function () { return Promise.resolve(JSON.stringify(payload)); }
  });
}

function load(options) {
  const opts = options || {};
  const window = makeWindow();
  const document = window.document;
  window.location.pathname = "/swipe.html";
  window.location.search = opts.search || "";
  window.location.hash = opts.hash || "";
  swipePage(document);
  window.SKETCHGEN_ROOT = "./";
  if (opts.stored) { window.localStorage.setItem("sketchgen-swipe", opts.stored); }
  if (opts.signedIn) { window.localStorage.setItem("sketchgen_session", "tok.1.sig"); }
  if (opts.hiddenTab) { document.visibilityState = "hidden"; }
  // Node lays nothing out, so a test that wants the frame fitted says how big
  // the stage is. A phone, in portrait.
  document.getElementById("stage").clientWidth = opts.stage ? opts.stage[0] : 390;
  document.getElementById("stage").clientHeight = opts.stage ? opts.stage[1] : 844;

  // Its own copy, and the write path moves it: a like that lands comes back
  // from /counts one higher, which is what the caption refreshes to. Shared
  // fixture rows would drift from run to run instead.
  const counts = JSON.parse(JSON.stringify(COUNTS));

  const asked = [];
  window.fetch = function (url, init) {
    const call = { url: String(url), init: init || null };
    const text = String(url);
    asked.push(call);
    EVERY.push(call);
    if (text.indexOf("swipe.json") !== -1) {
      if (opts.manifest === "reject") { return Promise.reject(new Error("offline")); }
      if (opts.manifest === "empty") { return answer({ entries: [] }); }
      return answer({ entries: JSON.parse(JSON.stringify(ENTRIES)) });
    }
    if (text.indexOf("config.json") !== -1) {
      return answer({
        write_path: opts.writePath === null ? "" : "https://write.example.invalid/api"
      });
    }
    if (text.indexOf("pairs.json") !== -1) { return answer(JSON.parse(JSON.stringify(PAIRS))); }
    if (/e\/(\d+)\/meta\.json$/.test(text)) {
      return answer(METAS[Number(/e\/(\d+)\/meta\.json$/.exec(text)[1])]);
    }
    if (text.indexOf("/counts") !== -1) {
      return answer({ counts: JSON.parse(JSON.stringify(counts)) });
    }
    if (text.indexOf("/me") !== -1) {
      return answer(opts.signedIn ? { username: "profcarroll" } : {});
    }
    if (text.indexOf("/view") !== -1) { return answer({ ok: true, counted: true }); }
    if (text.indexOf("/like") !== -1) {
      if (opts.likeRefuses) { return answer({ error: "unauthorised" }, 401); }
      const wanted = JSON.parse(init.body);
      const row = counts[wanted.entry_id];
      if (row) { row.likes = Math.max(0, row.likes + (wanted.on ? 1 : -1)); }
      return answer({ ok: true, on: wanted.on });
    }
    if (text.indexOf("/vote") !== -1) {
      if (opts.voteRefuses) { return answer({ error: "unauthorised" }, 401); }
      return answer({ ok: true });
    }
    if (text.indexOf("/logout") !== -1) { return answer({ ok: true }); }
    return Promise.reject(new Error("offline: " + url));
  };

  // A timer queue rather than node's: the stage's 160 ms, the hold's 450 and
  // the toast's 1600 are all stepped by hand below.
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
  window.setInterval = function () { return 0; };
  window.clearInterval = function () {};

  // One clock for both queues, as the kiosk harness has it: window.step
  // advances it and runs the animation frames dom.js is holding, and then
  // whatever timeouts that took us past. So a test that wants N frames calls
  // this N times, and the dwell that earns a view — which counts rAF deltas —
  // is driven a frame at a time with no wall clock involved.
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
    decodeURIComponent: decodeURIComponent,
    encodeURIComponent: encodeURIComponent
  }), { filename: "swipe.js" });

  return { window, document, asked, tock };
}

/* The stage takes this long to leave before the seat changes (§4.4). */
const LEAVE = 160;
const HOLD = 450;

function settleTurn() {
  return new Promise(function (resolve) { setTimeout(resolve, 0); });
}

async function quiet() {
  // Enough turns for swipe.json, config.json, /counts, /me, a meta.json and
  // pairs.json to land, and for the paints each of them triggers to run.
  for (let turn = 0; turn < 10; turn += 1) { await settleTurn(); }
}

async function started(options) {
  const world = load(options);
  await quiet();
  world.document.getElementById("go").click();
  await quiet();
  return world;
}

/* ---- driving the page ------------------------------------------------------ */

let pointer = 0;

/* One gesture, start to finish: down, two moves, a stretch of clock, up.
 *
 * The clock matters. A vertical drag commits if it went far enough OR fast
 * enough (COMMIT_V, or half a pixel per millisecond), so a drag with no time
 * in it would commit on any distance at all, and a test for "40 px snaps back"
 * would be testing nothing. The moves come before the clock is stepped so the
 * axis is locked and the hold timer cleared before it could fire.
 */
function drag(world, dx, dy, ms) {
  const shield = world.document.getElementById("shield");
  const id = (pointer += 1);
  const from = { x: 200, y: 420 };
  shield.dispatch("pointerdown", { pointerId: id, clientX: from.x, clientY: from.y });
  shield.dispatch("pointermove", {
    pointerId: id, clientX: from.x + dx / 2, clientY: from.y + dy / 2
  });
  shield.dispatch("pointermove", { pointerId: id, clientX: from.x + dx, clientY: from.y + dy });
  world.tock(ms === undefined ? 200 : ms);
  shield.dispatch("pointerup", { pointerId: id, clientX: from.x + dx, clientY: from.y + dy });
}

/* A release inside 10 px and 300 ms. No clock is stepped at all, so it is
 * inside both bounds by construction. */
function tap(world, x, y) {
  const shield = world.document.getElementById("shield");
  const id = (pointer += 1);
  const at = { clientX: x === undefined ? 200 : x, clientY: y === undefined ? 420 : y };
  shield.dispatch("pointerdown", { pointerId: id, clientX: at.clientX, clientY: at.clientY });
  shield.dispatch("pointerup", { pointerId: id, clientX: at.clientX, clientY: at.clientY });
}

/* A press that does not move for HOLD_MS. The hold timer clears the gesture,
 * so the release that follows can never be read as a tap. */
function hold(world) {
  const shield = world.document.getElementById("shield");
  const id = (pointer += 1);
  shield.dispatch("pointerdown", { pointerId: id, clientX: 200, clientY: 420 });
  world.tock(HOLD);
  shield.dispatch("pointerup", { pointerId: id, clientX: 200, clientY: 420 });
}

function press(document, key) {
  (document.listeners.keydown || []).forEach(function (fn) {
    fn({ key: key, metaKey: false, ctrlKey: false, altKey: false, preventDefault: function () {} });
  });
}

/* A click on something a delegating handler is listening for: dispatched at
 * the container with the real target on the event, which is what a browser
 * would have handed it. */
function clickIn(host, target) {
  host.dispatch("click", { target: target });
}

function showing(document) {
  const num = document.querySelector(".prompt .num");
  return num ? Number(num.textContent.replace("#", "")) : null;
}

function frames(document) { return document.querySelectorAll("iframe.sketch"); }

function classes(document) { return document.body.className.split(/\s+/).filter(Boolean); }

function openSheetId(document) {
  const sheet = document.querySelector(".sheet.open");
  return sheet ? sheet.getAttribute("id") : null;
}

/* Every write the script made, in order, unpacked. A POST that is not one of
 * the three would land here too, which is the point. */
function posted(asked) {
  return asked
    .filter(function (one) { return one.init && one.init.method; })
    .map(function (one) {
      return {
        url: one.url,
        method: one.init.method,
        body: JSON.parse(one.init.body),
        credentials: one.init.credentials || null,
        auth: (one.init.headers || {}).Authorization || null,
        keys: Object.keys(one.init).sort()
      };
    });
}

/* ---- the runs -------------------------------------------------------------- */

/* Each of the seven orders, three seats deep: the first sketch and the two the
 * feed walks up to. */
async function orderIs(order) {
  const world = await started({ search: "?order=" + order });
  const seen = [showing(world.document)];
  for (let step = 0; step < 2; step += 1) {
    drag(world, 0, -80);
    world.tock(LEAVE);
    seen.push(showing(world.document));
  }
  return seen;
}

async function orders() {
  const out = {};
  const names = ["newest", "oldest", "random", "liked", "reviewed", "controversial", "consensus"];
  for (const name of names) { out[name] = await orderIs(name); }
  return out;
}

/* ?at= seats on that id; the address bar follows every seat; the order is
 * stored and a stored one comes back. */
async function seating() {
  const at = await started({ search: "?at=22&order=oldest" });
  const seated = showing(at.document);

  const liked = await started({ search: "?order=liked" });
  const first = liked.window.history.lastUrl;
  drag(liked, 0, -80);
  liked.tock(LEAVE);
  const second = liked.window.history.lastUrl;

  const stored = await started({ stored: JSON.stringify({ v: 1, order: "oldest" }) });
  const urlWins = await started({
    stored: JSON.stringify({ v: 1, order: "oldest" }), search: "?order=newest"
  });
  const nonsense = await started({
    stored: JSON.stringify({ v: 1, order: "oldest" }), search: "?order=sideways"
  });
  // An id the manifest does not carry is seat 0, not an error.
  const missing = await started({ search: "?at=999&order=oldest" });

  return {
    seated: seated,
    addressAfterFirstSeat: first,
    addressAfterSecondSeat: second,
    seatAfterSecond: showing(liked.document),
    storedBlob: JSON.parse(liked.window.localStorage.getItem("sketchgen-swipe")),
    fromStorage: showing(stored.document),
    urlWins: showing(urlWins.document),
    nonsense: showing(nonsense.document),
    missingAt: showing(missing.document)
  };
}

/* Up, down, and a drag that does not reach the threshold. */
async function feed() {
  const world = await started({ search: "?order=newest" });
  const first = frames(world.document);
  const attrs = first.length === 1 ? Object.keys(first[0].attributes).sort() : null;
  const start = showing(world.document);

  drag(world, 0, -80);
  world.tock(LEAVE);
  const up = { entry: showing(world.document), frames: frames(world.document).length };

  drag(world, 0, 80);
  world.tock(LEAVE);
  const down = { entry: showing(world.document), frames: frames(world.document).length };

  drag(world, 0, -40);
  world.tock(LEAVE);
  const short = {
    entry: showing(world.document),
    frames: frames(world.document).length,
    style: String(world.document.getElementById("stage").getAttribute("style") || ""),
    className: world.document.getElementById("stage").className
  };

  // A fixed canvas is laid out at its own size and scaled to contain on a
  // 390x844 phone; a window-sized one leaves the stage to say how big it is.
  const stage = world.document.getElementById("stage");
  const fitted = {
    entry: showing(world.document),
    w: stage.style.getPropertyValue("--frame-w"),
    h: stage.style.getPropertyValue("--frame-h"),
    sx: stage.style.getPropertyValue("--frame-sx")
  };
  drag(world, 0, -80);
  world.tock(LEAVE);
  const windowSized = {
    entry: showing(world.document),
    style: stage.getAttribute("style")
  };

  return {
    start: start,
    framesAtStart: first.length,
    sandbox: first.length ? first[0].getAttribute("sandbox") : null,
    src: first.length ? first[0].getAttribute("src") : null,
    attributes: attrs,
    up: up,
    down: down,
    short: short,
    fitted: fitted,
    windowSized: windowSized,
    caption: world.document.getElementById("caption").textContent
  };
}

/* The tap, the hold, and the pill that gives the touch back. */
async function tapAndHold() {
  const world = await started({ search: "?order=newest" });
  const before = classes(world.document);
  tap(world);
  const quietNow = classes(world.document);
  tap(world);
  const loudAgain = classes(world.document);

  hold(world);
  const touching = {
    classes: classes(world.document),
    buzzed: world.window.navigator.buzzed.slice()
  };
  // A gesture cannot start while the sketch has the touch.
  drag(world, 0, -80);
  world.tock(LEAVE);
  const stillHere = showing(world.document);
  world.document.getElementById("touching").click();
  const given = classes(world.document);
  drag(world, 0, -80);
  world.tock(LEAVE);

  // The caption sits under the shield: a tap that lands in its box opens all
  // of the words, and one above it toggles them. The box is the test's to
  // set; the phone's bottom third, as on a 375×812 screen.
  world.document.getElementById("caption").rect =
    { left: 0, top: 700, right: 375, bottom: 812, width: 375, height: 112 };
  tap(world, 100, 750);
  await quiet();
  const sheet = openSheetId(world.document);
  world.document.querySelector("#sheet-info [data-close]").click();
  const closedAgain = openSheetId(world.document);
  tap(world, 100, 200);
  const aboveToggles = classes(world.document);

  return {
    before: before,
    quietNow: quietNow,
    loudAgain: loudAgain,
    touching: touching,
    heldEntry: stillHere,
    given: given,
    movedAfter: showing(world.document),
    captionOpens: sheet,
    closedAgain: closedAgain,
    aboveToggles: aboveToggles
  };
}

/* A right swipe: signed out it asks, signed in it posts, and a 401 empties the
 * heart and asks again. */
async function liking() {
  const out = await started({ search: "?order=newest" });
  drag(out, 100, 0);
  await quiet();
  const signedOut = {
    sheet: openSheetId(out.document),
    title: out.document.getElementById("si-title").textContent,
    posts: posted(out.asked).length,
    classes: classes(out.document)
  };

  const world = await started({ search: "?order=newest", signedIn: true });
  const heartBefore = classes(world.document);
  drag(world, 100, 0);
  await quiet();
  const liked = {
    classes: classes(world.document),
    toast: world.document.getElementById("toast").textContent,
    caption: world.document.getElementById("caption").textContent
  };
  drag(world, 100, 0);
  await quiet();
  const unliked = {
    classes: classes(world.document),
    toast: world.document.getElementById("toast").textContent
  };

  const refused = await started({
    search: "?order=newest", signedIn: true, likeRefuses: true
  });
  drag(refused, 100, 0);
  await quiet();

  return {
    signedOut: signedOut,
    heartBefore: heartBefore,
    liked: liked,
    unliked: unliked,
    posts: posted(world.asked),
    refused: {
      classes: classes(refused.document),
      sheet: openSheetId(refused.document),
      token: refused.window.localStorage.getItem("sketchgen_session"),
      posts: posted(refused.asked).length
    }
  };
}

/* A left swipe: the pair, the blinding, the swap, the two votes and the
 * reveal. */
async function judging() {
  const world = await started({ search: "?order=newest", signedIn: true });
  drag(world, -100, 0);
  await quiet();

  const document = world.document;
  const a = 33;
  const briefA = document.getElementById("j-brief-a").textContent;
  const briefB = document.getElementById("j-brief-b").textContent;
  // Which partner came up is a coin toss between the two offered pairs, so the
  // run says which and the test asserts over that.
  const b = Object.keys(METAS).map(Number).filter(function (id) {
    return id !== a && METAS[id].brief === briefB;
  })[0];
  const page = document.documentElement.innerHTML;
  const leaked = secretsOf(b).filter(function (secret) { return page.indexOf(secret) !== -1; });

  const opened = {
    sheet: openSheetId(document),
    revealHidden: document.getElementById("j-reveal").hidden,
    classes: classes(document),
    frames: frames(document).length,
    src: frames(document)[0].getAttribute("src"),
    status: document.getElementById("j-status").textContent
  };

  // Tapping B's tile swaps the one iframe to B's sketch.
  document.querySelector("[data-side=\"B\"]").click();
  const swapped = {
    frames: frames(document).length,
    src: frames(document)[0].getAttribute("src"),
    pressed: Array.prototype.map.call(document.querySelectorAll(".tile"), function (one) {
      return one.getAttribute("data-side") + ":" + one.getAttribute("aria-pressed");
    })
  };

  // One answer is not both: the reveal stays shut.
  document.querySelector("[data-question=\"brief\"] [data-vote=\"A\"]").click();
  await quiet();
  const afterOne = {
    revealHidden: document.getElementById("j-reveal").hidden,
    answered: document.querySelector("[data-question=\"brief\"] .answered").textContent,
    posts: posted(world.asked).filter(function (one) {
      return one.url.indexOf("/vote") !== -1;
    }).length
  };

  document.querySelector("[data-question=\"look\"] [data-vote=\"B\"]").click();
  await quiet();
  const afterBoth = {
    revealHidden: document.getElementById("j-reveal").hidden,
    verdicts: Array.prototype.map.call(
      document.querySelectorAll("#j-agents .verdict"), function (one) { return one.textContent; }
    ),
    both: document.getElementById("j-both").textContent,
    links: Array.prototype.map.call(
      document.querySelectorAll("#j-both a"), function (one) { return one.getAttribute("href"); }
    )
  };

  // Another pair: A stays, B changes, the reveal shuts again.
  document.getElementById("j-another").click();
  await quiet();
  const another = {
    briefA: document.getElementById("j-brief-a").textContent,
    briefB: document.getElementById("j-brief-b").textContent,
    revealHidden: document.getElementById("j-reveal").hidden,
    answered: document.querySelector("[data-question=\"brief\"] .answered").textContent,
    src: frames(document)[0].getAttribute("src")
  };
  const secondB = Object.keys(METAS).map(Number).filter(function (id) {
    return METAS[id].brief === another.briefB;
  })[0];

  // Keep swiping: the sheet closes and A is framed again.
  document.querySelector("[data-side=\"B\"]").click();
  document.getElementById("j-back").click();
  const back = {
    sheet: openSheetId(document),
    classes: classes(document),
    frames: frames(document).length,
    src: frames(document)[0].getAttribute("src"),
    entry: showing(document)
  };

  // And signed out, the answers are noted on the sheet only.
  const anon = await started({ search: "?order=newest" });
  drag(anon, -100, 0);
  await quiet();
  anon.document.querySelector("[data-question=\"look\"] [data-vote=\"tie\"]").click();
  await quiet();
  const signedOut = {
    status: anon.document.getElementById("j-status").textContent,
    answered: anon.document.querySelector("[data-question=\"look\"] .answered").textContent,
    sheet: openSheetId(anon.document)
  };
  // The offer under the questions is the only way to the sign-in sheet from
  // here, and it takes a tap.
  clickIn(
    anon.document.getElementById("j-status"),
    anon.document.getElementById("j-signin")
  );
  const offered = {
    sheet: openSheetId(anon.document),
    title: anon.document.getElementById("si-title").textContent
  };

  return {
    a: a,
    b: b,
    briefA: briefA,
    briefB: briefB,
    leaked: leaked,
    opened: opened,
    swapped: swapped,
    afterOne: afterOne,
    afterBoth: afterBoth,
    another: another,
    secondB: secondB,
    back: back,
    votes: posted(world.asked).filter(function (one) { return one.url.indexOf("/vote") !== -1; }),
    signedOut: signedOut,
    offered: offered,
    anonPosts: posted(anon.asked).length
  };
}

/* The view (§4.7): ten un-paused, tab-visible seconds on one frame, once. */
async function viewing() {
  const dwelled = await started({ search: "?order=newest", signedIn: true });
  const seat = showing(dwelled.document);
  dwelled.tock(9000);
  const atNine = posted(dwelled.asked).length;
  dwelled.tock(1000);
  const atTen = posted(dwelled.asked);
  // Six hundred more frames on the same seat. One view, still: the flag is set
  // before the request goes out, so no frame can post a second.
  for (let step = 0; step < 600; step += 1) { dwelled.tock(16); }
  const afterSixHundredFrames = posted(dwelled.asked).length;

  // A sketch somebody swiped past was never on screen for ten seconds.
  const skipped = await started({ search: "?order=newest", signedIn: true });
  skipped.tock(4000);
  drag(skipped, 0, -80);
  skipped.tock(LEAVE);
  skipped.tock(4000);
  const afterASkip = { posts: posted(skipped.asked).length, entry: showing(skipped.document) };

  // A tab nobody is looking at counts nothing.
  const hidden = await started({ search: "?order=newest", signedIn: true, hiddenTab: true });
  for (let step = 0; step < 40; step += 1) { hidden.tock(1000); }
  const whileHidden = posted(hidden.asked).length;
  hidden.document.visibilityState = "visible";
  hidden.tock(10000);
  const afterComingBack = posted(hidden.asked).length;

  // The judge sheet's swap is a seat of its own: ten seconds on B is B's view.
  const judged = await started({ search: "?order=newest", signedIn: true });
  judged.tock(9000);
  drag(judged, -100, 0);
  await quiet();
  judged.document.querySelector("[data-side=\"B\"]").click();
  judged.tock(9000);
  const beforeBEarnsIt = posted(judged.asked).length;
  judged.tock(2000);
  const bEarnedIt = posted(judged.asked);
  const bOnTheStage = Number(/e\/(\d+)\//.exec(frames(judged.document)[0].getAttribute("src"))[1]);

  return {
    seat: seat,
    atNine: atNine,
    atTen: atTen,
    afterSixHundredFrames: afterSixHundredFrames,
    afterASkip: afterASkip,
    whileHidden: whileHidden,
    afterComingBack: afterComingBack,
    beforeBEarnsIt: beforeBEarnsIt,
    bEarnedIt: bEarnedIt,
    bOnTheStage: bOnTheStage
  };
}

/* No write path: em dashes, no heart, a toast instead of a like, and not one
 * request to base(). */
async function offline() {
  const world = await started({ search: "?order=newest", writePath: null });
  const start = {
    caption: world.document.getElementById("caption").textContent,
    heartHidden: world.document.getElementById("heart").hidden
  };
  drag(world, 100, 0);
  await quiet();
  const right = {
    toast: world.document.getElementById("toast").textContent,
    sheet: openSheetId(world.document),
    classes: classes(world.document)
  };
  world.tock(11000);
  drag(world, -100, 0);
  await quiet();
  const judge = { sheet: openSheetId(world.document) };
  world.document.querySelector("[data-question=\"brief\"] [data-vote=\"A\"]").click();
  await quiet();
  const noted = world.document.querySelector("[data-question=\"brief\"] .answered").textContent;

  return {
    start: start,
    right: right,
    judge: judge,
    noted: noted,
    asked: world.asked.map(function (one) { return one.url; }),
    posts: posted(world.asked).length
  };
}

/* The words sheet: what it prints, and that meta.json is asked for once. */
async function words() {
  const world = await started({ search: "?order=oldest" });
  const document = world.document;
  document.getElementById("caption").click();
  await quiet();
  const fields = {};
  const terms = document.querySelectorAll("#i-fields dt");
  const values = document.querySelectorAll("#i-fields dd");
  for (let at = 0; at < terms.length; at += 1) {
    fields[terms[at].textContent] = values[at].textContent;
  }
  const shown = {
    sheet: openSheetId(document),
    title: document.getElementById("i-title").textContent,
    sub: document.getElementById("i-sub").textContent,
    brief: document.getElementById("i-brief").textContent,
    by: document.getElementById("i-by").textContent,
    statement: document.getElementById("i-statement").textContent,
    human: document.getElementById("i-jh").textContent,
    humanQuad: document.getElementById("i-qh").textContent,
    agent: document.getElementById("i-ja").textContent,
    agentQuad: document.getElementById("i-qa").textContent,
    fields: fields,
    entryLink: document.getElementById("i-entry").getAttribute("href"),
    sourceLink: document.getElementById("i-source").getAttribute("href")
  };

  // Closed on the ✕, reopened: still one request for that entry's meta.json.
  document.querySelector("#sheet-info [data-close]").click();
  const closed = openSheetId(document);
  document.getElementById("caption").click();
  await quiet();
  const metaCalls = world.asked.filter(function (one) {
    return one.url.indexOf("e/11/meta.json") !== -1;
  }).length;

  // Escape closes it too.
  press(document, "Escape");
  const afterEscape = openSheetId(document);
  const dimAfterEscape = document.getElementById("dim").className;

  // So does a tap on the dimmed sketch behind it.
  document.getElementById("caption").click();
  await quiet();
  document.getElementById("dim").click();
  const afterDim = openSheetId(document);

  // And so does dragging the sheet itself down past 90 px, from the top of
  // its own scroll, which is the gesture a phone taught every bottom sheet.
  document.getElementById("caption").click();
  await quiet();
  const sheet = document.getElementById("sheet-info");
  sheet.dispatch("pointerdown", { pointerId: 900, clientY: 300 });
  sheet.dispatch("pointermove", { pointerId: 900, clientY: 360 });
  const followed = String(sheet.getAttribute("style") || "");
  sheet.dispatch("pointerup", { pointerId: 900, clientY: 430 });
  const afterDragDown = openSheetId(document);

  // A shorter drag does not: it springs back and the sheet stays.
  document.getElementById("caption").click();
  await quiet();
  sheet.dispatch("pointerdown", { pointerId: 901, clientY: 300 });
  sheet.dispatch("pointermove", { pointerId: 901, clientY: 340 });
  sheet.dispatch("pointerup", { pointerId: 901, clientY: 340 });
  const afterShortDrag = {
    sheet: openSheetId(document),
    style: String(sheet.getAttribute("style") || "")
  };

  return {
    shown: shown,
    closed: closed,
    metaCalls: metaCalls,
    afterEscape: afterEscape,
    dimAfterEscape: dimAfterEscape,
    afterDim: afterDim,
    followed: followed,
    afterDragDown: afterDragDown,
    afterShortDrag: afterShortDrag
  };
}

/* The settings sheet: the orders, the session, the launch link. */
async function settings() {
  const world = await started({ search: "?order=newest", signedIn: true });
  const document = world.document;
  document.getElementById("status").click();
  await quiet();
  const open = {
    sheet: openSheetId(document),
    place: document.getElementById("s-place").textContent,
    rows: Array.prototype.map.call(document.querySelectorAll("#s-orders button"), function (one) {
      return one.getAttribute("data-order") + ":" + one.getAttribute("aria-current");
    }),
    session: document.getElementById("s-session").textContent,
    launch: document.getElementById("s-launch").textContent,
    statusLine: document.getElementById("status").textContent
  };

  // Changing the order keeps the sketch on screen and re-seats it.
  const before = showing(document);
  clickIn(
    document.getElementById("s-orders"),
    document.querySelector("#s-orders [data-order=\"consensus\"]")
  );
  const reordered = {
    entry: showing(document),
    frames: frames(document).length,
    launch: document.getElementById("s-launch").textContent,
    address: world.window.history.lastUrl,
    statusLine: document.getElementById("status").textContent,
    stored: JSON.parse(world.window.localStorage.getItem("sketchgen-swipe"))
  };

  // Sign out: the token goes, the row changes, the likes this page-load knew
  // about go with it.
  clickIn(document.getElementById("s-session"), document.getElementById("s-out"));
  await quiet();
  const out = {
    session: document.getElementById("s-session").textContent,
    token: world.window.localStorage.getItem("sketchgen_session"),
    toast: document.getElementById("toast").textContent
  };

  return { open: open, before: before, reordered: reordered, out: out };
}

/* Signing in from here: the note, and where it goes. */
async function signin() {
  const world = await started({ search: "?order=newest&at=22" });
  const document = world.document;
  drag(world, 100, 0);
  await quiet();
  const asked = openSheetId(document);
  document.getElementById("si-go").click();
  const note = world.window.localStorage.getItem("sketchgen-swipe-return");
  const went = world.window.location.href;

  // Declined, a like does nothing at all.
  const declined = await started({ search: "?order=newest" });
  drag(declined, 100, 0);
  await quiet();
  declined.document.querySelector("#sheet-signin [data-close]").click();
  const after = {
    sheet: openSheetId(declined.document),
    classes: classes(declined.document),
    posts: posted(declined.asked).length
  };

  // A token in the fragment is claimed and stripped, as it is everywhere else.
  const claimed = load({ search: "?order=newest", hash: "#session=tok.9.sig" });
  await quiet();

  return {
    asked: asked,
    note: note,
    went: went,
    after: after,
    claimedToken: claimed.window.localStorage.getItem("sketchgen_session"),
    claimedAddress: claimed.window.history.lastUrl
  };
}

/* The six courtesy keys, which do the six things a thumb does. */
async function keys() {
  const world = await started({ search: "?order=newest", signedIn: true });
  const document = world.document;
  const start = showing(document);
  press(document, "ArrowUp");
  world.tock(LEAVE);
  const up = showing(document);
  press(document, "ArrowDown");
  world.tock(LEAVE);
  const down = showing(document);
  press(document, "i");
  const quietNow = classes(document);
  press(document, "I");
  const loud = classes(document);
  press(document, "l");
  await quiet();
  const liked = classes(document);
  press(document, "j");
  await quiet();
  const judging = openSheetId(document);
  press(document, "Escape");
  const closed = openSheetId(document);

  // A modifier chord is a browser shortcut and stays one.
  const chord = showing(document);
  (document.listeners.keydown || []).forEach(function (fn) {
    fn({ key: "ArrowUp", metaKey: true, ctrlKey: false, altKey: false, preventDefault: function () {} });
  });
  world.tock(LEAVE);

  return {
    start: start,
    up: up,
    down: down,
    quietNow: quietNow,
    loud: loud,
    liked: liked,
    judging: judging,
    closed: closed,
    afterChord: showing(document) === chord
  };
}

/* The start card holds the tap until there is something to swipe through. */
async function starting() {
  const waiting = load({});
  const before = {
    disabled: waiting.document.getElementById("go").disabled === true,
    label: waiting.document.getElementById("go").textContent,
    welcomeUp: waiting.document.getElementById("welcome").hidden === false
  };
  await quiet();
  const loaded = {
    disabled: waiting.document.getElementById("go").disabled === true,
    label: waiting.document.getElementById("go").textContent
  };

  const broken = load({ manifest: "reject" });
  await quiet();
  broken.document.getElementById("go").click();
  await quiet();
  const failed = {
    note: broken.document.querySelector(".welcome .note").textContent,
    welcomeUp: broken.document.getElementById("welcome").hidden === false,
    frames: frames(broken.document).length,
    classes: classes(broken.document)
  };
  // And the gestures must be inert rather than half alive.
  drag(broken, 0, -80);
  broken.tock(LEAVE);
  const stillNothing = frames(broken.document).length;

  return { before: before, loaded: loaded, failed: failed, stillNothing: stillNothing };
}

async function main() {
  const report = {
    orders: await orders(),
    seating: await seating(),
    feed: await feed(),
    tapAndHold: await tapAndHold(),
    liking: await liking(),
    judging: await judging(),
    viewing: await viewing(),
    offline: await offline(),
    words: await words(),
    settings: await settings(),
    signin: await signin(),
    keys: await keys(),
    starting: await starting()
  };
  // Every request every run above made, so the test can pin the whole list of
  // writes rather than one run's.
  report.everyPost = posted(EVERY).map(function (one) {
    return {
      url: one.url,
      method: one.method,
      credentials: one.credentials,
      hasAuth: !!one.auth,
      keys: one.keys
    };
  });
  report.everyCredentials = EVERY
    .filter(function (one) { return one.init && one.init.credentials !== undefined; })
    .map(function (one) { return one.init.credentials; });
  console.log(JSON.stringify(report));
  process.exit(0);
}

main().catch(function (err) {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
