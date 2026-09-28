/* A local render's picks, run for real (docs/plans/local-gallery.md §1.7).
 *
 * render-local writes "local" into config.json; the site's config never has
 * it. This loads the real gallery.js onto a two-card grid and an entry page,
 * under both configs, and checks:
 *
 *   1. with "local", a banner names the render and every card gets a toggle;
 *   2. a click picks, a second click unpicks, and the list is kept under the
 *      render's own name;
 *   3. Copy picked ids writes one id per line, ascending, newline-terminated;
 *   4. the site's calls are not made: no /view, no /me, only /counts;
 *   5. without "local", nothing of it appears.
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

function answer(payload) {
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(payload); } });
}

function card(id) {
  return '<div class="card" data-entry="' + id + '" data-published="" data-search="entry ' + id +
    '"><p class="card-counts"><span class="count" data-count="views">—</span></p></div>';
}

function load(config, entryPage) {
  const window = makeWindow();
  const document = window.document;
  window.location.pathname = entryPage ? "/e/4/" : "/index.html";
  const asked = [];
  const options = [];
  window.fetch = function (url, init) {
    asked.push(String(url));
    options.push({ url: String(url), init: init });
    if (String(url).indexOf("config.json") !== -1) { return answer(config); }
    if (String(url).indexOf("/counts") !== -1) { return answer({}); }
    return Promise.reject(new Error("offline"));
  };
  if (entryPage) {
    const main = document.createElement("main");
    main.className = "entry";
    main.setAttribute("data-entry", "4");
    document.body.appendChild(main);
  } else {
    const grid = document.createElement("div");
    grid.className = "grid";
    grid.innerHTML = card(4) + card(1);
    document.body.appendChild(grid);
  }
  window.SKETCHGEN_ROOT = "./";
  const context = vm.createContext({
    window: window, document: document, fetch: window.fetch, navigator: window.navigator,
    URLSearchParams: URLSearchParams, Promise: Promise, console: console,
    Math: Math, Number: Number, String: String, Object: Object, Array: Array,
    JSON: JSON, parseInt: parseInt, parseFloat: parseFloat, isNaN: isNaN,
    setTimeout: setTimeout, Error: Error
  });
  vm.runInContext(fs.readFileSync(SCRIPT, "utf8"), context, { filename: "gallery.js" });
  return { window, document, asked, options };
}

function settle() {
  return new Promise(function (resolve) { setTimeout(resolve, 0); });
}

function click(page, el) {
  page.document.dispatch("click", { target: el });
}

async function main() {
  const LOCAL = { local: "sld-gpu", write_path: "https://write.example.invalid" };

  // 1. The banner and the toggles.
  let page = load(LOCAL, false);
  await settle(); await settle(); await settle();
  const banner = page.document.querySelector("[data-local-banner]");
  ok(banner !== null, "a local render gets the banner");
  ok(banner.textContent.indexOf("sld-gpu") !== -1, "the banner names the render");
  let toggles = page.document.querySelectorAll(".card [data-pick]");
  equal(toggles.length, 2, "every card gets a toggle");
  equal(toggles.map(function (b) { return b.textContent; }), ["Pick", "Pick"], "nothing picked yet");

  // 2. Pick, unpick, pick: the list is kept under the render's name.
  click(page, toggles[0]);               // entry 4
  click(page, toggles[1]);               // entry 1
  click(page, toggles[0]);               // entry 4 again: unpicked
  click(page, toggles[0]);               // and picked once more
  equal(JSON.parse(page.window.localStorage.getItem("sketchgen-picks:sld-gpu")), [1, 4],
        "the picks are stored under sketchgen-picks:<label>");
  equal(toggles[0].getAttribute("aria-pressed"), "true", "a picked card's toggle is pressed");
  equal(page.document.querySelector("[data-picked]").textContent, "2", "the banner counts the picks");

  // 3. Copy: ascending, one per line, newline at the end.
  click(page, page.document.querySelector("[data-copy-picks]"));
  equal(page.document.querySelector("[data-picks-text]").value, "1\n4\n",
        "Copy picked ids writes what import run --ids reads");

  // 4. Only counts reach the write path.
  equal(page.asked.filter(function (u) { return u.indexOf("config.json") === -1; })
    .map(function (u) { return u.replace(/\?.*$/, ""); }),
        ["https://write.example.invalid/counts"], "a local render reads counts and nothing else");

  // And anonymously: a kiosk origin is never given credentials.
  equal(page.options.filter(function (o) { return o.url.indexOf("/counts") !== -1; })
    .map(function (o) { return o.init === undefined || !o.init.credentials; }), [true],
        "a local render reads counts without credentials");

  // An entry page of the same render: its own toggle, and the stored pick.
  page = load(LOCAL, true);
  page.window.localStorage.setItem("sketchgen-picks:sld-gpu", "[4]");
  await settle(); await settle(); await settle();
  equal(page.document.querySelector("main.entry [data-pick]").textContent, "Picked",
        "the entry page shows the pick made on the grid");

  // Another render's list is its own.
  page = load({ local: "sld-cloud" }, false);
  page.window.localStorage.setItem("sketchgen-picks:sld-gpu", "[4]");
  await settle(); await settle(); await settle();
  equal(page.document.querySelector("[data-picked]").textContent, "0",
        "a second render does not see the first one's picks");

  // 5. The site: no banner, no toggles.
  page = load({ write_path: "" }, false);
  await settle(); await settle(); await settle();
  ok(page.document.querySelector("[data-local-banner]") === null, "the site has no banner");
  equal(page.document.querySelectorAll("[data-pick]").length, 0, "the site has no toggles");

  console.log("ok " + checks);
}

main().catch(function (err) {
  console.error("FAIL: " + (err && err.stack || err));
  process.exit(1);
});
