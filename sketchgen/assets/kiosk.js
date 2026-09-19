/* kiosk.js — the projector page, kiosk.html, and nothing else.
 *
 * It plays the published gallery one sketch after another, full screen, in a
 * room. Any key brings up a menu of key commands; the first press only opens
 * the menu, so a bumped keyboard never skips a sketch. The layout, the copy,
 * the key bindings and the timings are the mockup's
 * (docs/plans/kiosk-mockup/kiosk.html); this file is that script ported onto
 * the real gallery's data and the real gallery's sandbox.
 *
 * What it reads, and that is the whole list:
 *
 *   kiosk.json      the manifest render_index writes: every published entry's
 *                   prompt, brief, statement, judgment, provenance and canvas
 *   config.json     the write path's base URL, and nothing else
 *   <base>/counts   views and likes, a plain GET, at start and every ten minutes
 *   <root><source>  one sketch's source, once, and only while the code overlay
 *                   is on
 *   <root><sketch>  in a sandboxed iframe, which is where the sketch runs
 *
 * The QR code beside each sketch is not on that list and never will be: it is
 * an <img src> the browser loads and caches, pointing at a file render_index
 * already wrote (qr.md §1.9). This file encodes nothing, fetches no SVG and
 * adds no parameter to any URL. It counts a view of the sketch on the stage,
 * below, and never a scan of the code: a camera is not a page load and this
 * page cannot see one.
 *
 * The one thing it writes (docs/plans/kiosk-views.md), and the only one:
 *
 *   <base>/view     one view of one entry, once that entry has been on the
 *                   stage, playing, for ten seconds
 *
 * That endpoint de-duplicates a signed-in viewer and nothing else — an
 * anonymous view is counted exactly as it arrives, on purpose, because
 * de-duplicating one would mean keeping an IP or a fingerprint. This page
 * signs nobody in, so every request it makes counts, and the three conditions
 * below are the only restraint there is:
 *
 *   1. ten seconds of un-paused time on one seat, so a held arrow key skips
 *      without counting, and a throttled tab counts nothing at all: the timer
 *      is rAF deltas, and a browser stops handing those to a hidden page;
 *   2. once per seat, ever — a boolean set before the request goes out, so no
 *      frame can post a second one while the first is still in flight;
 *   3. nothing after eight hours of playing with no key and no mouse. The
 *      sketches keep playing; a room nobody is in stops being an audience.
 *
 * Any of that can be turned off without a deploy: kiosk_views in config.json
 * for the gallery, ?views=0 for one projector.
 *
 * What it must never do (spec §4.5), and what the tests hold it to: write
 * anything but that one view; read document.cookie; present an identity of any
 * kind, because /counts is a public read, /view takes an anonymous one, and
 * this page never signs anybody in; evaluate sketch source; touch a
 * localStorage key other than sketchgen-kiosk; or hold more than one iframe at
 * a time.
 *
 * This is the only script the page loads. gallery.js is not on it: its ready()
 * asks /me for the signed-in viewer, and a projector in a lobby has no viewer
 * to name and no business sending a credentialed request all day. So base()
 * and loadConfig() are copied from it — twenty duplicated lines, rather than
 * a shared global or a second script whose other five jobs all write.
 * window.SKETCHGEN_ROOT, set inline by the template, is the only thing this
 * file reads that it did not put there itself.
 *
 * Vanilla, ES5, no build step, no framework, no web font, nothing from a CDN.
 */

