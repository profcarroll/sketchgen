/* gallery.js — the only script the published gallery loads.
 *
 * Three jobs, all of them read-or-write against the gallery write path
 * (packet 3.3), whose base URL comes from config.json and from nowhere else:
 *
 *   1. counts   GET  <base>/counts?ids=1,2,3   views and likes per entry
 *   2. like     POST <base>/like               one like, identified by GitHub
 *   3. vote     POST <base>/vote               one answer to one question
 *
 * Until that service exists, config.json carries an empty write_path: every
 * count stays an em dash, the like button stays disabled, and the compare page
 * says votes are not being recorded. The generator never writes a number it
 * does not have, and neither does this file.
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
    fetch(base() + "/counts?ids=" + encodeURIComponent(ids.join(",")), {
      credentials: "include"
    })
      .then(function (response) { return response.json(); })
      .then(function (data) { paintCounts(data && data.counts ? data.counts : data || {}); })
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
    button.disabled = false;
    button.addEventListener("click", function () {
      button.disabled = true;
      fetch(base() + "/like", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entry: Number(button.getAttribute("data-like")) })
      })
        .then(function (response) {
          if (response.status === 401 && login) {
            button.textContent = "sign in to like";
            button.disabled = false;
            return null;
          }
          return response.json();
        })
        .then(function (data) {
          if (!data) { return; }
          button.textContent = data.liked === false ? "like" : "liked";
          button.disabled = false;
          loadCounts();
        })
        .catch(function () { button.disabled = false; });
    });
  }

  /* ---- the grid's filters, which are plain links ----------------------- */

  function applyFilters() {
    var cards = document.querySelectorAll(".card[data-entry]");
    if (cards.length === 0) { return; }
    var params = new URLSearchParams(window.location.search);
    var rules = params.get("rules");
    var executor = params.get("executor");
    Array.prototype.forEach.call(cards, function (card) {
      var keep = (!rules || card.getAttribute("data-rules") === rules) &&
                 (!executor || card.getAttribute("data-executor") === executor);
      card.hidden = !keep;
    });
    Array.prototype.forEach.call(document.querySelectorAll("a.filter"), function (link) {
      var href = link.getAttribute("href") || "";
      var query = href.indexOf("?") === -1 ? "" : href.slice(href.indexOf("?"));
      var active = (query === "" && !rules && !executor) ||
                   (query !== "" && query === "?" + params.toString());
      if (active) { link.setAttribute("aria-current", "true"); }
    });
  }

  /* ---- the compare page ------------------------------------------------ */

  function embeddedEntries() {
    var node = document.getElementById("sketchgen-entries");
    if (!node) { return []; }
    try { return JSON.parse(node.textContent) || []; } catch (err) { return []; }
  }

  function pickPair(entries) {
    var params = new URLSearchParams(window.location.search);
    var byId = {};
    entries.forEach(function (entry) { byId[String(entry.id)] = entry; });
    var a = byId[String(params.get("a"))];
    var b = byId[String(params.get("b"))];
    if (a && b && a.id !== b.id) { return [a, b]; }
    var rest = entries.filter(function (entry) { return !a || entry.id !== a.id; });
    if (a && rest.length) { return [a, rest[0]]; }
    if (entries.length >= 2) { return [entries[0], entries[1]]; }
    return null;
  }

  function paintSide(side, entry) {
    var thumb = side.querySelector("[data-thumb]");
    var brief = side.querySelector("[data-brief]");
    if (thumb) {
      thumb.textContent = "";
      var img = document.createElement("img");
      img.src = ROOT + entry.strip;
      img.alt = "four frames from one of the two sketches";
      thumb.appendChild(img);
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
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                a: sides.A.id, b: sides.B.id, question: question, choice: choice
              })
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
    applyFilters();
    loadConfig().then(function () {
      loadCounts();
      wireLike();
      wireCompare();
    });
  });
})();
