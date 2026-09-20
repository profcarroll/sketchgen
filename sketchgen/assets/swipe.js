/* swipe.js — the phone page, swipe.html, and nothing else.
 *
 * One sketch at a time, full screen, nothing timed, and six gestures instead
 * of a keyboard: up is the next sketch, down the last one, right likes it,
 * left judges it against another, a tap shows and hides the words, a hold
 * hands the touch to the sketch. It is the entry page's three verbs — view,
 * like, judge — with the page taken away. The layout, the copy, the gesture
 * constants and the sheets are the mockup's (docs/plans/swipe-mockup/
 * swipe.html); this file is that script ported onto the real gallery's data
 * and the real gallery's sandbox.
 *
 * It shares a great deal with kiosk.js — the manifest loader, the seven
 * comparators, showEntry, fitFrame, promptParts, the caption, quadText, the
 * settings blob, the rAF dwell that earns a view — and every one of those is
 * marked below as the kiosk's, with the kiosk's name kept. The kiosk's timer,
 * menu, key table and fourteen overlays are most of that file and none of
 * this one, which is why this is not a fork of it: if the two ever have to be
 * one module, the matching names are what makes that a mechanical change.
 *
 * What it reads, and that is the whole list (spec §4.1):
 *
 *   swipe.json        the slim manifest render_index writes: every published
 *                     entry's prompt, judgment, provenance, canvas and where
 *                     to run it — no brief, no statement, a fifth of kiosk.json
 *   config.json       the write path's base URL, and nothing else
 *   <base>/counts     views and likes, in batches of a hundred, once at start
 *   <base>/me         who is signed in, once at start
 *   e/<id>/meta.json  the brief, the statement and the provenance the words
 *                     sheet prints — fetched when a sheet needs it, once per id
 *   pairs.json        the balanced pairs and the agents' verdicts, on the
 *                     first judge and never again
 *   <root><sketch>    in a sandboxed iframe, which is where the sketch runs
 *
 * What it writes, and that is the whole list:
 *
 *   <base>/view       one view of one entry, once that entry has been on the
 *                     screen, un-paused and tab-visible, for ten seconds (§4.7)
 *   <base>/like       one like, identified by the GitHub session
 *   <base>/vote       one answer to one of the compare page's two questions
 *
 * All three carry the session as a bearer header, because unlike the kiosk
 * this page is somebody's phone: a like and a vote are counted by GitHub
 * login. The session is the gallery's own — the same sketchgen_session key
 * gallery.js keeps on this origin — and the helpers that read it are
 * gallery.js's, duplicated (§4.6): the closure exports nothing, and forty
 * lines copied is cheaper than a shared module across every page. This page
 * asks for a sign-in only when a like or a vote needs one, never at start.
 *
 * A finger that lands on the sandboxed frame belongs to the frame, and there
 * is no channel to ask for it back, so a transparent shield sits over the
 * stage and takes every gesture (§4.4). The cost is that a sketch that
 * answers a touch cannot feel one, and the hold is what pays it: half a
 * second without moving lifts the shield and one pill gives the touch back.
 *
 * What it must never do (spec §4.8), and what the tests hold it to: advance
 * on its own; count a view before ten seconds; hold more than one iframe,
 * postMessage to a frame, or evaluate sketch source; touch storage beyond
 * sketchgen-swipe, the shared sketchgen_session and sketchgen-swipe-return,
 * which it only ever writes; read document.cookie; show B's prompt, authors,
 * counts or verdicts before both questions are answered; open the sign-in
 * sheet before the visitor has done something that needs one; or send a
 * request the list above does not name.
 *
 * Vanilla, ES5, no build step, no framework, no web font, nothing from a CDN.
 */

