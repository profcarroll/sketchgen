/* gallery.js — the only script the published gallery loads.
 *
 * Six jobs, all of them read-or-write against the gallery write path
 * (packet 3.3), whose base URL comes from config.json and from nowhere else:
 *
 *   0. view     POST <base>/view               one view of one entry page
 *   1. counts   GET  <base>/counts?entries=1,2,3   views and likes per entry
 *   2. like     POST <base>/like               one like, identified by GitHub
 *   3. vote     POST <base>/vote               one answer to one question
 *   4. prompt   POST <base>/prompt             a sketch a visitor asked for
 *   5. critique POST <base>/critique           a revision of one that exists
 *
 * The last two are submissions and not jobs: they wait in a table until the
 * operator releases them (plan §1.2), and both forms say so.
 *
 * Both writes need a session, and the gallery is a different origin from the
 * write path, so the session arrives as a token in the /callback redirect's
 * fragment, lives in this origin's localStorage, and rides back as a bearer
 * header. See "the session" below for why a cookie cannot do this job.
 *
 * Two keys in this origin's localStorage, and only one of them is this file's:
 * sketchgen_session, which it owns, and sketchgen-swipe-return, which swipe.js
 * writes and this file only ever reads — a one-shot note saying the sign-in
 * started on swipe.html and should end there (swipe.md §5).
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
 * And it greets somebody who arrived by scanning a projection: ?kiosk on an
 * entry page reveals a strip the generator already wrote and then leaves the
 * address bar, which asks nothing of anyone either (qr.md §6.2).
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
   * server, so it cannot land in a log or a Referer header.
   *
   * It says whether it actually claimed one, because returnFromSignIn() below
   * is allowed to run in that load and in no other. */
  function claimTokenFromHash() {
    var hash = window.location.hash || "";
    if (hash.indexOf("#session=") !== 0) { return false; }
    var token = decodeURIComponent(hash.slice("#session=".length));
    if (token) { writeToken(token); }
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    } else {
      window.location.hash = "";
    }
    return !!token;
  }

  var SWIPE_RETURN_KEY = "sketchgen-swipe-return";

  /* Coming back to the swipe page after signing in (swipe.md §5).
   *
   * <write_path>/callback returns to the gallery's front page and only there,
   * so a visitor who started on swipe.html lands here instead. Rather than
   * teach the Worker a return path — a column on the OAuth state table and a
   * deploy — swipe.js leaves a note in storage on its way to /login and this
   * reads it on the way back. One extra hop, no Worker change.
   *
   * A note in storage is not evidence of anything, so three rules, and each
   * one is a way for it not to be trusted:
   *
   *   consumed once — it is removed before it is acted on, so it sends
   *   somebody back exactly one time and never again;
   *
   *   only in the load that claimed a token — ready() calls this only when
   *   claimTokenFromHash() took one out of the fragment, so a stale note can
   *   never redirect somebody who typed the front page's address;
   *
   *   only to this page's own swipe.html — the pattern allows that filename
   *   and a query of plain characters, nothing more, so nothing in storage can
   *   send a visitor to another origin, another path, or a javascript: URL.
   *
   * Anything else is dropped unread. */
  function returnFromSignIn() {
    var note;
    try {
      note = window.localStorage.getItem(SWIPE_RETURN_KEY) || "";
      window.localStorage.removeItem(SWIPE_RETURN_KEY);
    } catch (err) { return; /* private mode: there was no note to begin with */ }
    if (!/^swipe\.html(\?[A-Za-z0-9=&_.-]*)?$/.test(note)) { return; }
    window.location.replace(ROOT + note);
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

  /* `me` is the /me payload when there is one: the username the caller has
   * already read out of it, and the day's remaining budget the composer shows.
   * Every other caller passes nothing, which paints a signed-out page. */
  function paintSession(username, me) {
    // More than one of these now: the like button's, the composer's and the
    // critique form's all offer the same sign-in and all take the same href.
    Array.prototype.forEach.call(document.querySelectorAll("[data-login]"), function (login) {
      if (base()) { login.setAttribute("href", base() + "/login"); }
      login.hidden = !!username;
    });
    paintCompose(username, me);
    paintCritique(username);
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
        paintSession(username, data);
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

  /* The write path answers one statement per ask, and its database binds at
   * most a hundred parameters to a statement. The grid passed that many entries
   * long ago, so the ids go over in batches of a hundred: a gallery of any size
   * gets its counts, and one batch that fails leaves the others painted. */
  var COUNTS_BATCH = 100;

  function fetchCounts(ids) {
    return fetch(base() + "/counts?entries=" + encodeURIComponent(ids.join(",")), {
      credentials: "include",
      headers: authHeaders()
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        paintCounts(data && data.counts ? data.counts : data || {});
      })
      .catch(function () { /* leave this batch's em dashes where they are */ });
  }

  function loadCounts() {
    var ids = entryIds();
    if (!base() || ids.length === 0) { return; }
    var batches = [];
    for (var at = 0; at < ids.length; at += COUNTS_BATCH) {
      batches.push(fetchCounts(ids.slice(at, at + COUNTS_BATCH)));
    }
    // The numbers the "most liked" order sorts on have only just arrived, and
    // only the last batch makes that order right.
    return Promise.all(batches).then(function () {
      if (currentSort() === "liked") { applySort("liked"); }
    });
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

  /* ---- asking for a sketch, and asking for a revision ------------------ */

  /* The two forms of plan §5: a prompt on the gallery index, a critique on an
   * entry page. They are the only places this script sends anything a person
   * typed, and they hold no more than the like button does — the same bearer
   * token, the same /me, no credential in the page.
   *
   * The generator writes neither block unless config.json names a write path,
   * and writes every state of the one it does write, hidden. This file reveals
   * exactly one of them, and only once /me has answered: a form that appears
   * before the page knows who is looking invites a stranger to type something
   * it is about to refuse.
   *
   * Neither submission is a job. The Worker files it in a table, the node
   * pulls it down, and it runs when the operator releases it — which is what
   * the note under each button and the receipt after each one say out loud.
   */

  function composeBlock() { return document.querySelector("[data-compose]"); }
  function critiqueBlock() { return document.querySelector("[data-critique]"); }

  /* Whitespace collapsed and trimmed, exactly as lineage.validate does it
   * before it counts anything: the page must count the same words. */
  function collapse(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/\s+/g, " ")
      .trim();
  }

  /* "2 of 3 left today" — the denominator is in the HTML because it is the
   * Worker's cap and not this page's; the numerator arrives from /me, and the
   * line stays hidden until it has. */
  function paintQuota(slot, left) {
    if (!slot) { return; }
    if (typeof left !== "number") { slot.hidden = true; return; }
    slot.hidden = false;
    var value = slot.querySelector("[data-left]");
    if (value) { value.textContent = String(left); }
  }

  /* Once a form has been submitted its receipt stands: nothing repaints it
   * back into an empty box the visitor might fill in twice. */
  function done(block) { return block.getAttribute("data-done") === "true"; }

  function paintCompose(username, me) {
    var block = composeBlock();
    if (!block || !base() || done(block)) { return; }
    var out = block.querySelector("[data-compose-out]");
    var form = block.querySelector("[data-compose-in]");
    var left = me && typeof me.prompts_left === "number" ? me.prompts_left : null;
    if (out) { out.hidden = !!username; }
    if (form) { form.hidden = !username; }
    paintQuota(block.querySelector("[data-compose-quota]"), username ? left : null);
    var button = block.querySelector("[data-compose-send]");
    if (button) { button.disabled = left === 0; }
    block.hidden = false;
  }

  function paintCritique(username) {
    var block = critiqueBlock();
    if (!block || !base() || done(block)) { return; }
    var out = block.querySelector("[data-critique-out]");
    var form = block.querySelector("[data-critique-in]");
    if (out) { out.hidden = !!username; }
    if (form) { form.hidden = !username; }
    block.hidden = false;
  }

  /* One answer, whatever it was: the status, and whatever JSON came with it.
   * A refusal whose body this page cannot read is still a refusal. */
  function answered(response) {
    var status = response.status;
    return response.json()
      .catch(function () { return {}; })
      .then(function (data) { return { status: status, data: data || {} }; });
  }

  /* lineage.validate(), in the page.
   *
   * The same cases in the same order and the same thresholds as
   * sketchgen/lineage.py: empty, a code mark, more than one sentence, forty
   * words or more. The sentences are shorter here because this line is read
   * while someone is typing; the Worker refuses the same text again in
   * Python's longer words, and sync.py refuses it a third time on the way in.
   *
   * CODE_MARKS is lineage._CODE_MARKS, in its order, so the two can be read
   * side by side. */
  var CODE_MARKS = ["```", "{", "}", ";", "()", "=>", "function ", "<script", "//", "$"];
  var MAX_CRITIQUE_WORDS = 40;

  /* Python splits on lineage._SENTENCE_SPLIT_RE — a terminator, then space —
   * and drops the empties. Its lookbehind is counted out here rather than
   * written: a regex lookbehind is a syntax error in a browser too old for it,
   * and a syntax error anywhere in this file takes the whole file down, the
   * counts and the like button and the sort with it. The text is already
   * collapsed, so every split point is one space after a terminator. */
  function sentenceCount(text) {
    var count = text ? 1 : 0;
    for (var at = 1; at < text.length; at += 1) {
      if (text.charAt(at) === " " && ".!?".indexOf(text.charAt(at - 1)) !== -1) {
        count += 1;
      }
    }
    return count;
  }

  function critiqueVerdict(raw) {
    var text = collapse(raw);
    if (!text) { return { ok: false, text: "", says: "say something" }; }
    for (var at = 0; at < CODE_MARKS.length; at += 1) {
      if (text.indexOf(CODE_MARKS[at]) !== -1) {
        return {
          ok: false,
          text: text,
          says: "holds code (" + CODE_MARKS[at] +
            ") — a critique becomes a prompt, not a patch"
        };
      }
    }
    var sentences = sentenceCount(text);
    if (sentences > 1) {
      return { ok: false, text: text, says: sentences + " sentences — one is the contract" };
    }
    var words = text.split(" ").length;
    if (words >= MAX_CRITIQUE_WORDS) {
      return {
        ok: false,
        text: text,
        says: words + " words — under " + MAX_CRITIQUE_WORDS + " is the contract"
      };
    }
    return { ok: true, text: text, says: "one sentence · " + words + " words · no code" };
  }

  function wireCompose() {
    var block = composeBlock();
    if (!block || !base()) { return; }
    var field = block.querySelector("[data-compose-text]");
    var button = block.querySelector("[data-compose-send]");
    var form = block.querySelector("[data-compose-in]");
    var receipt = block.querySelector("[data-compose-receipt]");
    var refusal = block.querySelector("[data-compose-refusal]");
    if (!field || !button) { return; }

    function refuse(says) {
      if (!refusal) { return; }
      refusal.textContent = says || "";
      refusal.hidden = !says;
    }

    button.addEventListener("click", function () {
      var text = collapse(field.value);
      if (!text) { refuse("say something"); return; }
      refuse("");
      button.disabled = true;
      fetch(base() + "/prompt", {
        method: "POST",
        credentials: "include",
        headers: authHeaders({ "Content-Type": "application/json" }),
        // worker.js routePrompt: { prompt } — one sentence, nothing else.
        body: JSON.stringify({ prompt: text })
      })
        .then(function (response) {
          if (refused(response)) {
            refuse("sign in with GitHub to submit a prompt");
            return null;
          }
          return answered(response);
        })
        .then(function (answer) {
          if (!answer) { return; }
          var left = typeof answer.data.prompts_left === "number"
            ? answer.data.prompts_left
            : (answer.status === 429 ? 0 : null);
          paintQuota(block.querySelector("[data-compose-quota]"), left);
          if (answer.status === 200 && answer.data.ok) {
            block.setAttribute("data-done", "true");
            if (form) { form.hidden = true; }
            if (receipt) { receipt.hidden = false; }
            return;
          }
          // 429 is the day's budget spent and nothing to try again; anything
          // else is the Worker's own sentence about the text, in the words
          // lineage.validate would have used.
          refuse(answer.data.error || "that was not queued; try again");
          button.disabled = answer.status === 429;
        })
        .catch(function () {
          refuse("could not queue that; try again");
          button.disabled = false;
        });
    });
  }

  function wireCritique() {
    var block = critiqueBlock();
    if (!block || !base()) { return; }
    var field = block.querySelector("[data-critique-text]");
    var button = block.querySelector("[data-critique-send]");
    var rule = block.querySelector("[data-critique-rule]");
    var echo = block.querySelector("[data-critique-echo]");
    var form = block.querySelector("[data-critique-in]");
    var sent = block.querySelector("[data-critique-sent]");
    var sentEcho = block.querySelector("[data-critique-echo-sent]");
    if (!field || !button) { return; }

    /* The rules line, and the child's prompt under it, repainted on every
     * keystroke: the contract is worth reading before the round trip, not
     * after it. */
    function check() {
      var verdict = critiqueVerdict(field.value);
      if (echo) { echo.textContent = verdict.text || "…"; }
      if (rule) {
        rule.className = verdict.ok ? "rule good" : "rule bad";
        rule.textContent = verdict.says;
      }
      // A budget the Worker has already said is spent stays spent: retyping
      // does not buy another one.
      button.disabled = !verdict.ok || block.getAttribute("data-spent") === "true";
      return verdict;
    }

    field.addEventListener("input", check);
    check();

    button.addEventListener("click", function () {
      var verdict = check();
      if (!verdict.ok) { return; }
      button.disabled = true;
      fetch(base() + "/critique", {
        method: "POST",
        credentials: "include",
        headers: authHeaders({ "Content-Type": "application/json" }),
        // worker.js routeCritique: { entry_id, critique } — the parent and the
        // sentence; who asked is the session's business, not the body's.
        body: JSON.stringify({
          entry_id: Number(block.getAttribute("data-critique")),
          critique: verdict.text
        })
      })
        .then(function (response) {
          if (refused(response)) {
            if (rule) {
              rule.className = "rule bad";
              rule.textContent = "sign in with GitHub to ask for a revision";
            }
            return null;
          }
          return answered(response);
        })
        .then(function (answer) {
          if (!answer) { return; }
          if (answer.status === 200 && answer.data.ok) {
            block.setAttribute("data-done", "true");
            if (sentEcho) { sentEcho.textContent = verdict.text; }
            if (form) { form.hidden = true; }
            if (sent) { sent.hidden = false; }
            return;
          }
          if (rule) {
            rule.className = "rule bad";
            rule.textContent = answer.data.error || "that was not queued; try again";
          }
          if (answer.status === 429) { block.setAttribute("data-spent", "true"); }
          button.disabled = answer.status === 429;
        })
        .catch(function () {
          if (rule) {
            rule.className = "rule bad";
            rule.textContent = "could not queue that; try again";
          }
          button.disabled = false;
        });
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
  var SORTS = ["newest", "oldest", "random", "liked", "reviewed", "controversial", "consensus"];
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

  /* The total number of paired comparisons this entry has been in, summed
   * across all populations and questions. Read from the standing marks' title
   * attributes, which carry e.g. "agents: 48th of 63 · 0.52 over 1 pair". */
  function pairsOf(card) {
    var total = 0;
    Array.prototype.forEach.call(
      card.querySelectorAll(".standing .mark:not(.inline)"),
      function (mark) {
        var title = mark.getAttribute("title") || "";
        var match = title.match(/over (\d+) pairs?/);
        if (match) { total += parseInt(match[1], 10); }
      }
    );
    return total;
  }

  /* The percentile gap between the human and agent marks on the "rather look
   * at it" track (the first bar-row). Returns -1 when either population is
   * missing, so entries with no comparison data sort to the end. */
  function gapOf(card) {
    var rows = card.querySelectorAll(".bar-row");
    if (rows.length === 0) { return -1; }
    var lookRow = rows[0];
    var human = lookRow.querySelector(".mark.human");
    var agent = lookRow.querySelector(".mark.agent");
    if (!human || !agent) { return -1; }
    var humanPct = parseFloat(human.style.left) / 100;
    var agentPct = parseFloat(agent.style.left) / 100;
    if (isNaN(humanPct) || isNaN(agentPct)) { return -1; }
    return Math.abs(humanPct - agentPct);
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

  /* Most reviewed: entries with the most paired comparisons first. Entries
   * with no pairs sort to the end and are ordered newest first among themselves. */
  function byReviewed(a, b) {
    var diff = pairsOf(b) - pairsOf(a);
    return diff !== 0 ? diff : byNewest(a, b);
  }

  /* Most controversial: the biggest gap between humans and agents first.
   * Entries without both populations' marks sort to the end. */
  function byControversial(a, b) {
    var ga = gapOf(a);
    var gb = gapOf(b);
    // Entries without gap data sort last.
    if (ga < 0 && gb < 0) { return byNewest(a, b); }
    if (ga < 0) { return 1; }
    if (gb < 0) { return -1; }
    var diff = gb - ga;
    return diff !== 0 ? diff : byNewest(a, b);
  }

  /* Most consensus: the smallest gap between humans and agents first.
   * Entries without both populations' marks sort to the end. */
  function byConsensus(a, b) {
    var ga = gapOf(a);
    var gb = gapOf(b);
    if (ga < 0 && gb < 0) { return byNewest(a, b); }
    if (ga < 0) { return 1; }
    if (gb < 0) { return -1; }
    var diff = ga - gb;
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
    else if (name === "reviewed") { cards.sort(byReviewed); }
    else if (name === "controversial") { cards.sort(byControversial); }
    else if (name === "consensus") { cards.sort(byConsensus); }
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

  function isRejection(entry) {
    // An older page may predate the state key; absent means published, which is
    // what every entry on the compare page was before rejections arrived here.
    return !!entry && entry.state === "failed-kept";
  }

  function oneOf(list) { return list[Math.floor(Math.random() * list.length)]; }

  /* The status quo a rejection is judged against: the published entries, minus
   * whichever one is already on the other side. */
  function publishedPool(entries, not) {
    return entries.filter(function (entry) {
      return !isRejection(entry) && (!not || entry.id !== not.id);
    });
  }

  function pickPair(entries) {
    var params = new URLSearchParams(window.location.search);
    var byId = {};
    entries.forEach(function (entry) { byId[String(entry.id)] = entry; });
    var a = byId[String(params.get("a"))];
    var b = byId[String(params.get("b"))];
    // Both named: honoured whatever their states. A link that says exactly
    // which two to compare is a person's request, not the balanced offer.
    if (a && b && a.id !== b.id) { return [a, b]; }

    var offered = offeredPairs().filter(function (pair) {
      var left = byId[String(pair.a)];
      var right = byId[String(pair.b)];
      return left && right && left.id !== right.id &&
             (!a || left.id === a.id || right.id === a.id);
    });
    if (offered.length) {
      var choice = oneOf(offered);
      var first = byId[String(choice.a)];
      var second = byId[String(choice.b)];
      // An entry page links here with ?a=<itself>; keep that entry on the left.
      if (a && second.id === a.id) { return [second, first]; }
      return [first, second];
    }

    /* No offered pair contains `a` — which is every kept rejection, because the
     * balanced offer is published-only (spec §9). Judge it against the status
     * quo: a published entry drawn at random, never `a` itself and never
     * another rejection. Random, not the first entry in the list: taking
     * rest[0] sent every visitor who followed a rejection's link to the same
     * partner, so the rejection's score was a verdict on one comparison. */
    var pool = publishedPool(entries, a);
    if (a) { return pool.length ? [a, oneOf(pool)] : null; }
    if (pool.length >= 2) { return [pool[0], pool[1]]; }
    return null;
  }

  /* A rejection is named in the reveal and nowhere else. Spec §5 blinds the
   * human until both answers are in, and "the gate threw this one out" is the
   * loudest anchor the page could hand them, so neither side carries a REJECTED
   * chip while they are looking: a rejection is just a sketch until they vote.
   * This block sits inside [data-reveal], so it is written at load and stays
   * hidden with the rest of it. Nothing at all when neither side is one. */
  function paintRejected(host, sides) {
    if (!host) { return; }
    host.textContent = "";
    ["A", "B"].forEach(function (letter) {
      if (!isRejection(sides[letter])) { return; }
      var line = document.createElement("p");
      line.className = "rejected-note";
      line.textContent = letter + " was rejected by the gate and kept";
      host.appendChild(line);
    });
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
   * The compare page and the entry page's ledger both need exactly this, so it
   * lives here rather than inside wireCompare: one toggle, one rule about what
   * may be playing, one sandbox. Every caller hands over a host element that
   * holds a [data-play] button, the src of the sketch page, and the words it
   * wants spoken; nothing else about the two pages is shared.
   *
   * This is the whole of the toggle's state: the host element whose sketch is
   * playing, or null. Stopping REMOVES the iframe rather than hiding it —
   * display:none does not unload a document, so a hidden frame keeps drawing
   * and keeps its AudioContext open. */
  var running = null;

  function runNote(host, text) {
    var note = host.querySelector("[data-run-note]");
    if (note) { note.textContent = text; }
  }

  function stopSketch() {
    if (!running) { return; }
    var state = running;
    running = null;
    var frame = state.host.querySelector("iframe.sketch");
    if (frame) { frame.remove(); }
    var button = state.button;
    if (button) {
      button.className = "play";
      button.setAttribute("aria-label", "run " + state.name);
      var label = button.querySelector("[data-play-label]");
      if (label) { label.textContent = ""; }
    }
    runNote(state.host, state.idle);
  }

  function startSketch(host, src, options) {
    var opts = options || {};
    if (opts.mic) {
      // A listening sketch cannot reach the microphone inside our sandboxed
      // frame — an opaque origin with no allow="microphone" is refused it — so
      // it runs in its own tab, where the Pages origin is a secure context the
      // browser will grant. Making sound is not this; only listening leaves.
      window.open(src, "_blank", "noopener");
      runNote(host, "opened in a new tab — the microphone works there");
      return;
    }
    stopSketch();
    var mount = opts.mount || host;
    var button = host.querySelector("[data-play]");
    if (!button) { return; }
    var name = opts.name || "this sketch";
    var frame = document.createElement("iframe");
    frame.className = "sketch";
    frame.src = src;
    frame.title = name + " running";
    // Exactly the entry page's sandbox, so a sketch behaves the same wherever
    // it is run: scripts yes, and nothing else — no same-origin, no forms, no
    // top navigation. p5.sound still works; the viewer's click is the gesture
    // that lets the frame start audio.
    frame.setAttribute("sandbox", "allow-scripts");
    mount.insertBefore(frame, button);
    button.className = "play running";
    button.setAttribute("aria-label", "stop " + name);
    var label = button.querySelector("[data-play-label]");
    if (label) { label.textContent = "stop"; }
    running = {
      host: host,
      button: button,
      name: name,
      idle: opts.idle === undefined ? "" : opts.idle
    };
    runNote(host, opts.note === undefined ? "" : opts.note);
  }

  /** Make one [data-play] button start and stop one sketch in its own box. */
  function runInPlace(host, src, options) {
    return function () {
      if (running && running.host === host) { stopSketch(); }
      else { startSketch(host, src, options); }
    };
  }

  function paintSide(side, entry) {
    var thumb = side.querySelector("[data-thumb]");
    var brief = side.querySelector("[data-brief]");
    if (thumb) {
      if (running && running.host === side) { stopSketch(); }
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
        // The same helper the ledger's tiles use, with the compare page's own
        // words: the side is the host, the thumbnail is where the frame goes.
        button.addEventListener("click", runInPlace(side, ROOT + entry.href + "sketch/", {
          mount: thumb,
          name: "sketch " + side.getAttribute("data-side"),
          idle: "click to run",
          note: "running · click to stop"
        }));
        thumb.appendChild(button);
        runNote(side, "click to run");
      }
    }
    if (brief) { brief.textContent = entry.brief || entry.prompt || "(no brief recorded)"; }
  }

  /* ---- the ledger ------------------------------------------------------ */

  /* The entry page's lineage panel. Its ancestry is static HTML: it was true
   * when the entry was published and it stays true. Everything below this
   * entry is not — a sibling or a child can arrive a month after the page was
   * rendered — so the server leaves those containers empty and they are
   * painted here from lineage.json, which render_index rewrites on every push.
   * With no script the page keeps the ancestry and the plain children line. */

  function ledgerNumber(a, b) { return Number(a) - Number(b); }

  function generationOf(item) {
    // Whatever the database recorded, printed as it stands; see the note on
    // gallery._generation_label for why a root and its first child agree.
    return (item && item.generation) || 1;
  }

  function criticChip(item, isRoot) {
    var who = String((isRoot ? (item || {}).submitted_by : (item || {}).critique_by) || "").trim();
    if (!who) { return null; }
    var chip = document.createElement("span");
    var model = who.indexOf(":") >= 0;
    chip.className = "chip " + (model ? "model" : "human");
    chip.textContent = model ? who.split(":")[0] : who;
    return chip;
  }

  function plainChip(text) {
    var chip = document.createElement("span");
    chip.className = "chip unpublished";
    chip.textContent = text;
    return chip;
  }

  function ledgerTile(id, item, width) {
    var tile = document.createElement("div");
    var public_ = !!(item && item.public);
    tile.className = "ledger-tile " + width + (public_ ? "" : " blank");
    if (!public_) {
      tile.setAttribute("aria-hidden", "true");
      return tile;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className = "play";
    button.setAttribute("data-play", "");
    button.setAttribute("aria-label", "run entry " + id);
    var img = document.createElement("img");
    img.src = "../" + id + "/strip.png";
    img.loading = "lazy";
    img.alt = "the first frame of entry " + id;
    button.appendChild(img);
    // Empty while the frame is showing, "stop" while the sketch runs, so one
    // control does both without the row changing height.
    var label = document.createElement("span");
    label.setAttribute("data-play-label", "");
    button.appendChild(label);
    button.addEventListener("click", runInPlace(tile, "../" + id + "/sketch/", {
      name: "entry " + id,
      mic: !!(item && item.mic)
    }));
    tile.appendChild(button);
    return tile;
  }

  function ledgerText(id, item, options) {
    var opts = options || {};
    var text = document.createElement("div");
    text.className = "ledger-text";
    var critique = String((item || {}).critique || "").trim();
    if (critique) {
      var line = document.createElement("p");
      line.className = "ledger-critique" + (opts.clamp ? " clamp" : "");
      var revise = document.createElement("span");
      revise.className = "revise";
      revise.textContent = "Revise:";
      line.appendChild(revise);
      line.appendChild(document.createTextNode(" "));
      var em = document.createElement("em");
      em.textContent = critique;
      line.appendChild(em);
      text.appendChild(line);
    }
    var meta = document.createElement("p");
    meta.className = "ledger-meta";
    if (item && item.public) {
      var link = document.createElement("a");
      link.href = "../" + id + "/";
      link.textContent = "entry " + id;
      meta.appendChild(link);
    } else {
      meta.appendChild(document.createTextNode("entry " + id));
    }
    meta.appendChild(document.createTextNode(" · generation " + generationOf(item)));
    if (opts.tail) { meta.appendChild(document.createTextNode(" · " + opts.tail)); }
    var chip = criticChip(item, false);
    if (chip) {
      meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(chip);
    }
    if (!(item && item.public)) {
      meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(plainChip("not published"));
    }
    text.appendChild(meta);
    return text;
  }

  function ledgerRow(id, item, options) {
    var opts = options || {};
    var row = document.createElement(opts.card ? "div" : "li");
    row.className = opts.card ? "ledger-card" : "ledger-row";
    row.appendChild(ledgerTile(id, item, opts.card ? "wide" : "narrow"));
    row.appendChild(ledgerText(id, item, opts));
    return row;
  }

  function moreLine(text, root) {
    var line = document.createElement("p");
    line.className = "ledger-more";
    var link = document.createElement("a");
    link.href = "../../lines/" + root + ".html";
    link.textContent = text;
    line.appendChild(link);
    return line;
  }

  function countOf(n, one, many) { return n + " " + (n === 1 ? one : many); }

  function paintForks(panel, entries, id, mine) {
    var host = panel.querySelector("[data-ledger-forks]");
    if (!host || mine.parent === null || mine.parent === undefined) { return; }
    var parent = entries[String(mine.parent)] || {};
    var siblings = (parent.children || [])
      .filter(function (kid) { return String(kid) !== String(id); })
      .sort(ledgerNumber);
    if (!siblings.length) { return; }
    host.textContent = "";
    var label = document.createElement("p");
    label.className = "ledger-fork-label";
    label.textContent = "also from entry " + mine.parent;
    host.appendChild(label);
    var list = document.createElement("ol");
    list.className = "ledger";
    siblings.slice(0, 4).forEach(function (kid) {
      var item = entries[String(kid)];
      var kids = ((item || {}).children || []).length;
      list.appendChild(ledgerRow(kid, item, {
        tail: kids ? countOf(kids, "child", "children") : ""
      }));
    });
    host.appendChild(list);
    if (siblings.length > 4) {
      host.appendChild(moreLine(
        "…and " + (siblings.length - 4) + " more from entry " + mine.parent,
        mine.root
      ));
    }
    host.hidden = false;
  }

  function descendantsOf(entries, id) {
    var out = [];
    var seen = {};
    var queue = ((entries[String(id)] || {}).children || []).slice();
    while (queue.length) {
      var next = queue.shift();
      if (seen[String(next)]) { continue; }
      seen[String(next)] = true;
      out.push(next);
      queue = queue.concat(((entries[String(next)] || {}).children || []).slice());
    }
    return out;
  }

  function paintDescendants(panel, entries, id, mine) {
    var after = panel.querySelector("[data-ledger-after]");
    var grid = panel.querySelector("[data-ledger-tiles]");
    var plain = panel.querySelector("[data-ledger-plain]");
    var kids = (mine.children || []).slice().sort(ledgerNumber);
    var all = descendantsOf(entries, id);
    if (!all.length) {
      if (after) { after.textContent = "No children yet."; }
      return;
    }
    var generations = {};
    all.forEach(function (one) {
      generations[String(generationOf(entries[String(one)]))] = true;
    });
    if (after) {
      after.textContent = "After this entry: " + countOf(kids.length, "child", "children") +
        ", " + countOf(Object.keys(generations).length, "generation", "generations") +
        " so far";
    }
    if (!grid) { return; }
    // Direct children first, then the rest of the line in generation order:
    // the question a reader has is "what came of this one", and the answer
    // starts with the entries that came of it directly.
    var rest = all.filter(function (one) { return kids.indexOf(one) < 0; });
    rest.sort(function (a, b) {
      var ga = generationOf(entries[String(a)]);
      var gb = generationOf(entries[String(b)]);
      return ga === gb ? ledgerNumber(a, b) : ga - gb;
    });
    var order = kids.concat(rest);
    grid.textContent = "";
    order.slice(0, 8).forEach(function (one) {
      grid.appendChild(ledgerRow(one, entries[String(one)], { card: true, clamp: true }));
    });
    grid.hidden = false;
    if (order.length > 8) {
      grid.parentNode.insertBefore(
        moreLine("…and " + (order.length - 8) + " more in the line", mine.root),
        grid.nextSibling
      );
    }
    // The script has painted what this line said in words; the words would
    // only repeat it.
    if (plain) { plain.hidden = true; }
  }

  function paintDepth(panel, entries, mine, id) {
    var deepest = 1;
    Object.keys(entries).forEach(function (key) {
      if (String(entries[key].root) === String(mine.root)) {
        deepest = Math.max(deepest, generationOf(entries[key]));
      }
    });
    var slot = document.querySelector("[data-ledger-deepest]");
    if (slot) {
      slot.textContent = String(deepest);
      return;
    }
    // A root that had no children when its page was rendered and has some now:
    // there is no number in the heading to replace, so the phrase changes.
    var head = document.querySelector("[data-ledger-head]");
    if (head && deepest > 1 && String(mine.root) === String(id)) {
      head.textContent = "the root of a line " + deepest + " generations deep";
    }
  }

  function paintLedger(panel, id, entries) {
    var mine = entries[String(id)];
    if (!mine) { return; }
    paintDepth(panel, entries, mine, id);
    paintForks(panel, entries, id, mine);
    paintDescendants(panel, entries, id, mine);
  }

  /** Every play button the server wrote: the ledger's tiles, and the stage of
   *  an entry too expensive to start itself. */
  function wireRunButtons(scope) {
    Array.prototype.forEach.call(scope.querySelectorAll("[data-run-href]"), function (button) {
      if (button.getAttribute("data-run-wired")) { return; }
      button.setAttribute("data-run-wired", "1");
      var host = button.parentNode;
      var mic = !!button.getAttribute("data-run-mic");
      button.addEventListener("click", runInPlace(host, button.getAttribute("data-run-href"), {
        name: button.getAttribute("data-run-name") || "this sketch",
        idle: mic ? "opens in a tab" : "click to run",
        note: "running · click to stop",
        mic: mic
      }));
      // Only now that something will happen when it is clicked.
      runNote(host, mic ? "opens in a tab (needs the microphone)" : "click to run");
    });
  }

  function wireLedger() {
    wireRunButtons(document);
    var panel = document.querySelector("[data-ledger]");
    if (!panel) { return; }
    var id = panel.getAttribute("data-ledger");
    fetch(ROOT + "lineage.json")
      .then(function (response) { return response.json(); })
      .then(function (data) { paintLedger(panel, id, (data && data.entries) || {}); })
      .catch(function () { /* the ancestry and the plain children line stand */ });
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
    paintRejected(page.querySelector("[data-rejected-note]"), sides);
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

  /* Somebody who arrived by scanning a projection (qr.md §6.2).
   *
   * The kiosk's QR carries ?kiosk and the entry page's does not, so this fires
   * for exactly one kind of visitor: a stranger standing in front of a wall
   * who took out a phone. The strip it reveals is markup the generator wrote,
   * hidden, linking controls this page already has — nothing here is built
   * with innerHTML out of a URL parameter.
   *
   * params.has, not params.get: a bare key parses to "", which is falsy, and
   * a version of this that silently never fires would be worse than one that
   * does not exist. ?kiosk, ?kiosk= and ?kiosk=1 all count, which is what a
   * QR reader that normalises a bare key might hand us.
   *
   * Then the param goes, immediately. claimTokenFromHash() is the precedent
   * and the reason is the same: an address bar people copy from should not
   * carry provenance, so a scanner who texts the link to a friend does not
   * pass a projection's along with it. Any other parameter survives. */
  function greetAScan() {
    var main = document.querySelector("main.entry[data-entry]");
    var strip = document.querySelector("[data-scanned]");
    var params = new URLSearchParams(window.location.search);
    var revision = document.querySelector("[data-scanned-critique]");
    var query;
    if (!main || !strip || !params.has("kiosk")) { return; }
    // A verb the gallery cannot honour is worse than one it never offered:
    // with no write path in config.json the generator writes no critique
    // panel at all, so the third link would point at nothing. Its separator
    // is inside the same span and goes with it. This is the rule the kiosk's
    // own third line follows (qr.md §5.3).
    if (revision && !document.getElementById("critique-text")) {
      revision.parentNode.removeChild(revision);
    }
    strip.hidden = false;
    if (!window.history || !window.history.replaceState) { return; }
    params.delete("kiosk");
    query = params.toString();
    window.history.replaceState(
      null, "", window.location.pathname + (query ? "?" + query : "") + window.location.hash
    );
  }

  /* One view per entry page, sent once the write path's base is known. Scoped
   * to main.entry so the grid, with hundreds of [data-entry] cards, sends none.
   * The Worker de-duplicates a signed-in viewer for 60 s; a view that does not
   * land is not an error. */
  function sendView() {
    var main = document.querySelector("main.entry[data-entry]");
    if (!main || !base()) { return; }
    var id = Number(main.getAttribute("data-entry"));
    if (!id) { return; }
    fetch(base() + "/view", {
      method: "POST",
      credentials: "include",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ entry_id: id })
    }).catch(function () {});
  }

  ready(function () {
    // First, before any request: the token /callback handed back in the
    // fragment, stored for this origin and stripped from the address bar. A
    // load that claimed one may be somebody on their way back to the swipe
    // page, and no other load is (swipe.md §5).
    if (claimTokenFromHash()) { returnFromSignIn(); }
    // And the other parameter this page takes out of the address bar as soon
    // as it has read it: the projection's ?kiosk (qr.md §6.2).
    greetAScan();
    var sort = currentSort();
    if (sort !== DEFAULT_SORT) { carrySort(sort); }
    // Before applyFilters: this is what fills the box from ?q=, and the grid's
    // visibility is decided by the filters and that query together.
    wireSearch();
    applyFilters();
    applySort(sort);
    wireSort();
    loadConfig().then(function () {
      sendView();
      loadCounts();
      wireLike();
      // Both forms are wired before loadMe answers and revealed only by it.
      wireCompose();
      wireCritique();
      wireCompare();
      wireLedger();
      loadMe();
    });
  });
})();
