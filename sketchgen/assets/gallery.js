/* gallery.js — the only script the published gallery loads.
 *
 * Three jobs, all of them read-or-write against the gallery write path
 * (packet 3.3), whose base URL comes from config.json and from nowhere else:
 *
 *   1. counts   GET  <base>/counts?entries=1,2,3   views and likes per entry
 *   2. like     POST <base>/like               one like, identified by GitHub
 *   3. vote     POST <base>/vote               one answer to one question
 *
 * Both writes need a session, and the gallery is a different origin from the
 * write path, so the session arrives as a token in the /callback redirect's
 * fragment, lives in this origin's localStorage, and rides back as a bearer
 * header. See "the session" below for why a cookie cannot do this job.
 *
 * Until that service exists, config.json carries an empty write_path: every
 * count stays an em dash, the like button stays disabled, and the compare page
 * says votes are not being recorded. The generator never writes a number it
 * does not have, and neither does this file.
 *
 * It also owns the grid's order, its filters and its search box. Those ask
 * nothing of anyone: the cards are rendered newest first, the filters are
 * plain links and the search is a form that submits to its own page, so the
 * page is right before this file runs and stays right if it never does.
 *
 * Vanilla, no framework, no build step. The page sets window.SKETCHGEN_ROOT to
 * its own relative path back to the gallery root.
 */