(function () {
  "use strict";

  var ROOT = window.SKETCHGEN_ROOT || "./";

  /* The three localStorage keys this page is allowed to touch (spec §4.8).
   *
   * SETTINGS_KEY is its own, STORAGE_KEY is the gallery's session and is
   * shared on purpose — it is the gallery's session and this page is the
   * gallery — and RETURN_KEY is the note the front page reads once after a
   * sign-in and sends the visitor back here with (§5). This file only ever
   * writes that last one; gallery.js is the only reader. */
  var SETTINGS_KEY = "sketchgen-swipe";
  var STORAGE_KEY = "sketchgen_session";
  var RETURN_KEY = "sketchgen-swipe-return";

  /* The stored settings blob's shape. One key in it so far, and a version
   * anyway: the kiosk learned what an unversioned blob costs. */
  var SETTINGS_VERSION = 1;

  /* The gesture engine, from the mockup, and these numbers are the spec
   * (§4.4). Under a hundred lines of behaviour hangs off them. */
  var AXIS_LOCK = 12;         /* the first axis to move this far owns it */
  var TAP_MOVE = 10;          /* a release inside this, and TAP_MS, is a tap */
  var TAP_MS = 300;
  var HOLD_MS = 450;          /* a press this long without an axis is a hold */
  var COMMIT_V = 64;          /* a vertical drag past this commits… */
  var FLICK = 0.5;            /* …or past this, in px per ms */
  var COMMIT_H = 90;          /* a horizontal drag past this commits */
  var FOLLOW_V = 0.6;         /* how far the stage follows a vertical finger */
  var FOLLOW_H = 0.35;        /* and a horizontal one */
  /* The stage leaves the way the finger went over this long, and the next
   * one comes in from the far edge over the 220 ms of `.stage.snap`, which
   * lives in the CSS because that half is a transition and not a timer. */
  var LEAVE_MS = 160;
  var SHEET_CLOSE = 90;       /* a sheet dragged down this far closes */
  var BURST_MS = 720;         /* the like cue's one animation */
  var TOAST_MS = 1600;

  /* kiosk.js's, and for the same reason (docs/plans/kiosk-views.md §3.1): ten
   * seconds of un-paused, tab-visible time on one seat is a view. */
  var VIEW_AFTER_S = 10;

  /* The write path answers one statement per ask and its database binds at
   * most a hundred parameters, which is the number gallery.js batches on. */
  var COUNTS_BATCH = 100;

  /* kiosk.js's, which is gallery.py's _COMPASS_STRONG: above this percentile a
   * population is on the strong half of an axis, and the quadrant words are
   * drawn at the same 0.5, so the phrase here and the square on the entry page
   * cannot disagree. */
  var COMPASS_STRONG = 0.5;

  /* kiosk.js's, which is gallery.py's _QUADRANT_WORDS, keyed
   * "<look strong><brief strong>". */
  var QUADRANTS = {
    "11": "looks good, on brief",
    "10": "looks good, misses the brief",
    "01": "on brief, not much to look at",
    "00": "neither"
  };

  /* kiosk.js's, which is gallery.py's NO_PAIRS. A score nobody voted on is
   * not a low score. */
  var NO_PAIRS = "no pairs yet";

  /* kiosk.js's ORDERS: the grid's seven, in the grid's order. */
  var ORDERS = [
    { key: "newest", label: "newest first" },
    { key: "oldest", label: "oldest first" },
    { key: "random", label: "random" },
    { key: "liked", label: "most liked" },
    { key: "reviewed", label: "most reviewed" },
    { key: "controversial", label: "controversial" },
    { key: "consensus", label: "consensus" }
  ];

  var DEFAULT_ORDER = "newest";

  /* Said on the start card in place of its last line when there is nothing to
   * swipe through, as the kiosk's card says it. */
  var CANNOT_LOAD = "Could not load the gallery's list of sketches.";

  /* The one-line answers, and the entry page's own sentence for a gallery
   * whose write path is not up yet. */
  var NO_WRITE_PATH = "the gallery write path is not deployed yet";

  var ENTRIES = [];
  var COUNTS = {};
  var METAS = {};             /* entry id -> a promise for its meta.json */
  var PAIRS = [];             /* pairs.json's offered pairs, on the first judge */
  var AGENTS = {};            /* and its verdicts, keyed "<lo>-<hi>" */
  var pairsAsked = null;
  var config = null;
  var frame = null;           /* the one iframe, or null (spec §4.8) */

  var state = {
    order: DEFAULT_ORDER,
    at: null,                 /* ?at=<id>, honoured once, at the first seat */
    seq: [],
    i: 0,
    started: false,
    words: true,
    touching: false,
    liked: {},                /* ids this page-load has toggled on */
    username: null,
    judging: null,
    stage: null,              /* the entry whose frame is up, which is not */
                              /* the seat while the judge sheet plays B */
    viewed: false,            /* has this frame been counted? */
    dwell: 0                  /* un-paused, tab-visible seconds on it */
  };

  /* ---- small helpers, kiosk.js's ---------------------------------------- */

  function $(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text).replace(/[&<>"]/g, function (ch) {
      if (ch === "&") { return "&amp;"; }
      if (ch === "<") { return "&lt;"; }
      if (ch === ">") { return "&gt;"; }
      return "&quot;";
    });
  }

  /* A count that has not arrived is an em dash, not a zero: the generator
   * never writes a number it does not have, and neither does this file. */
  function num(value) {
    return typeof value === "number" ? Number(value).toLocaleString() : "—";
  }

  function secs(value) {
    return typeof value === "number" ? value.toFixed(1) + " s" : "—";
  }

  function now() {
    return (window.performance && window.performance.now)
      ? window.performance.now() : new Date().getTime();
  }

  function ready(fn) {
    if (document.readyState !== "loading") { fn(); }
    else { document.addEventListener("DOMContentLoaded", fn); }
  }

  function oneOf(list) { return list[Math.floor(Math.random() * list.length)]; }

  /* Copied from gallery.js, deliberately: see the file header. */
  function base() {
    return config && config.write_path
      ? String(config.write_path).replace(/\/+$/, "") : "";
  }

  function loadConfig() {
    return fetch(ROOT + "config.json", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (data) { config = data || {}; return config; })
      .catch(function () { config = {}; return config; });
  }

  /* kiosk.js's loadManifest, over the slim manifest. no-store for the reason
   * the kiosk's has it: render_index rewrites this file on every deploy, and a
   * cached one would hide half the gallery from a phone that kept the tab. */
  function loadManifest() {
    return fetch(ROOT + "swipe.json", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        ENTRIES = (data && data.entries) || [];
        return ENTRIES;
      });
  }

  /* One entry's meta.json, fetched once and kept for the page's life: the
   * words sheet and the judge sheet both want it, and a phone that swipes back
   * to a sketch it has read should not ask again. The promise is what is
   * cached, so two sheets opening at once are still one request. */
  function loadMeta(entry) {
    if (!entry || !entry.meta) { return Promise.resolve(null); }
    if (!METAS[entry.id]) {
      METAS[entry.id] = fetch(ROOT + entry.meta)
        .then(function (response) { return response.json(); })
        .catch(function () { return null; });
    }
    return METAS[entry.id];
  }

  /* pairs.json, on the first judge and never again: it is the balanced offer
   * the generator baked for the compare page, and a phone that never swipes
   * left never asks for it. */
  function loadPairs() {
    if (pairsAsked) { return pairsAsked; }
    pairsAsked = fetch(ROOT + "pairs.json")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        PAIRS = (data && data.pairs) || [];
        AGENTS = (data && data.agents) || {};
      })
      .catch(function () { /* judge against a random entry instead */ });
    return pairsAsked;
  }

  /* ---- the session: gallery.js's, duplicated (spec §4.6) ----------------- */

  /* Why the session is kept here and not left to a cookie: the gallery is on
   * github.io and the write path is on workers.dev, so every call is
   * cross-site and the Worker's SameSite=Lax cookie is not sent on those. So
   * <write_path>/callback redirects back with the same signed token in the
   * fragment, this origin keeps it, and it goes back as a bearer header.
   *
   * The key is the gallery's own, on purpose: it is the gallery's session and
   * this page is the gallery. */
  function readToken() {
    try { return window.localStorage.getItem(STORAGE_KEY) || ""; } catch (err) { return ""; }
  }

  function writeToken(token) {
    try { window.localStorage.setItem(STORAGE_KEY, token); } catch (err) { /* private mode */ }
  }

  function dropToken() {
    try { window.localStorage.removeItem(STORAGE_KEY); } catch (err) { /* ditto */ }
  }

  /* Runs before anything else, as it does on every other page: take the token
   * out of the fragment and out of the address bar. /callback returns to the
   * front page today and never here, but a callback that one day returns here
   * directly is then already handled. */
  function claimTokenFromHash() {
    var hash = window.location.hash || "";
    var token;
    if (hash.indexOf("#session=") !== 0) { return; }
    token = decodeURIComponent(hash.slice("#session=".length));
    if (token) { writeToken(token); }
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    } else {
      window.location.hash = "";
    }
  }

  /* The one place a request's headers are built, so the like and the two
   * votes cannot drift apart. credentials: "include" rides on every call that
   * uses these, which costs nothing and is what works for somebody browsing
   * the Worker's own domain. */
  function authHeaders(extra) {
    var headers = {};
    var key;
    var token;
    if (extra) {
      for (key in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, key)) { headers[key] = extra[key]; }
      }
    }
    token = readToken();
    if (token) { headers["Authorization"] = "Bearer " + token; }
    return headers;
  }

  /* A 401 means the token expired, or the signing key was rotated. Drop it
   * rather than go on presenting it. */
  function refused(response) {
    if (response && response.status === 401) {
      dropToken();
      paintSession(null);
      return true;
    }
    return false;
  }

  /* /me at start paints the settings sheet's You row and decides nothing
   * else: the page does not ask the Worker whether this login has liked an
   * entry, because /counts does not say and no endpoint does. */
  function loadMe() {
    if (!base()) { paintSession(null); return Promise.resolve(null); }
    return fetch(base() + "/me", { credentials: "include", headers: authHeaders() })
      .then(function (response) {
        if (refused(response)) { return null; }
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        var username = data && data.username ? data.username : null;
        paintSession(username);
        return username;
      })
      .catch(function () { paintSession(null); return null; });
  }

  function paintSession(username) {
    state.username = username || null;
    if (!state.username) {
      // A signed-out page has liked nothing it can prove: the map is this
      // page-load's own record and it belonged to whoever just left.
      state.liked = {};
      paintLiked();
    }
    paintSettings();
    paintJudgeStatus();
  }

  /* ---- views and likes --------------------------------------------------- */

  /* kiosk.js's fetchCounts, with the session on it: this page has one, and
   * /counts is the same public read either way. */
  function fetchCounts(ids) {
    return fetch(base() + "/counts?entries=" + encodeURIComponent(ids.join(",")), {
      credentials: "include",
      headers: authHeaders()
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        var rows = (data && data.counts) || data || {};
        var id;
        for (id in rows) {
          if (Object.prototype.hasOwnProperty.call(rows, id)) { COUNTS[id] = rows[id]; }
        }
      })
      .catch(function () { /* leave this batch's em dashes where they are */ });
  }

  /* kiosk.js's loadCounts: one pass over the whole manifest at start, in
   * batches, because "most liked" needs all of them before it is an order. */
  function loadCounts() {
    var ids = [];
    var batches = [];
    var at;
    if (!base() || !ENTRIES.length) { return Promise.resolve(); }
    for (at = 0; at < ENTRIES.length; at += 1) { ids.push(ENTRIES[at].id); }
    for (at = 0; at < ids.length; at += COUNTS_BATCH) {
      batches.push(fetchCounts(ids.slice(at, at + COUNTS_BATCH)));
    }
    return Promise.all(batches).then(function () {
      // "most liked" sorts on numbers that have only just arrived.
      if (state.order === "liked" && state.started) { reorder("liked"); }
      if (state.started) { paintCaption(current()); paintStatus(); }
    });
  }

  /* The view (spec §4.7), which is the entry page's and not the kiosk's: the
   * same body, the same headers, no `source`. A person who stayed ten seconds
   * on a sketch they chose to look at is what the number already means.
   *
   * Fire and forget: a view that does not land is not an error and there is
   * nothing to tell the visitor. */
  function sendView(entry) {
    if (!entry || !base()) { return; }
    fetch(base() + "/view", {
      method: "POST",
      credentials: "include",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ entry_id: entry.id })
    }).catch(function () { /* nobody is told, and nobody needs to be */ });
  }

  function tabHidden() { return document.visibilityState === "hidden"; }

  /* kiosk.js's tick, with the playback timer taken out: nothing on this page
   * advances by itself, so all that is left of it is the dwell that earns a
   * view. Counted in rAF deltas rather than off a wall clock, so a
   * backgrounded tab counts nothing; the flag is set before the request goes
   * out, so no frame can post a second one while the first is in flight. */
  var last = 0;

  function tick(stamp) {
    var delta = (stamp - last) / 1000;
    last = stamp;
    if (state.started && !tabHidden()) { state.dwell += delta; }
    if (!state.viewed && state.dwell >= VIEW_AFTER_S && base()) {
      state.viewed = true;
      sendView(state.stage);
    }
    window.requestAnimationFrame(tick);
  }

  /* ---- settings: the URL, then storage, then the default (spec §4.2) ----- */

  /* kiosk.js's named(). An order nobody defined is not an order. */
  function named(order) {
    var at;
    for (at = 0; at < ORDERS.length; at += 1) {
      if (ORDERS[at].key === order) { return true; }
    }
    return false;
  }

  function orderLabel(key) {
    var at;
    for (at = 0; at < ORDERS.length; at += 1) {
      if (ORDERS[at].key === key) { return ORDERS[at].label; }
    }
    return key;
  }

  function readSettings() {
    var params = new URLSearchParams(window.location.search);
    var saved = null;
    var order;
    var at;
    try {
      saved = JSON.parse(window.localStorage.getItem(SETTINGS_KEY) || "null");
    } catch (err) { saved = null; }
    if (saved && typeof saved === "object" && named(saved.order)) {
      state.order = saved.order;
    }
    // A parameter beats a stored setting, so a link that says what it does
    // comes up the way it says whatever the phone did last.
    order = params.get("order");
    if (named(order)) { state.order = order; }
    // The seat is read from the URL and never from storage: where somebody
    // was last is not a setting, it is a link somebody sent.
    at = Number(params.get("at"));
    state.at = at > 0 ? at : null;
  }

  /* The launch link and the address bar are the same two parameters, built
   * the same way, so the link on the settings sheet is a link to what is on
   * the screen. Both come from fixed vocabularies — an order key and an id —
   * so nothing here needs escaping. */
  function query() {
    var entry = current();
    return "order=" + state.order + "&at=" + (entry ? entry.id : "");
  }

  /* Both halves of §4.2 at once, after every seat and every order change.
   * order=newest is written like any other: a launch link that says what it
   * does is better than one that relies on a default. */
  function persist() {
    try {
      window.localStorage.setItem(SETTINGS_KEY, JSON.stringify({
        v: SETTINGS_VERSION, order: state.order
      }));
    } catch (err) { /* private mode: the URL still carries it */ }
    if (window.history && window.history.replaceState) {
      window.history.replaceState(
        null, "", window.location.pathname + "?" + query() + window.location.hash
      );
    }
  }

  /* ---- the manifest's numbers, ordered as the grid orders its cards ------
   *
   * All seven comparators below are kiosk.js's, unchanged, so the phone and
   * the grid cannot disagree about what "most reviewed" means. */

  function likesOf(entry) {
    var row = COUNTS[entry.id];
    var value = row ? row.likes : 0;
    return typeof value === "number" ? value : 0;
  }

  function stand(entry, population, question) {
    var pop = entry.judgment && entry.judgment[population];
    var cell = pop && pop[question];
    return cell && typeof cell.score === "number" ? cell : null;
  }

  function pairsOf(entry) {
    var total = 0;
    var populations = ["human", "agent"];
    var questions = ["look", "brief"];
    var p;
    var q;
    var cell;
    for (p = 0; p < populations.length; p += 1) {
      for (q = 0; q < questions.length; q += 1) {
        cell = stand(entry, populations[p], questions[q]);
        if (cell && typeof cell.n === "number") { total += cell.n; }
      }
    }
    return total;
  }

  function gapOf(entry) {
    var human = stand(entry, "human", "look");
    var agent = stand(entry, "agent", "look");
    if (!human || !agent) { return -1; }
    if (typeof human.pct !== "number" || typeof agent.pct !== "number") { return -1; }
    return Math.abs(human.pct - agent.pct);
  }

  function byNewest(a, b) {
    var left = a.published_utc || "";
    var right = b.published_utc || "";
    if (left !== right) { return left < right ? 1 : -1; }
    return b.id - a.id;
  }

  function byOldest(a, b) { return -byNewest(a, b); }

  function byLikes(a, b) {
    var diff = likesOf(b) - likesOf(a);
    return diff !== 0 ? diff : byNewest(a, b);
  }

  function byReviewed(a, b) {
    var diff = pairsOf(b) - pairsOf(a);
    return diff !== 0 ? diff : byNewest(a, b);
  }

  function byControversial(a, b) {
    var ga = gapOf(a);
    var gb = gapOf(b);
    if (ga < 0 && gb < 0) { return byNewest(a, b); }
    if (ga < 0) { return 1; }
    if (gb < 0) { return -1; }
    return gb - ga !== 0 ? gb - ga : byNewest(a, b);
  }

  function byConsensus(a, b) {
    var ga = gapOf(a);
    var gb = gapOf(b);
    if (ga < 0 && gb < 0) { return byNewest(a, b); }
    if (ga < 0) { return 1; }
    if (gb < 0) { return -1; }
    return ga - gb !== 0 ? ga - gb : byNewest(a, b);
  }

  /* Fisher-Yates: every order equally likely, which "random" ought to mean. */
  function shuffle(list) {
    var index;
    var pick;
    var held;
    for (index = list.length - 1; index > 0; index -= 1) {
      pick = Math.floor(Math.random() * (index + 1));
      held = list[index];
      list[index] = list[pick];
      list[pick] = held;
    }
    return list;
  }

  /* kiosk.js's sequence(): positions into ENTRIES, in the asked-for order. */
  function sequence(order) {
    var seats = [];
    var at;
    var by = null;
    for (at = 0; at < ENTRIES.length; at += 1) { seats.push(at); }
    if (order === "random") { return shuffle(seats); }
    if (order === "oldest") { by = byOldest; }
    else if (order === "liked") { by = byLikes; }
    else if (order === "reviewed") { by = byReviewed; }
    else if (order === "controversial") { by = byControversial; }
    else if (order === "consensus") { by = byConsensus; }
    else { by = byNewest; }
    seats.sort(function (left, right) { return by(ENTRIES[left], ENTRIES[right]); });
    return seats;
  }

  /* kiosk.js's reshuffled(): a fresh permutation every time a random sequence
   * wraps, and never one that opens on the sketch already on the screen. */
  function reshuffled() {
    var seats = sequence("random");
    var showing = current();
    var pick;
    var held;
    if (showing && seats.length > 1 && ENTRIES[seats[0]].id === showing.id) {
      pick = 1 + Math.floor(Math.random() * (seats.length - 1));
      held = seats[0];
      seats[0] = seats[pick];
      seats[pick] = held;
    }
    return seats;
  }

  function current() {
    if (!state.seq.length) { return null; }
    return ENTRIES[state.seq[state.i]] || null;
  }

  function byId(id) {
    var at;
    for (at = 0; at < ENTRIES.length; at += 1) {
      if (ENTRIES[at].id === Number(id)) { return ENTRIES[at]; }
    }
    return null;
  }

  /* Where ?at=<id> sits in the current sequence. An id the manifest does not
   * have is seat 0 rather than an error: a link to a rejected entry, or to one
   * from before a prune, should still open the gallery. */
  function seatOf(id) {
    var at;
    if (!id) { return 0; }
    for (at = 0; at < state.seq.length; at += 1) {
      if (ENTRIES[state.seq[at]].id === Number(id)) { return at; }
    }
    return 0;
  }

  /* kiosk.js's reorder(): changing the order keeps the sketch on screen on
   * screen, and re-seats it where the new order puts it. */
  function reorder(order) {
    var showing = current();
    var id = showing ? showing.id : null;
    var at;
    state.order = order;
    state.seq = sequence(state.order);
    state.i = 0;
    for (at = 0; at < state.seq.length; at += 1) {
      if (ENTRIES[state.seq[at]].id === id) { state.i = at; break; }
    }
    paintStatus();
    persist();
  }

  /* ---- the frame (spec §4.3), kiosk.js's ---------------------------------- */

  /* The same sandbox as everywhere else in the gallery: scripts yes, and
   * nothing else — no same-origin, no forms, no top navigation, no allow= and
   * no postMessage channel. One iframe in the document at any time: the
   * previous one is removed before the next is appended. */
  function showEntry(entry) {
    if (frame) { frame.remove(); frame = null; }
    state.stage = entry || null;
    // A new frame is a new view to earn, whether it arrived by a swipe or by
    // the judge sheet swapping B onto the stage (§4.7). One place, because two
    // would drift.
    state.viewed = false;
    state.dwell = 0;
    if (!entry) { return; }
    frame = document.createElement("iframe");
    frame.className = "sketch";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.src = ROOT + entry.sketch;
    frame.title = "entry " + entry.id + " running";
    $("stage").appendChild(frame);
    fitFrame();
  }

  /* kiosk.js's fitFrame, with one of its four size modes: fit, and only fit
   * (§1.11). An opaque frame cannot be asked how big its canvas is, so the
   * manifest says: a row with `canvas` gets a frame laid out at exactly that
   * size and then scaled to contain, and one without gets an unscaled frame
   * that fills the stage — a sketch that sized itself to its window is already
   * as big as the screen it was given. No `native`, because on a phone native
   * is smaller than the screen for every canvas in the gallery, and no `fill`,
   * because cropping a work to a phone is not done silently. */
  function fitFrame() {
    var stage = $("stage");
    var entry = state.stage;
    var width;
    var height;
    var room;
    var tall;
    var scale;
    stage.style.removeProperty("--frame-w");
    stage.style.removeProperty("--frame-h");
    stage.style.removeProperty("--frame-sx");
    stage.style.removeProperty("--frame-sy");
    if (!entry || !entry.canvas || entry.canvas.length !== 2) { return; }
    width = Number(entry.canvas[0]);
    height = Number(entry.canvas[1]);
    room = stage.clientWidth;
    tall = stage.clientHeight;
    if (!width || !height || !room || !tall) { return; }
    stage.style.setProperty("--frame-w", width + "px");
    stage.style.setProperty("--frame-h", height + "px");
    scale = Math.min(room / width, tall / height);
    stage.style.setProperty("--frame-sx", String(scale));
    stage.style.setProperty("--frame-sy", String(scale));
  }

  /* ---- the words, kiosk.js's ---------------------------------------------- */

  /* kiosk.js's promptParts, which is lineage.split_prompt in JavaScript:
   * Revise: only counts at the start of its own line. */
  function promptParts(prompt) {
    var parts = String(prompt || "").replace(/^\s+|\s+$/g, "").split(/^Revise:[ \t]*/m);
    var revisions = [];
    var at;
    for (at = 1; at < parts.length; at += 1) {
      revisions.push(parts[at].split(/\s+/).join(" ").replace(/^ | $/g, ""));
    }
    return { root: parts[0].split(/\s+/).join(" ").replace(/^ | $/g, ""), revisions: revisions };
  }

  /* kiosk.js's fmtDate: the clock is read in UTC as well as printed as UTC. */
  function fmtDate(iso) {
    var when = new Date(iso);
    if (isNaN(when.getTime())) { return String(iso); }
    return when.toLocaleDateString(undefined, {
      day: "numeric", month: "short", year: "numeric", timeZone: "UTC"
    }) + " " + when.toISOString().slice(11, 16) + " UTC";
  }

  /* kiosk.js's judgeText: the score on "rather look at it", which is the axis
   * the orders sort on, and how many pairs it rests on. */
  function judgeText(entry, population) {
    var look = stand(entry, population, "look");
    if (!look) { return NO_PAIRS; }
    return look.score.toFixed(2) + " over " + look.n + " pair" + (look.n === 1 ? "" : "s");
  }

  /* kiosk.js's quadText. The quadrant needs both questions, as the entry
   * page's compass does: one coordinate is not a point. */
  function quadText(entry, population) {
    var look = stand(entry, population, "look");
    var brief = stand(entry, population, "brief");
    if (!look || !brief) { return ""; }
    return QUADRANTS[
      (look.pct >= COMPASS_STRONG ? "1" : "0") + (brief.pct >= COMPASS_STRONG ? "1" : "0")
    ];
  }

  /* The one fact the kiosk's caption does not carry: what this sketch has to
   * give a finger, from the gate's own assertions (§2). Nobody should hold a
   * sketch that has nothing to give, and nobody should wonder why a listening
   * sketch is deaf — it cannot reach the microphone inside a sandboxed frame,
   * and the entry page is where it can. */
  function touchFacts(entry) {
    var responds = entry.responds || [];
    var facts = [];
    var at;
    var wants = false;
    var sounds = false;
    for (at = 0; at < responds.length; at += 1) {
      if (responds[at] === "click" || responds[at] === "drag") { wants = true; }
      if (responds[at] === "audio") { sounds = true; }
    }
    if (wants) { facts.push("responds to touch · hold to try"); }
    if (sounds) { facts.push("makes sound · hold to hear it"); }
    if (entry.mic) { facts.push("listens to the microphone · open the entry page to let it"); }
    return facts;
  }

  /* kiosk.js's captionHtml, with the overlay switches taken out: this page has
   * one caption and it always says the same things. */
  function captionHtml(entry) {
    var counts = COUNTS[entry.id] || {};
    var parts = promptParts(entry.prompt);
    var facts = touchFacts(entry);
    var revised;
    var html = "";
    var at;
    html += "<p class=\"prompt\"><span class=\"num\">#" + esc(entry.id) + "</span>" +
      esc(parts.root) + "</p>";
    if (parts.revisions.length) {
      revised = parts.revisions.length === 1
        ? "revised once" : "revised " + parts.revisions.length + " times";
      html += "<p class=\"revisions\"><b>" + revised + ", latest:</b> " +
        esc(parts.revisions[parts.revisions.length - 1]) + "</p>";
    }
    html += "<p class=\"authors\">Prompted by <span>" + esc(entry.submitted_by) +
      "</span> · planned by <span>" + esc(entry.planner) +
      "</span> · written by <span>" + esc(entry.executor) + "</span> under the " +
      esc(entry.rules_file) + " rules" +
      (entry.attempts > 1 ? ", gate passed on attempt " + esc(entry.attempts) : "") + "</p>";
    html += "<ul class=\"facts\"><li class=\"gen\"><b>generation " +
      esc(entry.generation) + "</b></li>";
    html += "<li><b>" + num(counts.views) + "</b> views</li>";
    html += "<li><b>" + num(counts.likes) + "</b> likes</li>";
    for (at = 0; at < facts.length; at += 1) {
      html += "<li class=\"touch\">" + esc(facts[at]) + "</li>";
    }
    html += "</ul><p class=\"more\">tap here for the words</p>";
    return html;
  }

  function paintCaption(entry) {
    if (!entry) { return; }
    $("caption").innerHTML = captionHtml(entry);
  }

  function paintLiked() {
    var entry = current();
    if (entry && state.liked[entry.id]) { document.body.classList.add("liked"); }
    else { document.body.classList.remove("liked"); }
  }

  function paintStatus() {
    $("status").innerHTML = "<b>" + (state.i + 1) + "</b> of " +
      num(ENTRIES.length) + " · " + esc(state.order);
    // With nowhere to post a like, the heart is a control that cannot act.
    // setAttribute, not .hidden: the heart is an <svg>, and SVGElement has no
    // hidden property to reflect — the assignment would be a silent expando.
    if (base()) { $("heart").removeAttribute("hidden"); } else { $("heart").setAttribute("hidden", ""); }
    paintLiked();
  }

  /* ---- the toast ---------------------------------------------------------- */

  var toastTimer = null;

  function toast(text) {
    var el = $("toast");
    el.textContent = text;
    el.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () { el.classList.remove("show"); }, TOAST_MS);
  }

  /* ---- the sheets (spec §4.5) --------------------------------------------- */

  function anySheet() { return !!document.querySelector(".sheet.open"); }

  /* Closing is where a pair of judgments ends, so every way out of the judge
   * sheet — the ✕, the dim, Esc, a drag down, "keep swiping" — comes through
   * one function and puts A back on the stage. */
  function closeSheets() {
    Array.prototype.forEach.call(document.querySelectorAll(".sheet"), function (sheet) {
      sheet.classList.remove("open");
      sheet.style.transform = "";
    });
    $("dim").classList.remove("open");
    if (state.judging) { endJudging(); }
  }

  /* Only one sheet at a time, so opening one closes whatever was open —
   * including a pair being judged. startJudging() therefore opens the sheet
   * *before* it sets state.judging, or this call would throw away the pair it
   * was about to show. */
  function openSheet(id) {
    closeSheets();
    $(id).classList.add("open");
    $("dim").classList.add("open");
    // The judge sheet is half height so the stage stays visible above it and
    // the pair can be watched in turn; a scrim over both sides defeats that.
    $("dim").style.background = id === "sheet-judge" ? "transparent" : "";
  }

  function wireSheets() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-close]"), function (button) {
      button.addEventListener("click", function () { closeSheets(); });
    });
    $("dim").addEventListener("click", function () { closeSheets(); });
    // A sheet dragged down past 90 px closes, which is the gesture a phone has
    // taught everyone a bottom sheet answers. Only from the top of its scroll,
    // so reading the provenance does not close the words.
    Array.prototype.forEach.call(document.querySelectorAll(".sheet"), function (sheet) {
      var y0 = null;
      sheet.addEventListener("pointerdown", function (ev) {
        if (!sheet.scrollTop) { y0 = ev.clientY; }
      });
      sheet.addEventListener("pointermove", function (ev) {
        var dy;
        if (y0 === null) { return; }
        dy = Math.max(0, ev.clientY - y0);
        if (dy > 8) {
          sheet.style.transition = "none";
          sheet.style.transform = "translateY(" + dy + "px)";
        }
      });
      function end(ev) {
        var dy;
        if (y0 === null) { return; }
        dy = ev.clientY - y0;
        y0 = null;
        sheet.style.transition = "";
        if (dy > SHEET_CLOSE) { closeSheets(); } else { sheet.style.transform = ""; }
      }
      sheet.addEventListener("pointerup", end);
      sheet.addEventListener("pointercancel", end);
    });
  }

  /* ---- the words sheet ---------------------------------------------------- */

  function provenance(entry, meta) {
    var line = meta.lineage || {};
    var canvas = entry.canvas ? entry.canvas.join("×") : "the window";
    return [
      ["prompted by", meta.submitted_by],
      ["planned by", meta.planner],
      ["written by", meta.executor],
      ["rules", meta.rules_file],
      ["generation", String(entry.generation) + (line.parent_entry_id
        ? ", revised from #" + line.parent_entry_id +
          (line.critique_by ? " after a critique by " + line.critique_by : "")
        : ", a root")],
      ["attempts", meta.attempts],
      ["created", fmtDate(meta.created_utc)],
      ["tokens", num(meta.prompt_tokens) + " prompt + " + num(meta.completion_tokens) + " completion"],
      ["written in", secs(meta.wall_s)],
      ["canvas", canvas],
      ["licence", meta.licence]
    ];
  }

  function paintInfo(entry, meta) {
    var parts = promptParts(entry.prompt);
    var rows = [];
    var at;
    var fields;
    $("i-title").textContent = "#" + entry.id + " · " + parts.root;
    $("i-sub").textContent = parts.revisions.length
      ? (parts.revisions.length === 1 ? "Revised once" : "Revised " + parts.revisions.length + " times") + ", latest: " +
        parts.revisions[parts.revisions.length - 1]
      : "A root prompt, unrevised.";
    $("i-brief").textContent = (meta && meta.brief) || "";
    $("i-by").textContent = entry.executor || "";
    $("i-statement").textContent = (meta && meta.statement) || "";
    $("i-jh").textContent = judgeText(entry, "human");
    $("i-qh").textContent = quadText(entry, "human");
    $("i-ja").textContent = judgeText(entry, "agent");
    $("i-qa").textContent = quadText(entry, "agent");
    fields = meta ? provenance(entry, meta) : [];
    for (at = 0; at < fields.length; at += 1) {
      rows.push("<dt>" + esc(fields[at][0]) + "</dt><dd>" + esc(fields[at][1]) + "</dd>");
    }
    $("i-fields").innerHTML = rows.join("");
    $("i-entry").href = entry.url;
    $("i-source").href = entry.url + "sketch/sketch.js";
  }

  function openInfo() {
    var entry = current();
    if (!entry) { return; }
    openSheet("sheet-info");
    // Painted twice: once with what the manifest already has, so the sheet is
    // never empty while a phone waits on a network, and once when meta.json
    // lands with the brief and the statement in it.
    paintInfo(entry, null);
    loadMeta(entry).then(function (meta) {
      if (current() === entry) { paintInfo(entry, meta); }
    });
  }

  /* ---- the judge sheet ---------------------------------------------------- */

  /* gallery.js's pickPair, restated over the manifest: an offered pair from
   * pairs.json that contains A, at random among those, else a published entry
   * at random that is not A. `not` is the partner already on the sheet, which
   * "judge another pair" excludes so that the button changes something. */
  function pickPartner(a, not) {
    var offered = [];
    var pool = [];
    var at;
    var pair;
    var other;
    for (at = 0; at < PAIRS.length; at += 1) {
      pair = PAIRS[at];
      other = null;
      if (Number(pair.a) === a.id) { other = byId(pair.b); }
      else if (Number(pair.b) === a.id) { other = byId(pair.a); }
      if (other && other.id !== a.id) { offered.push(other); }
    }
    if (not) {
      offered = offered.filter(function (one) { return one.id !== not.id; });
    }
    if (offered.length) { return oneOf(offered); }
    for (at = 0; at < ENTRIES.length; at += 1) {
      if (ENTRIES[at].id !== a.id && (!not || ENTRIES[at].id !== not.id)) {
        pool.push(ENTRIES[at]);
      }
    }
    // With two entries in the gallery there is no third to move on to, so the
    // pair stands rather than the button doing nothing at all.
    if (!pool.length && not) { return not; }
    return pool.length ? oneOf(pool) : null;
  }

  /* gallery.js's pairKey. A verdict is stored against the pair in (low, high)
   * order; this sheet may be showing it the other way round. */
  function pairKey(a, b) {
    var low = Math.min(Number(a), Number(b));
    var high = Math.max(Number(a), Number(b));
    return low + "-" + high;
  }

  function paintJudgeStatus() {
    var host = $("j-status");
    if (!host) { return; }
    if (state.username) {
      host.innerHTML = "Signed in as " + esc(state.username) +
        ": your answers are recorded and counted.";
      return;
    }
    // The offer, and only an offer: a vote while signed out is noted on the
    // sheet, as the compare page notes one, and the sign-in sheet opens only
    // if the visitor asks for it (§4.8).
    host.innerHTML = "Not signed in: your answers are noted here only. " +
      "<button type=\"button\" id=\"j-signin\">Sign in with GitHub</button> to have them counted.";
  }

  function paintTiles() {
    var judging = state.judging;
    if (!judging) { return; }
    Array.prototype.forEach.call(document.querySelectorAll(".tile"), function (tile) {
      var on = tile.getAttribute("data-side") === judging.playing;
      var small = tile.querySelector("small");
      tile.setAttribute("aria-pressed", on ? "true" : "false");
      if (small) { small.textContent = on ? "playing" : "tap to play"; }
    });
  }

  /* One frame, swapped, and the caption stays hidden throughout: the pair is
   * judged by watching both in turn, which is the whole reason the sheet is
   * half height. The swap is a seat-like call, so ten seconds on B counts for
   * B and not for A (§4.7). */
  function playSide(side) {
    var judging = state.judging;
    if (!judging) { return; }
    judging.playing = side;
    paintTiles();
    showEntry(side === "A" ? judging.a : judging.b);
  }

  function startJudging() {
    var a = current();
    if (!a || state.judging || anySheet() || state.touching) { return; }
    return loadPairs().then(function () {
      var b = pickPartner(a, null);
      if (!b) { toast("there is nothing to judge this against yet"); return; }
      openJudging(a, b);
    });
  }

  function openJudging(a, b) {
    // The sheet is opened before the pair is recorded: openSheet closes
    // whatever was open, and closing ends any judging in progress.
    openSheet("sheet-judge");
    state.judging = { a: a, b: b, playing: "A", answered: {} };
    $("j-brief-a").textContent = "";
    $("j-brief-b").textContent = "";
    // Two tiles, two briefs, and nothing else: no prompt, no authors, no
    // counts, no verdicts (§4.8). B's are the loudest anchor the sheet could
    // hand somebody who has not answered yet.
    loadMeta(a).then(function (meta) {
      if (state.judging && state.judging.a === a) {
        $("j-brief-a").textContent = (meta && meta.brief) || "";
      }
    });
    loadMeta(b).then(function (meta) {
      if (state.judging && state.judging.b === b) {
        $("j-brief-b").textContent = (meta && meta.brief) || "";
      }
    });
    Array.prototype.forEach.call(document.querySelectorAll(".choices button"), function (button) {
      button.setAttribute("aria-pressed", "false");
    });
    Array.prototype.forEach.call(document.querySelectorAll(".answered"), function (line) {
      line.textContent = "";
    });
    $("j-reveal").hidden = true;
    $("j-agents").innerHTML = "";
    $("j-both").innerHTML = "";
    paintJudgeStatus();
    playSide("A");
    // The words go quiet behind the sheet: A's caption is above the stage and
    // the stage is what is being watched.
    document.body.classList.add("quiet");
  }

  function endJudging() {
    var judging = state.judging;
    state.judging = null;
    // The words come back if they were up before the sheet: the sheet
    // borrowed the quiet, it did not decide it.
    paintQuiet();
    if (judging && judging.playing !== "A") { showEntry(judging.a); }
  }

  function vote(question, choice) {
    var judging = state.judging;
    var block = document.querySelector("[data-question=\"" + question + "\"]");
    var line = block ? block.querySelector(".answered") : null;
    if (!judging) { return; }
    judging.answered[question] = choice;
    if (block) {
      Array.prototype.forEach.call(block.querySelectorAll("[data-vote]"), function (button) {
        button.setAttribute(
          "aria-pressed", button.getAttribute("data-vote") === choice ? "true" : "false"
        );
      });
    }
    if (line) {
      line.textContent = (base() && state.username ? "recorded: " : "noted here only: ") + choice;
    }
    if (base()) {
      fetch(base() + "/vote", {
        method: "POST",
        credentials: "include",
        headers: authHeaders({ "Content-Type": "application/json" }),
        // Exactly the payload writepath/worker.js:routeVote destructures.
        body: JSON.stringify({
          entry_a: judging.a.id,
          entry_b: judging.b.id,
          question: question,
          choice: choice
        })
      }).then(function (response) {
        if (refused(response) && line) {
          line.textContent = "noted here only: " + choice +
            " — sign in with GitHub to record it";
        }
      }).catch(function () {
        if (line) { line.textContent = "could not record " + choice + "; try again"; }
      });
    }
    if (judging.answered.brief && judging.answered.look) { reveal(); }
  }

  /* After both answers, and never before: being shown one first would anchor
   * the other, which is the compare page's rule and this sheet's. */
  function reveal() {
    var judging = state.judging;
    var rows;
    var flipped;
    var html = [];
    if (!judging) { return; }
    rows = AGENTS[pairKey(judging.a.id, judging.b.id)] || [];
    flipped = Number(judging.a.id) > Number(judging.b.id);
    if (!rows.length) {
      html.push("<p class=\"verdict\">The agents have not judged this pair</p>");
    } else {
      rows.forEach(function (row) {
        var choice = row.choice;
        if (flipped && choice === "A") { choice = "B"; }
        else if (flipped && choice === "B") { choice = "A"; }
        html.push("<p class=\"verdict\"><b>" + esc(row.judge) + "</b> · " +
          (row.question === "brief" ? "closer to its brief" : "rather look at") +
          ": " + esc(choice) + "</p>");
      });
    }
    $("j-agents").innerHTML = html.join("");
    $("j-both").innerHTML = "A is <a href=\"" + esc(judging.a.url) + "\">#" +
      esc(judging.a.id) + "</a> · B is <a href=\"" + esc(judging.b.url) + "\">#" +
      esc(judging.b.id) + "</a>";
    $("j-reveal").hidden = false;
  }

  function wireJudge() {
    Array.prototype.forEach.call(document.querySelectorAll(".tile"), function (tile) {
      tile.addEventListener("click", function () {
        playSide(tile.getAttribute("data-side"));
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".question"), function (block) {
      var question = block.getAttribute("data-question");
      Array.prototype.forEach.call(block.querySelectorAll("[data-vote]"), function (button) {
        button.addEventListener("click", function () {
          vote(question, button.getAttribute("data-vote"));
        });
      });
    });
    $("j-status").addEventListener("click", function (event) {
      var target = event && event.target;
      if (target && target.closest && target.closest("#j-signin")) {
        askSignIn("Sign in to record it");
      }
    });
    // A new B, A unchanged: the sketch just watched is still the one being
    // judged, and the partner is the thing that changes.
    $("j-another").addEventListener("click", function () {
      var judging = state.judging;
      var a;
      var b;
      if (!judging) { return; }
      a = judging.a;
      b = pickPartner(a, judging.b);
      if (!b) { return; }
      if (judging.playing !== "A") { showEntry(a); }
      openJudging(a, b);
    });
    $("j-back").addEventListener("click", function () { closeSheets(); });
  }

  /* ---- the settings sheet -------------------------------------------------- */

  function paintSettings() {
    var rows = [];
    var at;
    var order;
    if (!$("s-orders")) { return; }
    $("s-place").textContent = "Sketch " + (state.i + 1) + " of " + num(ENTRIES.length);
    for (at = 0; at < ORDERS.length; at += 1) {
      order = ORDERS[at];
      rows.push("<li><button type=\"button\" data-order=\"" + order.key +
        "\" aria-current=\"" + (order.key === state.order) + "\"><span>" +
        esc(order.label) + "</span><span class=\"state\">" +
        (order.key === state.order ? "current" : "") + "</span></button></li>");
    }
    $("s-orders").innerHTML = rows.join("");
    $("s-session").innerHTML = state.username
      ? "<span>signed in: <b>" + esc(state.username) +
        "</b></span><button type=\"button\" id=\"s-out\">sign out</button>"
      : "<span>not signed in</span><button type=\"button\" id=\"s-in\">sign in with GitHub</button>";
    $("s-launch").textContent = "swipe.html?" + query();
  }

  function signOut() {
    var headers = authHeaders();
    dropToken();
    paintSession(null);
    toast("signed out");
    if (base()) {
      fetch(base() + "/logout", { credentials: "include", headers: headers })
        .catch(function () { /* the copy that mattered is already gone */ });
    }
  }

  function wireSettings() {
    $("status").addEventListener("click", function () {
      openSheet("sheet-settings");
      paintSettings();
    });
    $("s-orders").addEventListener("click", function (event) {
      var target = event && event.target;
      var button = target && target.closest ? target.closest("[data-order]") : null;
      if (!button) { return; }
      // The sketch on screen stays on screen and is re-seated where the new
      // order puts it.
      reorder(button.getAttribute("data-order"));
      paintSettings();
    });
    $("s-session").addEventListener("click", function (event) {
      var target = event && event.target;
      if (!target) { return; }
      if (target.id === "s-out") { signOut(); }
      if (target.id === "s-in") { askSignIn("Sign in with GitHub"); }
    });
  }

  /* ---- the sign-in sheet, and coming back (spec §5) ------------------------ */

  /* Opened by a like or a vote while signed out, and never otherwise: a page
   * that asks for a login before it has shown anything is a page people
   * leave. */
  function askSignIn(title) {
    openSheet("sheet-signin");
    $("si-title").textContent = title;
  }

  /* The note the front page reads once, immediately before leaving for the
   * Worker's /login and nowhere else. The Worker's /callback returns to the
   * gallery's front page with the token in the fragment and only there;
   * gallery.js, having just claimed one, reads this and sends the visitor
   * straight back to the sketch they were looking at (§5). */
  function wireSignIn() {
    $("si-go").addEventListener("click", function () {
      if (!base()) { closeSheets(); toast(NO_WRITE_PATH); return; }
      try {
        window.localStorage.setItem(RETURN_KEY, "swipe.html?" + query());
      } catch (err) { /* private mode: the sign-in still works, the seat is lost */ }
      window.location.href = base() + "/login";
    });
  }

  /* ---- a like (spec §4.6) -------------------------------------------------- */

  function burst() {
    var cue = $("cue-like");
    cue.classList.remove("burst");
    cue.classList.add("burst");
    window.setTimeout(function () {
      cue.classList.remove("burst");
      cue.style.opacity = "";
    }, BURST_MS);
  }

  function like() {
    var entry = current();
    var wanted;
    if (!entry) { return; }
    if (!base()) { toast(NO_WRITE_PATH); return; }
    if (!state.username) { askSignIn("Sign in to like it"); return; }
    wanted = !state.liked[entry.id];
    // Optimistic: the heart fills and the caption's count moves by one before
    // the answer comes back, and both are undone if it does not.
    state.liked[entry.id] = wanted;
    nudgeLikes(entry, wanted ? 1 : -1);
    paintCaption(entry);
    paintLiked();
    if (wanted) { burst(); }
    fetch(base() + "/like", {
      method: "POST",
      credentials: "include",
      headers: authHeaders({ "Content-Type": "application/json" }),
      // worker.js routeLike: { entry_id, on } — on is the state we are asking for
      body: JSON.stringify({ entry_id: entry.id, on: wanted })
    })
      .then(function (response) {
        if (refused(response)) {
          undoLike(entry, wanted);
          askSignIn("Sign in to like it");
          return null;
        }
        return response.json();
      })
      .then(function (data) {
        if (!data) { return; }
        state.liked[entry.id] = !!data.on;
        paintLiked();
        toast(data.on ? "liked" : "unliked");
        // The count the Worker now holds, rather than the one this page
        // guessed: a like from another phone landed in between often enough.
        fetchCounts([entry.id]).then(function () {
          if (current() === entry) { paintCaption(entry); }
        });
      })
      .catch(function () { undoLike(entry, wanted); });
  }

  function undoLike(entry, wanted) {
    state.liked[entry.id] = !wanted;
    nudgeLikes(entry, wanted ? -1 : 1);
    paintCaption(entry);
    paintLiked();
  }

  function nudgeLikes(entry, by) {
    var row = COUNTS[entry.id];
    if (!row || typeof row.likes !== "number") { return; }
    row.likes = Math.max(0, row.likes + by);
  }

  /* ---- the seat, and moving between seats ---------------------------------- */

  function seat(at) {
    var len = state.seq.length;
    var entry;
    if (!len) { return; }
    state.i = ((at % len) + len) % len;
    entry = current();
    showEntry(entry);
    paintCaption(entry);
    paintStatus();
    persist();
  }

  function settle() {
    var stage = $("stage");
    stage.className = "stage snap";
    stage.style.transform = "";
  }

  /* Nothing on this page advances by itself: every call to this one comes
   * from a finger or from one of the six courtesy keys. */
  function go(dir) {
    var len = state.seq.length;
    var stage = $("stage");
    var to;
    var out;
    if (!len || state.judging || anySheet() || state.touching) { return; }
    to = state.i + dir;
    // reshuffled() reads the sketch on screen, so it runs before the sequence
    // it is replacing is thrown away.
    if (state.order === "random" && (to >= len || to < 0)) { state.seq = reshuffled(); }
    out = dir > 0 ? -110 : 110;
    stage.className = "stage leave";
    stage.style.transform = "translateY(" + out + "%)";
    window.setTimeout(function () {
      seat(to);
      // Put it on the far edge with no transition, then let it come in.
      stage.className = "stage";
      stage.style.transform = "translateY(" + (-out) + "%)";
      // Reading a layout property is what makes the two writes two frames in a
      // browser; in node it is a property that is not there, and harmless.
      void stage.offsetWidth;
      stage.className = "stage snap";
      stage.style.transform = "";
    }, LEAVE_MS);
  }

  /* ---- the words, on and off, and the touch -------------------------------- */

  function paintQuiet() {
    if (state.words) { document.body.classList.remove("quiet"); }
    else { document.body.classList.add("quiet"); }
  }

  /* Whether a point is inside the caption's box. getBoundingClientRect is
   * read at the tap, not cached: the caption's height changes with every
   * seat. A hidden caption (quiet) has no box worth honouring, and the
   * caller checks that first. */
  function onCaption(x, y) {
    var box = $("caption").getBoundingClientRect();
    return x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
  }

  function toggleWords() {
    state.words = !state.words;
    paintQuiet();
  }

  /* The shield lifts and the sketch has the touch. This is also the gesture
   * the frame needs before it will make sound, which the kiosk could never
   * deliver. */
  function handTouch() {
    state.touching = true;
    document.body.classList.add("touching");
    if (window.navigator && window.navigator.vibrate) {
      try { window.navigator.vibrate(12); } catch (err) { /* not everywhere */ }
    }
  }

  function giveBack() {
    state.touching = false;
    document.body.classList.remove("touching");
  }

  /* ---- the gesture engine (spec §4.4) -------------------------------------- */

  /* Pointer events on the shield, with setPointerCapture on pointerdown so a
   * drag that leaves the element still ends. Six gestures and no more; the
   * constants at the top of this file are the spec. */
  var gesture = null;

  function onDown(event) {
    var shield = $("shield");
    if (!state.started || anySheet() || state.touching) { return; }
    // One finger at a time: a second pointerdown must not replace a gesture
    // whose hold timer is still live, or the timer reads the new press.
    if (gesture) { return; }
    if (shield.setPointerCapture) { shield.setPointerCapture(event.pointerId); }
    gesture = {
      x: event.clientX, y: event.clientY, t: now(),
      axis: null, dx: 0, dy: 0, id: event.pointerId, hold: 0
    };
    // A hold that never moved is never a tap: this clears the gesture before
    // the release can read it.
    gesture.hold = window.setTimeout(function () {
      if (gesture && !gesture.axis) { gesture = null; handTouch(); }
    }, HOLD_MS);
    $("stage").className = "stage";
  }

  function onMove(event) {
    var stage = $("stage");
    var ax;
    var ay;
    var reach;
    if (!gesture || event.pointerId !== gesture.id) { return; }
    gesture.dx = event.clientX - gesture.x;
    gesture.dy = event.clientY - gesture.y;
    ax = Math.abs(gesture.dx);
    ay = Math.abs(gesture.dy);
    if (!gesture.axis && (ax > AXIS_LOCK || ay > AXIS_LOCK)) {
      gesture.axis = ax > ay ? "x" : "y";
      window.clearTimeout(gesture.hold);
    }
    if (gesture.axis === "y") {
      stage.style.transform = "translateY(" + (gesture.dy * FOLLOW_V) + "px)";
    }
    if (gesture.axis === "x") {
      stage.style.transform = "translateX(" + (gesture.dx * FOLLOW_H) + "px)";
      reach = Math.min(1, ax / COMMIT_H);
      $("cue-like").style.opacity = gesture.dx > 0 ? reach : 0;
      $("cue-judge").style.opacity = gesture.dx < 0 ? reach : 0;
      $("cue-like").style.transform = "translateY(-50%) scale(" + (0.85 + 0.15 * reach) + ")";
      $("cue-judge").style.transform = "translateY(-50%) scale(" + (0.85 + 0.15 * reach) + ")";
    }
  }

  function abandon(event) {
    if (!gesture || event.pointerId !== gesture.id) { return; }
    window.clearTimeout(gesture.hold);
    gesture = null;
    $("cue-like").style.opacity = "";
    $("cue-judge").style.opacity = "";
    $("cue-like").style.transform = "";
    $("cue-judge").style.transform = "";
    settle();
  }

  function release(event) {
    var shield = $("shield");
    var done;
    var dt;
    var vy;
    if (!gesture || event.pointerId !== gesture.id) { return; }
    done = gesture;
    gesture = null;
    window.clearTimeout(done.hold);
    $("cue-like").style.opacity = "";
    $("cue-judge").style.opacity = "";
    $("cue-like").style.transform = "";
    $("cue-judge").style.transform = "";
    dt = now() - done.t;
    if (!done.axis) {
      settle();
      if (Math.abs(done.dx) < TAP_MOVE && Math.abs(done.dy) < TAP_MOVE && dt < TAP_MS) {
        // The caption sits under the shield, so the shield sees every tap and
        // routes the ones that land on the words: open all of them rather
        // than hide the few that are up. Under, not over, because a thumb
        // starts most swipes in the bottom third of a phone, which is exactly
        // where the caption is, and a caption above the shield ate them.
        if (state.words && !state.touching && onCaption(done.x, done.y)) { openInfo(); }
        else { toggleWords(); }
      }
      return;
    }
    if (done.axis === "y") {
      // Either far enough or fast enough: a flick is a swipe, and a phone has
      // taught every thumb that it is.
      vy = Math.abs(done.dy) / Math.max(1, dt);
      if (Math.abs(done.dy) > COMMIT_V || vy > FLICK) { go(done.dy < 0 ? 1 : -1); }
      else { settle(); }
      return;
    }
    // Horizontal: the stage snaps back either way, and the verb happens over
    // the sketch rather than taking it off the screen.
    settle();
    if (done.dx > COMMIT_H) { like(); }
    else if (done.dx < -COMMIT_H) { startJudging(); }
  }

  function wireGestures() {
    var shield = $("shield");
    shield.addEventListener("pointerdown", onDown);
    shield.addEventListener("pointermove", onMove);
    shield.addEventListener("pointerup", release);
    // A cancel is the browser taking the gesture — an edge swipe, the
    // notification shade, a second finger — and is never a decision: the
    // stage settles and nothing acts, however far the finger had gone.
    shield.addEventListener("pointercancel", abandon);
    // The caption is under the shield and takes no pointer events, so this
    // never fires from a finger; it is kept for a keyboard or an assistive
    // click on the element itself. The finger's path is onCaption() above.
    $("caption").addEventListener("click", function () {
      if (state.words && !state.touching) { openInfo(); }
    });
    $("touching").addEventListener("click", giveBack);
  }

  /* ---- the courtesy keys ---------------------------------------------------
   *
   * Desktop courtesy, not a second interface: the same six things a thumb
   * does. A modifier chord is a browser shortcut and stays one. */
  function onKey(event) {
    var key = event.key;
    if (!state.started || event.metaKey || event.ctrlKey || event.altKey) { return; }
    if (event.target && /input|textarea/i.test(event.target.tagName || "")) { return; }
    if (key === "Escape") {
      closeSheets();
      if (state.touching) { giveBack(); }
      return;
    }
    if (anySheet() || state.touching) { return; }
    if (key === "ArrowUp") { go(1); }
    else if (key === "ArrowDown") { go(-1); }
    else if (key === "l" || key === "L") { like(); }
    else if (key === "j" || key === "J") { startJudging(); }
    else if (key === "i" || key === "I") { toggleWords(); }
    else { return; }
    if (event.preventDefault) { event.preventDefault(); }
  }

  /* ---- start ---------------------------------------------------------------
   *
   * kiosk.js's discipline: the card waits for the manifest rather than the
   * other way round, because taking the tap first and fetching afterwards is
   * how a phone on a slow network ends up holding a black screen that answers
   * no gesture. */

  function waitToStart() {
    $("go").disabled = true;
    $("go").textContent = "loading…";
  }

  function offerStart() {
    $("go").disabled = false;
    $("go").textContent = "Start";
  }

  function cannotStart() {
    var note = document.querySelector(".welcome .note");
    $("go").disabled = true;
    $("go").textContent = "Start";
    if (note) { note.textContent = CANNOT_LOAD; }
  }

  function start() {
    if (state.started || !ENTRIES.length) { return; }
    state.started = true;
    $("welcome").hidden = true;
    document.body.classList.add("playing");
    state.seq = sequence(state.order);
    seat(seatOf(state.at));
    paintSettings();
    last = now();
    window.requestAnimationFrame(tick);
  }

  ready(function () {
    // First, before any request: the token /callback handed back in the
    // fragment, stored for this origin and stripped from the address bar.
    claimTokenFromHash();
    readSettings();
    wireSheets();
    wireJudge();
    wireSettings();
    wireSignIn();
    wireGestures();
    document.addEventListener("keydown", onKey);
    // Turning the phone refits; so does a keyboard appearing and going away.
    window.addEventListener("resize", function () { window.setTimeout(fitFrame, 60); });
    window.addEventListener("orientationchange", function () {
      window.setTimeout(fitFrame, 60);
    });
    $("go").addEventListener("click", start);
    waitToStart();
    paintSettings();
    // Both files are asked for on load, not on the tap: the card is the wait,
    // so that the tap is not.
    Promise.all([loadManifest(), loadConfig()]).then(function () {
      if (!ENTRIES.length) { cannotStart(); return; }
      offerStart();
      loadCounts();
      loadMe();
    }).catch(function () {
      cannotStart();
    });
  });
})();
