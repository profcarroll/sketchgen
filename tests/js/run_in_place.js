/* Run-in-place, run for real (plan §6.7, §6.9).
 *
 * gallery.js is loaded once into a stub DOM (tests/js/dom.js) and then
 * clicked: the ledger's tiles on an entry page, and the two thumbnails on the
 * compare page, must share one helper and one rule — at most one iframe on the
 * page at a time, sandboxed to scripts, and removed rather than hidden when
 * the sketch is stopped.
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
  ok(got === want, what + " — got " + JSON.stringify(got) + ", wanted " + JSON.stringify(want));
}

function load(page) {
  const window = makeWindow();
  const document = window.document;
  page(document, window);
  window.SKETCHGEN_ROOT = "../../";
  const context = vm.createContext({
    window: window,
    document: document,
    fetch: window.fetch,
    URLSearchParams: URLSearchParams,
    Promise: Promise,
    console: console,
    Math: Math,
    Number: Number,
    String: String,
    Object: Object,
    Array: Array,
    JSON: JSON,
    parseInt: parseInt,
    setTimeout: setTimeout
  });
  vm.runInContext(fs.readFileSync(SCRIPT, "utf8"), context, { filename: "gallery.js" });
  return { window, document };
}

function settle() {
  return new Promise(function (resolve) { setTimeout(resolve, 0); });
}

function tile(document, panel, id) {
  const box = document.createElement("div");
  box.className = "ledger-tile narrow";
  const button = document.createElement("button");
  button.setAttribute("data-play", "");
  button.setAttribute("data-run-href", "../" + id + "/sketch/");
  button.setAttribute("data-run-name", "entry " + id);
  button.className = "play";
  const label = document.createElement("span");
  label.setAttribute("data-play-label", "");
  button.appendChild(label);
  box.appendChild(button);
  panel.appendChild(box);
  return { box: box, button: button, label: label };
}

function entryPage(document) {
  const panel = document.createElement("div");
  panel.setAttribute("data-ledger", "82");
  panel.setAttribute("data-ledger-root", "11");
  panel.setAttribute("data-ledger-parent", "78");
  document.appendChild(panel);
  document.tiles = [tile(document, panel, 11), tile(document, panel, 78)];
}

function comparePage(document) {
  const page = document.createElement("main");
  page.setAttribute("data-compare", "");
  document.appendChild(page);
  const entries = document.createElement("script");
  entries.setAttribute("id", "sketchgen-entries");
  entries.textContent = JSON.stringify([
    { id: 1, href: "e/1/", strip: "e/1/strip.png", state: "published", brief: "one" },
    { id: 2, href: "e/2/", strip: "e/2/strip.png", state: "published", brief: "two" }
  ]);
  page.appendChild(entries);
  document.sides = {};
  ["A", "B"].forEach(function (letter) {
    const side = document.createElement("section");
    side.className = "side";
    side.setAttribute("data-side", letter);
    const thumb = document.createElement("p");
    thumb.setAttribute("data-thumb", "");
    side.appendChild(thumb);
    const brief = document.createElement("p");
    brief.setAttribute("data-brief", "");
    side.appendChild(brief);
    const note = document.createElement("p");
    note.setAttribute("data-run-note", "");
    side.appendChild(note);
    page.appendChild(side);
    document.sides[letter] = side;
  });
}

async function ledger() {
  const { document } = load(entryPage);
  await settle();
  const frames = function () { return document.querySelectorAll("iframe.sketch"); };
  equal(frames().length, 0, "nothing runs before a click");

  document.tiles[0].button.click();
  equal(frames().length, 1, "one frame after the first click");
  equal(frames()[0].getAttribute("src"), "../11/sketch/", "the frame is that entry's sketch page");
  equal(frames()[0].getAttribute("sandbox"), "allow-scripts", "sandboxed to scripts and nothing else");
  equal(document.tiles[0].button.className, "play running", "the button says it is running");
  equal(document.tiles[0].label.textContent, "stop", "and reads stop");
  equal(document.tiles[0].button.getAttribute("aria-label"), "stop entry 11", "the label stops it");

  // A second tile: the first frame goes away, it is not left playing behind.
  document.tiles[1].button.click();
  equal(frames().length, 1, "still exactly one frame with two tiles clicked");
  equal(frames()[0].getAttribute("src"), "../78/sketch/", "and it is the second tile's");
  equal(document.tiles[0].button.className, "play", "the first tile is idle again");
  equal(document.tiles[0].label.textContent, "", "and its label is empty");

  // Stopping REMOVES the iframe: a hidden frame would keep drawing.
  document.tiles[1].button.click();
  equal(frames().length, 0, "stopping removes the iframe");
  equal(document.querySelector("iframe.sketch"), null, "nothing is left hidden in the tile");
}

async function compare() {
  const { document } = load(comparePage);
  await settle();
  const frames = function () { return document.querySelectorAll("iframe.sketch"); };
  const buttons = document.querySelectorAll("[data-play]");
  equal(buttons.length, 2, "the compare page paints a play button per side");
  equal(frames().length, 0, "nothing runs before a click");

  buttons[0].click();
  equal(frames().length, 1, "one frame after a click");
  equal(frames()[0].getAttribute("src"), "../../e/1/sketch/", "the frame is that entry's sketch page");
  equal(frames()[0].getAttribute("sandbox"), "allow-scripts", "sandboxed to scripts and nothing else");
  equal(document.sides.A.querySelector("[data-run-note]").textContent,
        "running · click to stop", "the caption says what clicking does now");

  buttons[1].click();
  equal(frames().length, 1, "never two sketches at once");
  equal(document.sides.A.querySelector("[data-run-note]").textContent,
        "click to run", "the stopped side offers to run again");

  buttons[1].click();
  equal(frames().length, 0, "stopping removes the iframe");
}

async function main() {
  await ledger();
  await compare();
  console.log("ok " + checks);
}

main().catch(function (err) {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