(function () {
  "use strict";

  var ROOT = window.SKETCHGEN_ROOT || "./";
  var config = null;

  function ready(fn) {
    if (document.readyState !== "loading") {
      fn();
    } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  }

  function base() {
    return config && config.write_path ? String(config.write_path).replace(/\/+$/, "") : "";
  }

  /* ---- the session ----------------------------------------------------- */

  var STORAGE_KEY = "sketchgen_session";

  /* Why the session is kept here and not left to a cookie: this page is on
   * github.io and the write path is on workers.dev, so every call it makes is
   * cross-site. The Worker's SameSite=Lax cookie is not sent on those, and
   * SameSite=None would be dropped anyway as a third-party cookie by Safari and
   * increasingly by Chrome. So <write_path>/callback redirects back here with
   * the same signed token in the fragment, this origin keeps it, and it goes
   * back as an Authorization: Bearer header.
   *
   * The token is username.expiry.HMAC(username.expiry). It names the viewer to
   * the Worker and carries no secret of the service: the signing key never
   * leaves the Worker, and this token cannot be used to mint another. */
  function readToken() {
    try { return window.localStorage.getItem(STORAGE_KEY) || ""; } catch (err) { return ""; }
  }

  function writeToken(token) {
    try { window.localStorage.setItem(STORAGE_KEY, token); } catch (err) { /* private mode */ }
  }

  function dropToken() {
    try { window.localStorage.removeItem(STORAGE_KEY); } catch (err) { /* ditto */ }
  }

  /* Runs before anything else: take the token out of the fragment and out of
   * the address bar, so copying the URL does not hand it to someone else. A
   * fragment rather than a query string because a fragment is never sent to a
   * server, so it cannot land in a log or a Referer header. */
  function claimTokenFromHash() {
    var hash = window.location.hash || "";
    if (hash.indexOf("#session=") !== 0) { return; }
    var token = decodeURIComponent(hash.slice("#session=".length));
    if (token) { writeToken(token); }
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    } else {
      window.location.hash = "";
    }
  }

  /* The one place a request's headers are built. The like button and the
   * compare page's vote buttons both call it, so the two cannot drift apart.
   * credentials: "include" stays on every call too: it costs nothing, and it is
   * what works for someone browsing the Worker's own domain. */
  function authHeaders(extra) {
    var headers = {};
    var key;
    if (extra) {
      for (key in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, key)) { headers[key] = extra[key]; }
      }
    }
    var token = readToken();
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

  function paintSession(username) {
    var login = document.querySelector("[data-login]");
    if (login) {
      if (base()) { login.setAttribute("href", base() + "/login"); }
      login.hidden = !!username;
    }
    var button = document.querySelector("[data-like]");
    if (button && base()) {
      button.disabled = !username;
      if (!username) { button.textContent = "sign in to like"; }
      else if (button.textContent === "sign in to like") { button.textContent = "like"; }
    }
    var slot = document.querySelector("[data-session]");
    if (!slot) { return; }
    slot.textContent = "";
    if (!username) { slot.hidden = true; return; }
    slot.hidden = false;
    slot.appendChild(document.createTextNode("signed in: " + username + " · "));
    var out = document.createElement("a");
    out.setAttribute("href", "#");
    out.textContent = "sign out";
    out.addEventListener("click", function (event) {
      event.preventDefault();
      var headers = authHeaders();
      dropToken();
      paintSession(null);
      if (base()) {
        fetch(base() + "/logout", { credentials: "include", headers: headers })
          .catch(function () { /* the copy that mattered is already gone */ });
      }
    });
    slot.appendChild(out);
  }

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

  function loadConfig() {
    return fetch(ROOT + "config.json", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (data) { config = data || {}; return config; })
      .catch(function () { config = {}; return config; });
  }

  /* ---- views and likes ------------------------------------------------- */

  function entryIds() {
    var ids = [];
    Array.prototype.forEach.call(document.querySelectorAll("[data-entry]"), function (el) {
      var id = el.getAttribute("data-entry");
      if (id && ids.indexOf(id) === -1) { ids.push(id); }
    });
    return ids;
  }

  function paintCounts(counts) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-entry]"), function (el) {
      var row = counts[el.getAttribute("data-entry")];
      if (!row) { return; }
      Array.prototype.forEach.call(el.querySelectorAll("[data-count]"), function (slot) {
        var value = row[slot.getAttribute("data-count")];
        if (typeof value === "number") { slot.textContent = String(value); }
      });
    });
  }

  function loadCounts() {
    var ids = entryIds();
    if (!base() || ids.length === 0) { return; }
    fetch(base() + "/counts?entries=" + encodeURIComponent(ids.join(",")), {
      credentials: "include"
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        paintCounts(data && data.counts ? data.counts : data || {});
        // The numbers the "most liked" order sorts on have only just arrived.
        if (currentSort() === "liked") { applySort("liked"); }
      })
      .catch(function () { /* leave the em dashes where they are */ });
  }

  function wireLike() {
    var button = document.querySelector("[data-like]");
    var login = document.querySelector("[data-login]");
    if (login && base()) { login.setAttribute("href", base() + "/login"); }
    if (!button) { return; }
    if (!base()) {
      button.title = "the gallery write path is not deployed yet";
      return;
    }
    // loadMe() decides whether this is usable; until it answers, leave it alone.
    button.addEventListener("click", function () {
      button.disabled = true;
      fetch(base() + "/like", {
        method: "POST",
        credentials: "include",
        headers: authHeaders({ "Content-Type": "application/json" }),
        // worker.js routeLike: { entry_id, on } — on is the state we are asking for
        body: JSON.stringify({
          entry_id: Number(button.getAttribute("data-like")),
          on: button.getAttribute("data-liked") !== "true"
        })
      })
        .then(function (response) {
          if (refused(response)) {
            button.textContent = "sign in to like";
            return null;
          }
          return response.json();
        })
        .then(function (data) {
          if (!data) { return; }
          button.setAttribute("data-liked", data.on ? "true" : "false");
          button.textContent = data.on ? "liked" : "like";
          button.disabled = false;
          loadCounts();
        })
        .catch(function () { button.disabled = false; });
    });
  }

  /* ---- the grid's filters, which are plain links ----------------------- */

  /* The query a link's href carries, as parameters rather than as a string:
   * the sort lives in the same query now, so comparing whole query strings
   * would stop recognising the active filter the moment anything sorted. */
  function linkParams(link) {
    var href = link.getAttribute("href") || "";
    var cut = href.indexOf("?");
    return new URLSearchParams(cut === -1 ? "" : href.slice(cut + 1));
  }

  /* One parameter into the address bar without navigating: reload the page, or
   * send it to someone, and the grid comes back the way it was left. An empty
   * value drops the parameter rather than writing an empty one. */
  function remember(name, value) {
    if (!window.history || !window.history.replaceState) { return; }
    var params = new URLSearchParams(window.location.search);
    if (value) { params.set(name, value); } else { params.delete(name); }
    var query = params.toString();
    window.history.replaceState(
      null, "", window.location.pathname + (query ? "?" + query : "") + window.location.hash
    );
  }

  /* The filters are still plain links, so following one is a fresh page load:
   * whatever the viewer chose here has to ride along in the href or it is lost
   * at the click. The sort does this, and so does the search. */
  function carryOnLinks(name, value) {
    Array.prototype.forEach.call(document.querySelectorAll("a.filter"), function (link) {
      var href = link.getAttribute("href") || "";
      var cut = href.indexOf("?");
      var params = linkParams(link);
      if (value) { params.set(name, value); } else { params.delete(name); }
      var query = params.toString();
      link.setAttribute("href", (cut === -1 ? href : href.slice(0, cut)) + (query ? "?" + query : ""));
    });
  }

  /* The one place a card is hidden or shown, because two places would fight:
   * the filter links and the search box each answer half of the question and a
   * card is visible only if it passes both halves. Returns how many cards the
   * page has at all, which is none on an empty grid. */
  function applyVisibility() {
    var cards = document.querySelectorAll(".card[data-entry]");
    if (cards.length === 0) { return 0; }
    var params = new URLSearchParams(window.location.search);
    var rules = params.get("rules");
    var executor = params.get("executor");
    var words = queryTerms(currentQuery());
    var shown = 0;
    Array.prototype.forEach.call(cards, function (card) {
      var keep = (!rules || card.getAttribute("data-rules") === rules) &&
                 (!executor || card.getAttribute("data-executor") === executor) &&
                 searchMatches(card, words);
      card.hidden = !keep;
      if (keep) { shown += 1; }
    });
    paintSearchCount(words.length > 0, shown, cards.length);
    return cards.length;
  }

  function applyFilters() {
    if (applyVisibility() === 0) { return; }
    var params = new URLSearchParams(window.location.search);
    var rules = params.get("rules");
    var executor = params.get("executor");
    Array.prototype.forEach.call(document.querySelectorAll("a.filter"), function (link) {
      var own = linkParams(link);
      var active = (own.get("rules") || "") === (rules || "") &&
                   (own.get("executor") || "") === (executor || "");
      if (active) { link.setAttribute("aria-current", "true"); }
      else { link.removeAttribute("aria-current"); }
    });
    /* The block is collapsed in the HTML. A filtered view has to show which
     * filter it is showing, so the URL opens it. */
    var block = document.querySelector("details.filters");
    if (block && (rules || executor)) { block.open = true; }
  }

  /* ---- the grid's search box ------------------------------------------- */

  /* Every card carries a data-search attribute the generator built out of the
   * entry's number, its prompt, its brief, its rules file, its executor and
   * whoever submitted it — already lowercased and collapsed, so the matching
   * here is: lowercase the query, split it on spaces, and keep a card whose
   * attribute contains every one of those terms. Nothing is fetched and no
   * index is built; the whole grid is on the page already.
   *
   * With this script absent the box is a form that submits to the page it is
   * already on, which reloads it showing everything. Nothing is broken. */
  function searchBox() {
    return document.querySelector("input[type=\"search\"][data-search]");
  }

  /* The box is the truth once the page has one, and the URL fills the box on
   * load; that way ?q= works and so does a browser with no replaceState. */
  function currentQuery() {
    var box = searchBox();
    if (box) { return box.value; }
    return new URLSearchParams(window.location.search).get("q") || "";
  }

  function queryTerms(query) {
    return String(query || "").toLowerCase().split(/\s+/).filter(function (word) {
      return word !== "";
    });
  }

  function searchMatches(card, words) {
    var haystack = card.getAttribute("data-search") || "";
    for (var index = 0; index < words.length; index++) {
      if (haystack.indexOf(words[index]) === -1) { return false; }
    }
    return true;
  }

  /* "12 of 41" while a query is being made of the grid, and nothing at all
   * when it is not: an unsearched grid needs no commentary. */
  function paintSearchCount(active, shown, total) {
    var slot = document.querySelector("[data-search-count]");
    if (!slot) { return; }
    slot.textContent = active ? shown + " of " + total : "";
    slot.hidden = !active;
  }

  function wireSearch() {
    var box = searchBox();
    if (!box) { return; }
    box.value = new URLSearchParams(window.location.search).get("q") || "";

    function search() {
      var query = box.value.replace(/^\s+|\s+$/g, "");
      remember("q", query);
      carryOnLinks("q", query);
      applyVisibility();
    }

    // Small grids, one attribute each: there is nothing here to debounce.
    box.addEventListener("input", search);
    box.addEventListener("keydown", function (event) {
      if (event.key === "Escape" || event.keyCode === 27) {
        box.value = "";
        search();
      }
    });
    if (box.form) {
      // Enter must not reload the page out from under a live result.
      box.form.addEventListener("submit", function (event) {
        event.preventDefault();
        search();
      });
    }
    if (box.value) { carryOnLinks("q", box.value); }
  }

  /* ---- the grid's order ------------------------------------------------ */

  /* Every page is rendered newest first, so "newest" is the default and the
   * one order the URL never has to name. The other three are ?sort=<name>,
   * which makes a sorted view a link someone can send. */
  var SORTS = ["newest", "oldest", "random", "liked"];
  var DEFAULT_SORT = "newest";

  function currentSort() {
    var want = new URLSearchParams(window.location.search).get("sort");
    return SORTS.indexOf(want) === -1 ? DEFAULT_SORT : want;
  }

  function publishedAt(card) { return card.getAttribute("data-published") || ""; }

  function entryOf(card) { return Number(card.getAttribute("data-entry")) || 0; }

  /* The counts arrive from the write path long after the page does, and read
   * "—" until they do. An em dash is not a number of likes; it is no likes
   * yet, and sorts as zero. */
  function likesOf(card) {
    var slot = card.querySelector("[data-count=\"likes\"]");
    var value = slot ? parseInt(slot.textContent, 10) : 0;
    return isNaN(value) ? 0 : value;
  }

  /* The stamps are ISO 8601 in UTC with a trailing Z, so they compare as
   * strings; the id breaks a tie the way the generator breaks it. */
  function byNewest(a, b) {
    var left = publishedAt(a);
    var right = publishedAt(b);
    if (left !== right) { return left < right ? 1 : -1; }
    return entryOf(b) - entryOf(a);
  }

  function byOldest(a, b) { return -byNewest(a, b); }

  function byLikes(a, b) {
    var diff = likesOf(b) - likesOf(a);
    return diff !== 0 ? diff : byNewest(a, b);
  }

  /* Fisher-Yates: every order equally likely, which "random" ought to mean. */
  function shuffle(cards) {
    var index, pick, held;
    for (index = cards.length - 1; index > 0; index--) {
      pick = Math.floor(Math.random() * (index + 1));
      held = cards[index];
      cards[index] = cards[pick];
      cards[pick] = held;
    }
    return cards;
  }

  function paintSortButtons(name) {
    Array.prototype.forEach.call(document.querySelectorAll("button.sort"), function (button) {
      if (button.getAttribute("data-sort") === name) {
        button.setAttribute("aria-current", "true");
      } else {
        button.removeAttribute("aria-current");
      }
    });
  }

  /* Moving the nodes rather than rewriting the grid keeps every card's image,
   * its counts and anything already wired to it exactly as it was. */
  function applySort(name) {
    var grid = document.querySelector(".grid");
    if (!grid) { return; }
    var cards = Array.prototype.slice.call(grid.querySelectorAll(".card[data-entry]"));
    paintSortButtons(name);
    if (cards.length < 2) { return; }
    if (name === "random") { shuffle(cards); }
    else if (name === "oldest") { cards.sort(byOldest); }
    else if (name === "liked") { cards.sort(byLikes); }
    else { cards.sort(byNewest); }
    var order = document.createDocumentFragment();
    cards.forEach(function (card) { order.appendChild(card); });
    grid.appendChild(order);
  }

  /* The chosen sort goes in the address bar without navigating, and on the
   * filter links so a click keeps it. Newest is the default, so it is the one
   * the URL says nothing about: an empty value drops the parameter. */
  function rememberSort(name) {
    remember("sort", name === DEFAULT_SORT ? "" : name);
  }

  function carrySort(name) {
    carryOnLinks("sort", name === DEFAULT_SORT ? "" : name);
  }

  function wireSort() {
    var buttons = document.querySelectorAll("button.sort");
    if (buttons.length === 0) { return; }
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var name = button.getAttribute("data-sort") || DEFAULT_SORT;
        rememberSort(name);
        carrySort(name);
        // applySort shuffles every time it is asked for "random", so pressing
        // random again is another shuffle rather than nothing at all.
        applySort(name);
      });
    });
  }

  /* ---- the compare page ------------------------------------------------ */

  function embedded(id, fallback) {
    var node = document.getElementById(id);
    if (!node) { return fallback; }
    try { return JSON.parse(node.textContent) || fallback; } catch (err) { return fallback; }
  }

  function embeddedEntries() { return embedded("sketchgen-entries", []); }

  /* The generator ran pairs.pick_pair over several seeds and baked the result
   * into this page and into pairs.json, so a static site can still offer a
   * *balanced* pair — fewest judgments so far, control against treatment —
   * with no server to ask. ?a= and ?b= from an entry page still win. */
  function offeredPairs() { return embedded("sketchgen-pairs", []); }

  function agentVerdicts() { return embedded("sketchgen-agents", {}); }

  function pairKey(a, b) {
    var low = Math.min(Number(a), Number(b));
    var high = Math.max(Number(a), Number(b));
    return low + "-" + high;
  }

  function pickPair(entries) {
    var params = new URLSearchParams(window.location.search);
    var byId = {};
    entries.forEach(function (entry) { byId[String(entry.id)] = entry; });
    var a = byId[String(params.get("a"))];
    var b = byId[String(params.get("b"))];
    if (a && b && a.id !== b.id) { return [a, b]; }

    var offered = offeredPairs().filter(function (pair) {
      var left = byId[String(pair.a)];
      var right = byId[String(pair.b)];
      return left && right && left.id !== right.id &&
             (!a || left.id === a.id || right.id === a.id);
    });
    if (offered.length) {
      var choice = offered[Math.floor(Math.random() * offered.length)];
      var first = byId[String(choice.a)];
      var second = byId[String(choice.b)];
      // An entry page links here with ?a=<itself>; keep that entry on the left.
      if (a && second.id === a.id) { return [second, first]; }
      return [first, second];
    }

    var rest = entries.filter(function (entry) { return !a || entry.id !== a.id; });
    if (a && rest.length) { return [a, rest[0]]; }
    if (entries.length >= 2) { return [entries[0], entries[1]]; }
    return null;
  }

  /* The agent block is in the page from the start, hidden. It is filled here
   * and revealed only once both human answers are in (spec §5: blind the human
   * too, until they vote). */
  function paintAgents(host, sides) {
    if (!host) { return; }
    var rows = agentVerdicts()[pairKey(sides.A.id, sides.B.id)] || [];
    host.textContent = "";
    if (!rows.length) {
      var none = document.createElement("p");
      none.textContent = "No agent has judged this pair yet.";
      host.appendChild(none);
      return;
    }
    // A verdict is stored against the pair in (low, high) order; this page may
    // be showing it the other way round, so flip it to match A and B.
    var flipped = Number(sides.A.id) > Number(sides.B.id);
    rows.forEach(function (row) {
      var choice = row.choice;
      if (flipped && choice === "A") { choice = "B"; }
      else if (flipped && choice === "B") { choice = "A"; }
      var line = document.createElement("p");
      line.className = "agent-verdict";
      line.textContent = row.judge + " · " + row.question + " · " + choice +
        (row.prompt_version ? " · " + row.prompt_version : "");
      host.appendChild(line);
    });
  }

  /* ---- one running sketch at a time ------------------------------------ */

  /* Four still frames are not enough to answer "which is closer to its brief?"
   * when the brief is about motion, so a thumbnail can be clicked to run the
   * real sketch in its place — the same page the entry embeds, at the same
   * sandbox. Never both at once: two p5 sketches side by side fight over the
   * frame budget and over the audio context, and the comparison stops being
   * about the sketches.
   *
   * This is the whole of the toggle's state: the .side element whose sketch is
   * playing, or null. Stopping REMOVES the iframe rather than hiding it —
   * display:none does not unload a document, so a hidden frame keeps drawing
   * and keeps its AudioContext open. */
  var running = null;

  function runNote(side, text) {
    var note = side.querySelector("[data-run-note]");
    if (note) { note.textContent = text; }
  }

  function stopSketch() {
    if (!running) { return; }
    var side = running;
    running = null;
    var frame = side.querySelector("iframe.sketch");
    if (frame) { frame.remove(); }
    var button = side.querySelector("[data-play]");
    if (button) {
      button.className = "play";
      button.setAttribute("aria-label", "run sketch " + side.getAttribute("data-side"));
      var label = button.querySelector("[data-play-label]");
      if (label) { label.textContent = ""; }
    }
    runNote(side, "click to run");
  }

  function startSketch(side, entry) {
    stopSketch();
    var thumb = side.querySelector("[data-thumb]");
    var button = side.querySelector("[data-play]");
    if (!thumb || !button) { return; }
    var letter = side.getAttribute("data-side");
    var frame = document.createElement("iframe");
    frame.className = "sketch";
    frame.src = ROOT + entry.href + "sketch/";
    frame.title = "sketch " + letter + " running";
    // Exactly the entry page's sandbox, so a sketch behaves the same on both
    // pages: scripts yes, and nothing else — no same-origin, no forms, no top
    // navigation. p5.sound still works; the viewer's click is the gesture that
    // lets the frame start audio.
    frame.setAttribute("sandbox", "allow-scripts");
    thumb.insertBefore(frame, button);
    button.className = "play running";
    button.setAttribute("aria-label", "stop sketch " + letter);
    var label = button.querySelector("[data-play-label]");
    if (label) { label.textContent = "stop"; }
    running = side;
    runNote(side, "running · click to stop");
  }

  function paintSide(side, entry) {
    var thumb = side.querySelector("[data-thumb]");
    var brief = side.querySelector("[data-brief]");
    if (thumb) {
      if (running === side) { stopSketch(); }
      thumb.textContent = "";
      var img = document.createElement("img");
      img.src = ROOT + entry.strip;
      img.alt = "four frames from one of the two sketches";
      // No href means no published sketch page to run: leave the plain strip.
      if (!entry.href) {
        thumb.appendChild(img);
        runNote(side, "");
      } else {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "play";
        button.setAttribute("data-play", "");
        button.setAttribute("aria-label", "run sketch " + side.getAttribute("data-side"));
        button.appendChild(img);
        // Empty while the strip is showing; "stop" while the sketch runs, so
        // the one control both starts and stops without the layout moving.
        var label = document.createElement("span");
        label.setAttribute("data-play-label", "");
        button.appendChild(label);
        button.addEventListener("click", function () {
          if (running === side) { stopSketch(); } else { startSketch(side, entry); }
        });
        thumb.appendChild(button);
        runNote(side, "click to run");
      }
    }
    if (brief) { brief.textContent = entry.brief || entry.prompt || "(no brief recorded)"; }
  }

  function wireCompare() {
    var page = document.querySelector("[data-compare]");
    if (!page) { return; }
    var status = page.querySelector("[data-status]");
    var reveal = page.querySelector("[data-reveal]");
    var entries = embeddedEntries();
    var pair = pickPair(entries);
    if (!pair) {
      Array.prototype.forEach.call(page.querySelectorAll("[data-brief]"), function (el) {
        el.textContent = "There are not two published entries to compare yet.";
      });
      Array.prototype.forEach.call(page.querySelectorAll("[data-vote]"), function (el) {
        el.disabled = true;
      });
      return;
    }
    var sides = { A: pair[0], B: pair[1] };
    Array.prototype.forEach.call(page.querySelectorAll(".side"), function (side) {
      paintSide(side, sides[side.getAttribute("data-side")]);
    });
    paintAgents(page.querySelector("[data-agent-verdicts]"), sides);
    if (status && !base()) {
      status.textContent = "The gallery write path is not deployed yet, so these " +
        "buttons record nothing. Nothing is lost and nothing is counted.";
    }

    var answered = {};
    Array.prototype.forEach.call(page.querySelectorAll(".question"), function (block) {
      var question = block.getAttribute("data-question");
      var note = block.querySelector("[data-answered]");
      Array.prototype.forEach.call(block.querySelectorAll("[data-vote]"), function (button) {
        button.addEventListener("click", function () {
          var choice = button.getAttribute("data-vote");
          answered[question] = choice;
          Array.prototype.forEach.call(block.querySelectorAll("[data-vote]"), function (other) {
            other.setAttribute("aria-pressed", other === button ? "true" : "false");
          });
          if (note) {
            note.textContent = base()
              ? "recorded: " + choice
              : "noted here only: " + choice;
          }
          if (base()) {
            fetch(base() + "/vote", {
              method: "POST",
              credentials: "include",
              // The same header helper the like button uses; see authHeaders.
              headers: authHeaders({ "Content-Type": "application/json" }),
              // Exactly the payload writepath/worker.js:routeVote destructures.
              body: JSON.stringify({
                entry_a: sides.A.id,
                entry_b: sides.B.id,
                question: question,
                choice: choice
              })
            }).then(function (response) {
              if (refused(response) && note) {
                note.textContent = "noted here only: " + choice +
                  " — sign in with GitHub to record it";
              }
            }).catch(function () {
              if (note) { note.textContent = "could not record " + choice + "; try again"; }
            });
          }
          if (reveal && answered.brief && answered.look) { reveal.hidden = false; }
        });
      });
    });
  }

  ready(function () {
    // First, before any request: the token /callback handed back in the
    // fragment, stored for this origin and stripped from the address bar.
    claimTokenFromHash();
    var sort = currentSort();
    if (sort !== DEFAULT_SORT) { carrySort(sort); }
    // Before applyFilters: this is what fills the box from ?q=, and the grid's
    // visibility is decided by the filters and that query together.
    wireSearch();
    applyFilters();
    applySort(sort);
    wireSort();
    loadConfig().then(function () {
      loadCounts();
      wireLike();
      wireCompare();
      loadMe();
    });
  });
})();