(function () {
  "use strict";

  var ROOT = window.SKETCHGEN_ROOT || "./";

  /* The one localStorage key this page is allowed to touch (spec §4.5).
   * gallery.js keeps its session under its own key on the same origin, and
   * the kiosk has no business reading it: it never signs anybody in. */
  var STORAGE_KEY = "sketchgen-kiosk";

  var FADE_MS = 420;          /* the fade to black between sketches */
  var MENU_MS = 8000;         /* how long the menu waits for another key */
  var IDLE_MS = 3000;         /* how long before the cursor goes away */
  var START_MENU_MS = 600;    /* the one time the menu opens by itself */
  var CODE_HOLD_MS = 4000;    /* read the top of the file before it moves */
  var CODE_SPEED = 18;        /* pixels a second, a reading pace */
  var COUNTS_EVERY_MS = 600000;

  /* How long a sketch has to have been on the stage before it is a view, and
   * how long a room can go without a key or a mouse before it stops being an
   * audience. Both are counted in playing seconds off the same rAF deltas the
   * playback timer uses, not off a wall clock: a projector whose tab the
   * browser has backgrounded is neither playing nor being watched. */
  var VIEW_AFTER_S = 10;
  var VIEW_STOP_S = 8 * 60 * 60;

  /* The write path answers one statement per ask and its database binds at
   * most a hundred parameters, which is the number gallery.js batches on. */
  var COUNTS_BATCH = 100;

  /* gallery.py's _COMPASS_STRONG: above this percentile a population is on the
   * strong half of an axis, and the quadrant words are drawn at the same 0.5,
   * so the phrase here and the square on the entry page cannot disagree. */
  var COMPASS_STRONG = 0.5;

  /* gallery.py's _QUADRANT_WORDS, keyed "<look strong><brief strong>". */
  var QUADRANTS = {
    "11": "looks good, on brief",
    "10": "looks good, misses the brief",
    "01": "on brief, not much to look at",
    "00": "neither"
  };

  /* gallery.py's NO_PAIRS. A score nobody voted on is not a low score. */
  var NO_PAIRS = "no pairs yet";

  /* Said on the start card, in place of its last line, when there is no
   * manifest to play. One sentence, because a projector is read at a
   * distance by somebody who did not set it up. */
  var CANNOT_LOAD = "Could not load the gallery's list of sketches.";

  /* The grid's seven orders, in the grid's order: the 1-7 keys index this
   * list and S walks it. */
  var ORDERS = [
    { key: "newest", label: "newest first" },
    { key: "oldest", label: "oldest first" },
    { key: "random", label: "random" },
    { key: "liked", label: "most liked" },
    { key: "reviewed", label: "most reviewed" },
    { key: "controversial", label: "controversial" },
    { key: "consensus", label: "consensus" }
  ];

  /* The fourteen overlays, in the order the menu lists them and the order the
   * launch link's show= names them. */
  var OVERLAYS = [
    { key: "prompt", k: "P", label: "prompt" },
    { key: "authors", k: "A", label: "authors: who prompted, planned, wrote" },
    { key: "generation", k: "G", label: "generation and lineage" },
    { key: "views", k: "V", label: "views" },
    { key: "likes", k: "L", label: "likes" },
    { key: "qr", k: "Q", label: "QR code: scan to open this entry" },
    { key: "brief", k: "B", label: "brief" },
    { key: "statement", k: "T", label: "artist's statement" },
    { key: "judgment", k: "J", label: "judgment: humans, agents" },
    { key: "code", k: "C", label: "the sketch.js that is running" },
    { key: "licence", k: "I", label: "license" },
    { key: "date", k: "D", label: "creation date" },
    { key: "tokens", k: "K", label: "tokens" },
    { key: "seconds", k: "W", label: "seconds to write" }
  ];

  /* Six on, eight off (spec §1.9, amended by qr.md §1.7). The code is on by
   * default because it is the reason that packet exists: a projection nobody
   * can act on is a screensaver, and this is the only overlay that invites a
   * stranger to do something. */
  var DEFAULT_SHOW = ["prompt", "authors", "generation", "views", "likes", "qr"];
  var DEFAULT_EVERY = 60;
  var DEFAULT_ORDER = "newest";

  /* The stored settings blob's shape. A projector that has been running since
   * before the QR overlay existed has a show map that cannot mention a key
   * that did not exist, so without this it would come back with the code off
   * and nobody at the keyboard to notice (qr.md §5.1). */
  var SETTINGS_VERSION = 2;

  /* The third line under the code, and the only one that is a promise: all
   * three verbs are the write path. A projection that invites a stranger to
   * do something the gallery cannot accept is worse than one that only shows
   * the URL, so with no write path this line is not printed at all — the same
   * rule the views and likes overlays follow when they print an em dash. */
  var QR_VERBS = "judge it · like it · ask for a revision";

  var ENTRIES = [];
  var COUNTS = {};
  var SOURCES = {};           /* entry id -> sketch.js text, fetched once */
  var config = null;
  var frame = null;           /* the one iframe, or null (spec §4.5) */

  var state = {
    every: DEFAULT_EVERY,
    paused: false,
    order: DEFAULT_ORDER,
    hideAll: false,
    show: {},
    i: 0,
    elapsed: 0,
    seq: [],
    playing: false,
    viewed: false,      /* has this seat been counted? cleared by seat() */
    sinceInput: 0,      /* playing seconds since the last key or mouse move */
    viewsOff: false     /* ?views=0, which is not a key and is not persisted */
  };

  /* ---- small helpers ---------------------------------------------------- */

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

  /* One decimal and a unit, or the same em dash: a wall time the database does
   * not have is not zero seconds, and "written in 0.0 s" would be a claim. */
  function secs(value) {
    return typeof value === "number" ? value.toFixed(1) + " s" : "—";
  }

  /* The code heading says bytes, so it counts bytes rather than characters: a
   * sketch with an em dash in a comment is longer than its length says. ES5
   * has no TextEncoder to lean on, and this is the round trip that works. */
  function byteLength(text) {
    return unescape(encodeURIComponent(String(text))).length;
  }

  function ready(fn) {
    if (document.readyState !== "loading") { fn(); }
    else { document.addEventListener("DOMContentLoaded", fn); }
  }

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

  /* Unlike loadConfig, this one lets a failure through. A missing config.json
   * is a gallery with no write path, which is a state this page knows how to
   * be in; a missing kiosk.json is nothing to play, and the card has to say
   * so rather than hand over a black screen. */
  /* no-store for the same reason config.json has it: render_index rewrites
   * this file on every deploy, and the one page that must not show a stale
   * list is the one nobody reloads. A projector is opened once and left for
   * days; a cached manifest would keep playing the gallery as it was the
   * morning it was switched on. */
  function loadManifest() {
    return fetch(ROOT + "kiosk.json", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        ENTRIES = (data && data.entries) || [];
        return ENTRIES;
      });
  }

  /* ---- views and likes -------------------------------------------------- */

  /* A plain public GET, and anonymous like the view below it: no cookie, no
   * bearer, nothing that names anybody. /counts is a public read and this
   * page never signs anybody in, so it has nothing to present — and
   * presenting nothing is still the point now that it does write. */
  function fetchCounts(ids) {
    return fetch(base() + "/counts?entries=" + encodeURIComponent(ids.join(",")))
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

  /* The write, and the whole of it (docs/plans/kiosk-views.md §3).
   *
   * Anonymous, like the read above: no cookie, no bearer, nothing that names
   * anybody. /view takes a view without one, and a projector in a lobby has
   * no viewer to name — a kiosk that presented an identity would be filing
   * every sketch it played under whoever last signed in on that machine.
   *
   * Fire and forget. A view that does not land is not an error and there is
   * nothing to tell the room, which is how gallery.js sends the entry page's.
   * `source` lets the write path keep the projector's views apart from the
   * ones a person clicked; an absent one means the entry page, so nothing
   * else that posts here has to change. */
  function sendView(entry) {
    if (!entry) { return; }
    fetch(base() + "/view", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entry_id: entry.id, source: "kiosk" })
    }).catch(function () { /* the room is not told, and does not need to be */ });
  }

  /* Ten seconds, or the whole slot when the slot is shorter than ten: a
   * projector set to 15 s still counts what it shows. */
  function viewAfter() {
    return Math.min(VIEW_AFTER_S, state.every);
  }

  /* Every condition in one place, so there is one line to read when asking
   * why a kiosk is or is not counting. A gallery with no write path has
   * nowhere to post; the other three are the restraints. */
  function countingViews() {
    if (!base() || state.viewsOff) { return false; }
    if (config && config.kiosk_views === false) { return false; }
    return state.sinceInput < VIEW_STOP_S;
  }

  function loadCounts() {
    var ids = [];
    var at;
    var batches = [];
    if (!base() || ENTRIES.length === 0) { return Promise.resolve(); }
    for (at = 0; at < ENTRIES.length; at += 1) { ids.push(ENTRIES[at].id); }
    for (at = 0; at < ids.length; at += COUNTS_BATCH) {
      batches.push(fetchCounts(ids.slice(at, at + COUNTS_BATCH)));
    }
    return Promise.all(batches).then(function () {
      // "most liked" sorts on numbers that have only just arrived.
      if (state.order === "liked" && state.playing) { reorder("liked"); }
      if (state.playing) { paint(current()); paintStatus(); }
    });
  }

  /* ---- settings: the URL, then storage, then the defaults (spec §1.8) ---- */

  function named(order) {
    var at;
    for (at = 0; at < ORDERS.length; at += 1) {
      if (ORDERS[at].key === order) { return true; }
    }
    return false;
  }

  function overlayNamed(key) {
    var at;
    for (at = 0; at < OVERLAYS.length; at += 1) {
      if (OVERLAYS[at].key === key) { return true; }
    }
    return false;
  }

  /* 15 to 600 seconds in 15 second steps, whatever a URL or a stored setting
   * asks for. A projector nobody is watching should not be able to be left on
   * a sketch for an hour by a typo. */
  function clampEvery(value) {
    var seconds = Math.round(Number(value) / 15) * 15;
    if (isNaN(seconds)) { return DEFAULT_EVERY; }
    return Math.max(15, Math.min(600, seconds));
  }

  function showFrom(list) {
    var show = {};
    var at;
    for (at = 0; at < list.length; at += 1) {
      if (overlayNamed(list[at])) { show[list[at]] = true; }
    }
    return show;
  }

  function shownKeys() {
    var keys = [];
    var at;
    for (at = 0; at < OVERLAYS.length; at += 1) {
      if (state.show[OVERLAYS[at].key]) { keys.push(OVERLAYS[at].key); }
    }
    return keys;
  }

  function readSettings() {
    var params = new URLSearchParams(window.location.search);
    var saved = null;
    var order;
    var every;
    var show;
    var key;
    var list;

    state.show = showFrom(DEFAULT_SHOW);

    try {
      saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "null");
    } catch (err) { saved = null; }
    if (saved && typeof saved === "object") {
      if (named(saved.order)) { state.order = saved.order; }
      if (saved.every) { state.every = clampEvery(saved.every); }
      if (saved.show && typeof saved.show === "object") {
        list = [];
        for (key in saved.show) {
          if (Object.prototype.hasOwnProperty.call(saved.show, key) && saved.show[key]) {
            list.push(key);
          }
        }
        // A blob written before the QR overlay existed cannot mention it, so
        // it would come back off on every projector that has ever been
        // configured. Migrate rather than leave those rooms without the one
        // overlay a visitor can act on (qr.md §5.1).
        if (!saved.v || saved.v < SETTINGS_VERSION) { list.push("qr"); }
        state.show = showFrom(list);
      }
    }

    // A parameter beats a stored setting, so a bookmarked projector comes up
    // the way the bookmark says whatever the last person at the keyboard did.
    order = params.get("order");
    if (named(order)) { state.order = order; }
    every = params.get("every");
    if (every !== null && every !== "" && !isNaN(Number(every))) {
      state.every = clampEvery(every);
    }
    show = params.get("show");
    if (show !== null) { state.show = showFrom(show.split(",")); }
    // Not a key, and never written to storage: which sketches the gallery
    // counts is a property of the projector somebody set up, not something a
    // bumped keyboard should be able to change on the way past.
    state.viewsOff = params.get("views") === "0";
  }

  /* The launch link and the address bar are the same three parameters, built
   * the same way, so the link in the menu footer is a link to what is on the
   * screen. Order and every come from fixed vocabularies and the overlay keys
   * are [a-z], so nothing here needs escaping; commas stay commas, which is
   * what makes the link readable on a projector. */
  function query() {
    return "order=" + state.order +
      "&every=" + state.every +
      "&show=" + shownKeys().join(",") +
      // persist() rewrites the address bar from this string, so a parameter
      // that is not in it is a parameter the first acting key throws away.
      // It rides in the launch link for the same reason: the link is supposed
      // to reproduce the projector, and a projector that counts nothing is
      // not reproduced by a link that counts.
      (state.viewsOff ? "&views=0" : "");
  }

  /* Both halves of §1.8 at once. The address bar keeps only these three
   * parameters: the kiosk page has no others. */
  function persist() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
        v: SETTINGS_VERSION,
        every: state.every, order: state.order, show: state.show
      }));
    } catch (err) { /* private mode: the URL still carries it */ }
    if (window.history && window.history.replaceState) {
      window.history.replaceState(
        null, "", window.location.pathname + "?" + query() + window.location.hash
      );
    }
  }

  /* ---- the manifest's numbers, ordered as the grid orders its cards ------ */

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

  /* Every paired comparison this entry has been in, summed over both
   * populations and both questions — the same total the card's marks carry. */
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

  /* The percentile gap between the two populations on "rather look at it".
   * -1 when either is missing, so entries nobody has compared sort to the end
   * rather than sorting as perfect agreement. */
  function gapOf(entry) {
    var human = stand(entry, "human", "look");
    var agent = stand(entry, "agent", "look");
    if (!human || !agent) { return -1; }
    if (typeof human.pct !== "number" || typeof agent.pct !== "number") { return -1; }
    return Math.abs(human.pct - agent.pct);
  }

  /* The stamps are ISO 8601 in UTC with a trailing Z, so they compare as
   * strings; the id breaks a tie the way the generator breaks it. */
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

  /* Positions into ENTRIES, in the asked-for order. The seven comparators are
   * gallery.js's own, restated over the manifest's numbers instead of the
   * cards' DOM, so the projector and the grid cannot disagree about what
   * "most reviewed" means. */
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

  /* Without a write path there are no likes to sort on, and an order that is
   * every entry tied at zero is not an order (spec §4.2). The fallback lands
   * in state rather than only in the sequence, so the status line, the menu's
   * current row and the launch link all name the order that is really
   * playing: a projector that says "liked" while playing newest is lying
   * about what the room is looking at. */
  function settleOrder() {
    if (state.order === "liked" && !base()) {
      state.order = "newest";
      // The stored setup and the address bar named an order this gallery
      // cannot play. Leave them naming the one it can.
      persist();
    }
  }

  /* Changing the order keeps the sketch that is on screen on screen, and
   * re-seats it where the new order puts it. */
  function reorder(order) {
    var showing = current();
    var id = showing ? showing.id : null;
    var at;
    state.order = order;
    settleOrder();
    state.seq = sequence(state.order);
    state.i = 0;
    for (at = 0; at < state.seq.length; at += 1) {
      if (ENTRIES[state.seq[at]].id === id) { state.i = at; break; }
    }
  }

  function current() {
    if (!state.seq.length) { return null; }
    return ENTRIES[state.seq[state.i]] || null;
  }

  /* ---- the frame -------------------------------------------------------- */

  /* The same sandbox as everywhere else in the gallery: scripts yes, and
   * nothing else — no same-origin, no forms, no top navigation, no allow=
   * and no postMessage channel. A sketch's own key handler therefore never
   * sees a kiosk key, and a kiosk key never reaches a sketch. */
  function showEntry(entry) {
    if (frame) { frame.remove(); frame = null; }
    if (!entry) { return; }
    frame = document.createElement("iframe");
    frame.className = "sketch";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.src = ROOT + entry.sketch;
    frame.title = "entry " + entry.id + " running";
    $("stage").insertBefore(frame, $("caption"));
    fitFrame();
  }

  /* An opaque frame cannot be asked how big its canvas is, so the generator
   * says: a manifest row with canvas gets a frame of that aspect ratio scaled
   * to fit, and one without gets a frame that fills the stage. The size goes
   * on the stage as two custom properties rather than on the frame as inline
   * style, so the frame carries the four attributes it is allowed and no more. */
  function fitFrame() {
    var stage = $("stage");
    var entry = current();
    var width;
    var height;
    var room;
    var tall;
    var scale;
    stage.style.removeProperty("--frame-w");
    stage.style.removeProperty("--frame-h");
    if (!entry || !entry.canvas || entry.canvas.length !== 2) { return; }
    width = Number(entry.canvas[0]);
    height = Number(entry.canvas[1]);
    room = stage.clientWidth;
    tall = stage.clientHeight;
    if (!width || !height || !room || !tall) { return; }
    scale = Math.min(room / width, tall / height);
    stage.style.setProperty("--frame-w", Math.floor(width * scale) + "px");
    stage.style.setProperty("--frame-h", Math.floor(height * scale) + "px");
  }

  /* ---- the words -------------------------------------------------------- */

  /* lineage.split_prompt, in JavaScript: Revise: only counts at the start of
   * its own line, which is why a prompt with the word in the middle of a
   * sentence stays in one piece. */
  function promptParts(prompt) {
    var parts = String(prompt || "").replace(/^\s+|\s+$/g, "").split(/^Revise:[ \t]*/m);
    var revisions = [];
    var at;
    for (at = 1; at < parts.length; at += 1) {
      revisions.push(parts[at].split(/\s+/).join(" ").replace(/^ | $/g, ""));
    }
    return { root: parts[0].split(/\s+/).join(" ").replace(/^ | $/g, ""), revisions: revisions };
  }

  /* 14 Sep 2026 19:05 UTC. The clock is read in UTC as well as printed as
   * UTC: a projector in a room west of Greenwich should not be told a sketch
   * was made the day before it says it was. */
  function fmtDate(iso) {
    var when = new Date(iso);
    if (isNaN(when.getTime())) { return String(iso); }
    return when.toLocaleDateString(undefined, {
      day: "numeric", month: "short", year: "numeric", timeZone: "UTC"
    }) + " " + when.toISOString().slice(11, 16) + " UTC";
  }

  function on(key) { return !state.hideAll && !!state.show[key]; }

  /* One population's standing, the way the entry page says it: the score on
   * "rather look at it", which is the axis the orders sort on, and how many
   * pairs it rests on. */
  function judgeText(entry, population) {
    var look = stand(entry, population, "look");
    if (!look) { return NO_PAIRS; }
    return look.score.toFixed(2) + " over " + look.n + " pair" + (look.n === 1 ? "" : "s");
  }

  /* The quadrant needs both questions, as the entry page's compass does: one
   * coordinate is not a point, and inventing the other would put the entry
   * somewhere nobody voted for. */
  function quadText(entry, population) {
    var look = stand(entry, population, "look");
    var brief = stand(entry, population, "brief");
    if (!look || !brief) { return ""; }
    return QUADRANTS[
      (look.pct >= COMPASS_STRONG ? "1" : "0") + (brief.pct >= COMPASS_STRONG ? "1" : "0")
    ];
  }

  function captionHtml(entry) {
    var counts = COUNTS[entry.id] || {};
    var parts = promptParts(entry.prompt);
    var facts = [];
    var revised;
    var html = "";
    if (on("prompt")) {
      html += "<h1 class=\"prompt\"><span class=\"num\">#" + esc(entry.id) + "</span>" +
        esc(parts.root) + "</h1>";
      if (parts.revisions.length) {
        revised = parts.revisions.length === 1
          ? "revised once" : "revised " + parts.revisions.length + " times";
        html += "<p class=\"revisions\"><b>" + revised + ", latest:</b> " +
          esc(parts.revisions[parts.revisions.length - 1]) + "</p>";
      }
    }
    if (on("authors")) {
      html += "<p class=\"authors\">Prompted by <span>" + esc(entry.submitted_by) +
        "</span> · planned by <span>" + esc(entry.planner) +
        "</span> · written by <span>" + esc(entry.executor) + "</span> under the " +
        esc(entry.rules_file) + " rules" +
        (entry.attempts > 1 ? ", gate passed on attempt " + esc(entry.attempts) : "") + "</p>";
    }
    if (on("generation")) {
      facts.push("<li class=\"gen\"><b>generation " + esc(entry.generation) + "</b>" +
        (entry.parent_entry_id
          ? " · revised from #" + esc(entry.parent_entry_id) +
            (entry.critique_by ? " after a critique by " + esc(entry.critique_by) : "")
          : " · a root") + "</li>");
    }
    if (on("views")) { facts.push("<li><b>" + num(counts.views) + "</b> views</li>"); }
    if (on("likes")) { facts.push("<li><b>" + num(counts.likes) + "</b> likes</li>"); }
    if (on("date")) { facts.push("<li>created <b>" + esc(fmtDate(entry.created_utc)) + "</b></li>"); }
    if (on("tokens")) {
      facts.push("<li><b>" + num(entry.prompt_tokens) + "</b> prompt + <b>" +
        num(entry.completion_tokens) + "</b> completion tokens</li>");
    }
    if (on("seconds")) {
      facts.push("<li>written in <b>" + secs(entry.wall_s) + "</b></li>");
    }
    if (on("licence")) { facts.push("<li><b>" + esc(entry.licence) + "</b></li>"); }
    if (facts.length) { html += "<ul class=\"facts\">" + facts.join("") + "</ul>"; }
    return html;
  }

  /* The printed URL, minus its scheme, for width. Stripped for display only:
   * the line is for somebody typing it into a phone, and https:// is eight
   * characters nobody types. */
  function plainUrl(url) {
    return String(url).replace(/^https?:\/\//, "");
  }

  /* The QR block (qr.md §5.3): the code, then up to three fixed lines.
   *
   * entry.qr is the kiosk code's path, straight from the manifest and never
   * assembled here — the script encodes nothing, fetches nothing and adds no
   * parameter to anything. The code it names carries ?kiosk; the URL printed
   * under it does not, because the line is for someone typing what they read
   * and typing is not scanning (§1.8). */
  function qrHtml(entry) {
    var html;
    if (!on("qr") || !entry.qr || !entry.url) { return ""; }
    html = "<div class=\"qr\"><img class=\"code\" src=\"" + esc(ROOT + entry.qr) +
      "\" alt=\"\">" +
      "<p class=\"scan\">scan to open #" + esc(entry.id) + "</p>" +
      "<p class=\"addr\">" + esc(plainUrl(entry.url)) + "</p>";
    if (base()) { html += "<p class=\"verbs\">" + esc(QR_VERBS) + "</p>"; }
    return html + "</div>";
  }

  /* The caption is a row of two now: the words, and the code beside them. An
   * empty caption stays genuinely empty, because .caption:empty is what takes
   * the scrim off the stage when H hides everything. */
  function captionShell(entry) {
    var words = captionHtml(entry);
    var code = qrHtml(entry);
    if (!words && !code) { return ""; }
    return "<div class=\"words\">" + words + "</div>" + code;
  }

  function paintWords(entry) {
    var words = on("brief") || on("statement") || on("judgment");
    $("wordscol").hidden = !words;
    $("sec-brief").hidden = !on("brief");
    $("brief").textContent = entry.brief || "";
    $("sec-statement").hidden = !on("statement");
    $("statement").textContent = entry.statement || "";
    $("stmt-by").textContent = entry.executor || "";
    $("sec-judgment").hidden = !on("judgment");
    $("j-human").textContent = judgeText(entry, "human");
    $("q-human").textContent = quadText(entry, "human");
    $("j-agent").textContent = judgeText(entry, "agent");
    $("q-agent").textContent = quadText(entry, "agent");
  }

  /* The source, with line numbers, and the heading that says which file it is
   * and that nobody edited it after the executor wrote it. */
  function drawCode(entry, source) {
    var lines = String(source).replace(/\s+$/, "").split("\n");
    var html = [];
    var at;
    $("codefile").textContent = "e/" + entry.id + "/sketch/sketch.js · " +
      num(byteLength(source)) + " bytes, unedited";
    for (at = 0; at < lines.length; at += 1) {
      html.push("<span class=\"ln\">" + (at + 1) + "</span>" + esc(lines[at]));
    }
    $("code").innerHTML = html.join("\n");
    $("code").style.removeProperty("transform");
    codeScroll.start = now();
  }

  /* Fetched once per entry and kept: the same sketch comes round again on a
   * day-long run, and asking again for a file that cannot have changed is a
   * request a projector on a hotel network does not need to make. */
  function paintCode(entry) {
    var source;
    $("codecol").hidden = !on("code");
    if (!on("code")) { return; }
    source = SOURCES[entry.id];
    if (typeof source === "string") { drawCode(entry, source); return; }
    $("codefile").textContent = "e/" + entry.id + "/sketch/sketch.js";
    $("code").textContent = "";
    fetch(ROOT + entry.source)
      .then(function (response) { return response.text(); })
      .then(function (text) {
        SOURCES[entry.id] = text;
        // It may have moved on while that was in flight.
        if (current() === entry && on("code")) { drawCode(entry, text); }
      })
      .catch(function () {
        if (current() === entry && on("code")) {
          $("code").textContent = "the source could not be loaded";
        }
      });
  }

  function paint(entry) {
    if (!entry) { return; }
    // One <img> in the document at a time: replacing the caption's innerHTML
    // is what guarantees the code swaps with the entry like every other
    // overlay, with no second image left behind.
    $("caption").innerHTML = captionShell(entry);
    paintWords(entry);
    paintCode(entry);
    fitFrame();
  }

  /* The source scrolls slowly past when it is taller than its column: a
   * reading pace, then it rests at the end. */
  var codeScroll = { start: 0 };

  function scrollCode(stamp) {
    var pre = $("code");
    var wrap = pre.parentNode;
    var overflow;
    var seconds;
    if (!$("codecol").hidden && pre.scrollHeight > wrap.clientHeight) {
      overflow = pre.scrollHeight - wrap.clientHeight + 48;
      seconds = Math.max(0, (stamp - codeScroll.start - CODE_HOLD_MS) / 1000);
      pre.style.transform =
        "translateY(-" + Math.min(overflow, seconds * CODE_SPEED) + "px)";
    }
    window.requestAnimationFrame(scrollCode);
  }

  /* ---- the menu --------------------------------------------------------- */

  function paintMenu() {
    var rows = [];
    var shown = shownKeys();
    var at;
    var order;
    var overlay;
    $("m-every").textContent = state.every + " s each";
    $("m-every2").textContent = state.every + " s";
    $("m-pause-state").textContent = state.paused ? "paused" : "playing";
    $("m-pause").className = state.paused ? "on" : "";
    $("m-hide-state").textContent = state.hideAll ? "hidden" : "";
    for (at = 0; at < ORDERS.length; at += 1) {
      order = ORDERS[at];
      rows.push("<li class=\"" + (order.key === state.order ? "current" : "") +
        "\"><span class=\"keys\"><kbd>" + (at + 1) + "</kbd></span><span>" +
        order.label + "</span><span class=\"state\">" +
        (order.key === state.order ? "●" : "") + "</span></li>");
    }
    $("m-orders").innerHTML = rows.join("");
    rows = [];
    for (at = 0; at < OVERLAYS.length; at += 1) {
      overlay = OVERLAYS[at];
      rows.push("<li class=\"" + (state.show[overlay.key] ? "on" : "") +
        "\"><span class=\"keys\"><kbd>" + overlay.k + "</kbd></span><span>" +
        overlay.label + "</span><span class=\"state\">" +
        (state.show[overlay.key] ? "on" : "off") + "</span></li>");
    }
    $("m-overlays").innerHTML = rows.join("");
    $("m-count").textContent = shown.length + " of " + OVERLAYS.length + " on";
    $("launch").textContent = "kiosk.html?" + query();
  }

  var menuTimer = null;

  function openMenu() {
    $("menu").hidden = false;
    paintMenu();
    window.clearTimeout(menuTimer);
    menuTimer = window.setTimeout(function () { closeMenu(); paintStatus(); }, MENU_MS);
  }

  function closeMenu() {
    $("menu").hidden = true;
    window.clearTimeout(menuTimer);
  }

  /* ---- playback --------------------------------------------------------- */

  function paintStatus() {
    $("status").innerHTML = (state.paused ? "<span class=\"paused\">paused</span> · " : "") +
      (state.seq.length ? (state.i + 1) + " of " + state.seq.length : "0 of 0") +
      " · " + esc(state.order) +
      ($("menu").hidden ? "<span class=\"hint\"><kbd>any key</kbd> controls</span>" : "");
  }

  /* Put a sketch on the stage now, with no fade: the first one, after the
   * start card's click. */
  function seat() {
    var entry = current();
    state.elapsed = 0;
    // A new sketch is a new view to earn. Cleared here rather than in go(),
    // because seat() is the one path every sketch on the stage comes through.
    state.viewed = false;
    showEntry(entry);
    paint(entry);
    paintStatus();
  }

  /* A fresh permutation every time a random sequence wraps, so a day-long run
   * is not one order repeated. If the new one opens on the sketch that is
   * still on the stage, move it out of the way: the wrap would otherwise fade
   * out of a sketch and back into the same one, which reads as a stall rather
   * than as a shuffle. */
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

  /* Move by one, with the fade: the timer's end, and manual next/previous. */
  function go(to) {
    var len = state.seq.length;
    var stage = $("stage");
    if (!len) { return; }
    // reshuffled() reads the sketch on screen, so it runs before the sequence
    // it is replacing is thrown away.
    if (state.order === "random" && to >= len) { state.seq = reshuffled(); }
    state.i = ((to % len) + len) % len;
    state.elapsed = 0;
    stage.classList.add("fading");
    window.setTimeout(function () {
      seat();
      stage.classList.remove("fading");
    }, FADE_MS);
  }

  /* Counted by requestAnimationFrame deltas rather than by a wall clock, so a
   * tab the browser has throttled resumes where it was instead of skipping
   * ahead through everything it missed. */
  var last = 0;

  function now() {
    return (window.performance && window.performance.now)
      ? window.performance.now() : new Date().getTime();
  }

  function tick(stamp) {
    var delta = (stamp - last) / 1000;
    last = stamp;
    if (!state.paused) { state.elapsed += delta; state.sinceInput += delta; }
    // Before the advance below, so a fifteen-second slot counts the sketch it
    // is about to leave. The flag is set first and the request goes second:
    // the other order posts once a frame for as long as the first one is in
    // flight, into an endpoint that de-duplicates none of it.
    if (!state.viewed && state.elapsed >= viewAfter() && countingViews()) {
      state.viewed = true;
      sendView(current());
    }
    $("progress").style.width =
      Math.min(100, 100 * state.elapsed / state.every) + "%";
    $("progress").className = "progress" + (state.paused ? " paused" : "");
    if (state.elapsed >= state.every) { go(state.i + 1); }
    window.requestAnimationFrame(tick);
  }

  /* ---- keys (spec §4.1) -------------------------------------------------- */

  function fullScreen() {
    if (document.fullscreenElement) {
      if (document.exitFullscreen) { document.exitFullscreen(); }
      return;
    }
    if (document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen();
    }
  }

  function overlayFor(key) {
    var at;
    if (!key || key.length !== 1) { return null; }
    for (at = 0; at < OVERLAYS.length; at += 1) {
      if (OVERLAYS[at].k.toLowerCase() === key.toLowerCase()) { return OVERLAYS[at]; }
    }
    return null;
  }

  function onKey(event) {
    var key = event.key;
    var handled = true;
    var overlay;
    var at;
    if (!state.playing) { return; }
    // Somebody is here. Set before the modifier check below returns, because
    // a browser shortcut is still a person at the keyboard.
    state.sinceInput = 0;
    // A modifier chord is a browser shortcut and stays one.
    if (event.metaKey || event.ctrlKey || event.altKey) { return; }
    if ($("menu").hidden) {
      // The first press only brings up the controls, so a bumped keyboard
      // never skips a sketch.
      if (key !== "Escape") {
        event.preventDefault();
        openMenu();
        paintStatus();
      }
      return;
    }
    event.preventDefault();
    if (key === "Escape") { closeMenu(); paintStatus(); return; }
    if (key === " ") { state.paused = !state.paused; }
    else if (key === "ArrowRight" || key === "n" || key === "N") { go(state.i + 1); }
    else if (key === "ArrowLeft") { go(state.i - 1); }
    else if (key === "]") { state.every = Math.min(600, state.every + 15); }
    else if (key === "[") { state.every = Math.max(15, state.every - 15); }
    else if (key === "f" || key === "F") { fullScreen(); }
    else if (key === "h" || key === "H") { state.hideAll = !state.hideAll; paint(current()); }
    else if (key === "s" || key === "S") {
      at = 0;
      for (; at < ORDERS.length; at += 1) {
        if (ORDERS[at].key === state.order) { break; }
      }
      reorder(ORDERS[(at + 1) % ORDERS.length].key);
    } else if (/^[1-7]$/.test(key)) {
      reorder(ORDERS[parseInt(key, 10) - 1].key);
    } else {
      overlay = overlayFor(key);
      if (overlay) {
        state.show[overlay.key] = !state.show[overlay.key];
        // Turning one overlay on is asking to see overlays again.
        state.hideAll = false;
        paint(current());
      } else { handled = false; }
    }
    // Every acting key persists, repaints the menu and restarts its timer.
    if (handled) { persist(); paintMenu(); paintStatus(); openMenu(); }
  }

  /* ---- start ------------------------------------------------------------ */

  function play() {
    settleOrder();
    state.seq = sequence(state.order);
    state.i = 0;
    state.playing = true;
    seat();
    last = now();
    window.requestAnimationFrame(tick);
    window.requestAnimationFrame(scrollCode);
    window.setTimeout(openMenu, START_MENU_MS);
  }

  /* The card waits for the manifest rather than the other way round.
   *
   * Taking the click first and fetching afterwards is how a projector on a
   * slow or dead network ends up showing a black screen that answers no key:
   * the card is gone, nothing is playing, and because nothing is playing the
   * key handler returns without even opening the menu. So the button is dead
   * until there is something to play, and says which of the two it is. */
  function waitToStart() {
    var button = $("go");
    button.disabled = true;
    button.textContent = "loading…";
  }

  function offerStart() {
    var button = $("go");
    button.disabled = false;
    button.textContent = "Start";
  }

  function cannotStart() {
    // In place of the card's last line, which explains the click it is no
    // longer worth making.
    var note = document.querySelector(".welcome .note");
    $("go").disabled = true;
    $("go").textContent = "Start";
    if (note) { note.textContent = CANNOT_LOAD; }
  }

  function wireStart() {
    $("go").addEventListener("click", function () {
      // The manifest is already in hand, so this click only starts: it is
      // spent on the gesture the browser wants before it will run audio or go
      // full screen, and on nothing else.
      if (!ENTRIES.length) { return; }
      $("welcome").hidden = true;
      document.body.classList.add("playing");
      play();
    });
  }

  function wireIdle() {
    var idleTimer = null;
    document.addEventListener("mousemove", function () {
      // The same handler the cursor hides on, for the same reason: this is
      // where the page already learns that the room is not empty.
      state.sinceInput = 0;
      document.body.classList.remove("idle");
      window.clearTimeout(idleTimer);
      idleTimer = window.setTimeout(function () {
        document.body.classList.add("idle");
      }, IDLE_MS);
    });
  }

  ready(function () {
    readSettings();
    paintMenu();
    paintStatus();
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", function () { window.setTimeout(fitFrame, 60); });
    wireIdle();
    wireStart();
    waitToStart();
    // Both files are asked for on load, not on the click: the card is the
    // wait, so that the click is not.
    Promise.all([loadManifest(), loadConfig()]).then(function () {
      // An empty gallery is nothing to play, and says so rather than starting
      // into a black screen.
      if (!ENTRIES.length) { cannotStart(); return; }
      loadCounts();
      // Ten minutes: a projector runs all day, and a like recorded at noon
      // should show up before the room empties.
      window.setInterval(loadCounts, COUNTS_EVERY_MS);
      // The write path is known now, so an order that needs one can be
      // settled before anybody reads the menu.
      settleOrder();
      paintMenu();
      paintStatus();
      offerStart();
    }).catch(function () {
      cannotStart();
    });
  });
})();
