/* The paged index, run for real (2026-09-23).
 *
 * The index holds one page of the gallery and marks its grid data-paged; the
 * pager is plain links. A sort, a search or ?all has to be over the whole
 * gallery, so gallery.js fetches cards.json, puts every card in the grid and
 * hides the pager. This loads the real script onto a two-card page whose
 * cards.json holds four, and checks:
 *
 *   1. a plain visit fetches nothing and leaves the page as it is;
 *   2. ?sort=oldest arrives as the whole gallery, oldest first, pager hidden;
 *   3. typing a search fetches once and matches cards not on this page;
 *   4. a cards.json that fails leaves the page's own cards where they were.
 *
 * Exits 0 and prints "ok N" on success; prints the failure and exits 1.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { makeWindow } = require("./dom.js");

const SCRIPT = path.join(__dirname, "..", "..", "sketchgen", "assets", "gallery.js");

let checks = 0;

function ok(condition, what) {
  checks += 1;
  if (!condition) {
    console.error("FAIL: " + what);
    process.exit(1);
  }
}

function equal(got, want, what) {
  ok(JSON.stringify(got) === JSON.stringify(want),
     what + " — got " + JSON.stringify(got) + ", wanted " + JSON.stringify(want));
}

function card(id, published, words) {
  return '<div class="card" data-entry="' + id + '" data-rules="treatment" ' +
    'data-executor="qwen" data-published="' + published + '" data-search="entry ' + id +
    " " + words + '"><p class="card-counts"><span class="count" data-count="likes">—</span></p></div>';
}

// Newest first, as the generator writes them.
const ALL = [
  card(4, "2026-09-23T04:00:00Z", "loom weaving"),
  card(3, "2026-09-23T03:00:00Z", "tide chart"),
  card(2, "2026-09-23T02:00:00Z", "dancing bears"),
  card(1, "2026-09-23T01:00:00Z", "loom of light")
];

function answer(payload, status) {
  const code = status || 200;
  return Promise.resolve({
    ok: code < 400,
    status: code,
    json: function () { return Promise.resolve(payload); }
  });
}

function load(search, cardsStatus) {
  const window = makeWindow();
  const document = window.document;
  window.location.search = search;
  window.location.pathname = "/index.html";
  const asked = [];
  window.fetch = function (url) {
    asked.push(String(url));
    if (String(url).indexOf("cards.json") !== -1) {
      return cardsStatus ? answer({}, cardsStatus) : answer({ cards: ALL });
    }
    return Promise.reject(new Error("offline"));
  };
  const form = document.createElement("form");
  const box = document.createElement("input");
  box.setAttribute("type", "search");
  box.setAttribute("data-search", "");
  box.value = "";
  form.appendChild(box);
  document.appendChild(form);
  const grid = document.createElement("div");
  grid.className = "grid";
  grid.setAttribute("data-paged", "1");
  grid.innerHTML = ALL.slice(0, 2).join("");
  document.appendChild(grid);
  const pager = document.createElement("nav");
  pager.className = "pager";
  pager.setAttribute("data-pager", "");
  document.appendChild(pager);
  window.SKETCHGEN_ROOT = "./";
  const context = vm.createContext({
    window: window, document: document, fetch: window.fetch,
    URLSearchParams: URLSearchParams, Promise: Promise, console: console,
    Math: Math, Number: Number, String: String, Object: Object, Array: Array,
    JSON: JSON, parseInt: parseInt, parseFloat: parseFloat, isNaN: isNaN,
    setTimeout: setTimeout, Error: Error
  });
  vm.runInContext(fs.readFileSync(SCRIPT, "utf8"), context, { filename: "gallery.js" });
  return { window, document, grid, pager, box, asked };
}

function ids(grid) {
  return grid.querySelectorAll(".card[data-entry]").map(function (el) {
    return el.getAttribute("data-entry");
  });
}

function shown(grid) {
  return grid.querySelectorAll(".card[data-entry]")
    .filter(function (el) { return !el.hidden; })
    .map(function (el) { return el.getAttribute("data-entry"); });
}

function settle() {
  return new Promise(function (resolve) { setTimeout(resolve, 0); });
}

async function main() {
  // 1. Browsing: nothing fetched, the page's two cards, the pager showing.
  let page = load("", 0);
  await settle(); await settle();
  equal(page.asked.filter(function (u) { return u.indexOf("cards.json") !== -1; }), [],
        "a plain visit asks for no cards.json");
  equal(ids(page.grid), ["4", "3"], "a plain visit keeps the page's own cards");
  ok(!page.pager.hidden, "a plain visit shows the pager");

  // 2. A sort in the address bar is a sort of everything.
  page = load("?sort=oldest", 0);
  await settle(); await settle(); await settle();
  equal(ids(page.grid), ["1", "2", "3", "4"], "?sort=oldest sorts the whole gallery");
  ok(page.pager.hidden, "the pager hides once the grid holds everything");
  ok(page.grid.getAttribute("data-paged") === undefined || page.grid.getAttribute("data-paged") === null,
     "the grid stops calling itself paged");

  // 3. Typing a search: one fetch, and a match that was on another page.
  page = load("", 0);
  await settle();
  page.box.value = "loom";
  page.box.dispatch("input");
  page.box.value = "loom w";
  page.box.dispatch("input");
  await settle(); await settle(); await settle();
  equal(page.asked.filter(function (u) { return u.indexOf("cards.json") !== -1; }).length, 1,
        "two keystrokes fetch cards.json once");
  equal(shown(page.grid), ["4"], "the search runs over every card, not this page's");
  page.box.value = "loom";
  page.box.dispatch("input");
  await settle();
  equal(shown(page.grid), ["4", "1"], "entry 1 is found though it was never on this page");

  // 4. A cards.json that fails: the page is left as it was, and still sorts.
  page = load("?sort=oldest", 404);
  await settle(); await settle(); await settle();
  equal(ids(page.grid), ["3", "4"], "a failed fetch sorts the page it has");
  ok(!page.pager.hidden, "a failed fetch leaves the pager for browsing");

  console.log("ok " + checks);
}

main().catch(function (err) { console.error("FAIL: " + (err && err.stack || err)); process.exit(1); });
