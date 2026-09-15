/* The least DOM gallery.js needs, so that run-in-place can be tested for real.
 *
 * Node is not a browser and this is not jsdom: it is the handful of methods
 * the script actually calls, plus a selector matcher that understands the
 * selectors the script actually writes (tag, .class, [attr], [attr="value"],
 * :not(.class), and the descendant combinator). Anything the script starts
 * using that is not here fails loudly rather than quietly doing nothing.
 */

"use strict";

function parseCompound(text) {
  var part = { tag: null, classes: [], attrs: [], not: [] };
  var rest = text;
  var match;
  while (rest.length) {
    if ((match = /^:not\(\.([A-Za-z0-9_-]+)\)/.exec(rest))) {
      part.not.push(match[1]);
    } else if ((match = /^\.([A-Za-z0-9_-]+)/.exec(rest))) {
      part.classes.push(match[1]);
    } else if ((match = /^\[([A-Za-z0-9_:-]+)(?:=["']([^"']*)["'])?\]/.exec(rest))) {
      part.attrs.push([match[1], match[2] === undefined ? null : match[2]]);
    } else if ((match = /^([A-Za-z][A-Za-z0-9]*)/.exec(rest))) {
      part.tag = match[1].toLowerCase();
    } else {
      throw new Error("dom.js cannot parse selector fragment: " + rest);
    }
    rest = rest.slice(match[0].length);
  }
  return part;
}

function parse(selector) {
  return selector.trim().split(/\s+/).map(parseCompound);
}

function matchesCompound(el, part) {
  if (part.tag && el.tagName !== part.tag) { return false; }
  var classes = String(el.getAttribute("class") || "").split(/\s+/);
  for (var i = 0; i < part.classes.length; i += 1) {
    if (classes.indexOf(part.classes[i]) === -1) { return false; }
  }
  for (i = 0; i < part.not.length; i += 1) {
    if (classes.indexOf(part.not[i]) !== -1) { return false; }
  }
  for (i = 0; i < part.attrs.length; i += 1) {
    var name = part.attrs[i][0];
    var want = part.attrs[i][1];
    var have = el.getAttribute(name);
    if (have === null) { return false; }
    if (want !== null && String(have) !== want) { return false; }
  }
  return true;
}

function matches(el, parts) {
  if (!matchesCompound(el, parts[parts.length - 1])) { return false; }
  var rest = parts.slice(0, -1);
  var walk = el.parentNode;
  while (rest.length && walk) {
    if (walk.nodeType === 1 && matchesCompound(walk, rest[rest.length - 1])) {
      rest = rest.slice(0, -1);
    }
    walk = walk.parentNode;
  }
  return rest.length === 0;
}

class Text {
  constructor(value) {
    this.nodeType = 3;
    this.data = String(value);
    this.parentNode = null;
  }
  get textContent() { return this.data; }
}

const REFLECTED = ["src", "href", "title", "type", "loading", "alt", "id"];

class Element {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag).toLowerCase();
    this.attributes = {};
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = {};
    this.hidden = false;
  }

  get className() { return this.attributes["class"] || ""; }
  set className(value) { this.attributes["class"] = String(value); }

  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name] : null;
  }
  removeAttribute(name) { delete this.attributes[name]; }

  appendChild(node) {
    if (node.parentNode) { node.parentNode.removeChild(node); }
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  insertBefore(node, ref) {
    if (!ref) { return this.appendChild(node); }
    if (node.parentNode) { node.parentNode.removeChild(node); }
    var at = this.childNodes.indexOf(ref);
    node.parentNode = this;
    this.childNodes.splice(at === -1 ? this.childNodes.length : at, 0, node);
    return node;
  }

  removeChild(node) {
    var at = this.childNodes.indexOf(node);
    if (at !== -1) { this.childNodes.splice(at, 1); }
    node.parentNode = null;
    return node;
  }

  remove() { if (this.parentNode) { this.parentNode.removeChild(this); } }

  get nextSibling() {
    if (!this.parentNode) { return null; }
    var at = this.parentNode.childNodes.indexOf(this);
    return this.parentNode.childNodes[at + 1] || null;
  }

  get descendants() {
    var out = [];
    this.childNodes.forEach(function (child) {
      if (child.nodeType !== 1) { return; }
      out.push(child);
      out = out.concat(child.descendants);
    });
    return out;
  }

  querySelectorAll(selector) {
    var parts = parse(selector);
    return this.descendants.filter(function (el) { return matches(el, parts); });
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  get textContent() {
    return this.childNodes.map(function (child) { return child.textContent; }).join("");
  }
  set textContent(value) {
    this.childNodes.forEach(function (child) { child.parentNode = null; });
    this.childNodes = [];
    if (value !== "") { this.appendChild(new Text(value)); }
  }

  addEventListener(name, fn) {
    (this.listeners[name] = this.listeners[name] || []).push(fn);
  }

  click() {
    (this.listeners.click || []).forEach(function (fn) { fn({ preventDefault: function () {} }); });
  }
}

REFLECTED.forEach(function (name) {
  Object.defineProperty(Element.prototype, name, {
    get: function () { return this.getAttribute(name) || ""; },
    set: function (value) { this.setAttribute(name, value); }
  });
});

class Document extends Element {
  constructor() {
    super("#document");
    this.readyState = "complete";
  }
  createElement(tag) { return new Element(tag); }
  createTextNode(value) { return new Text(value); }
  getElementById(id) { return this.querySelector('[id="' + id + '"]'); }
}

function makeWindow() {
  var store = {};
  var document = new Document();
  var window = {
    document: document,
    location: { hash: "", search: "", pathname: "/e/82/" },
    history: { replaceState: function () {} },
    localStorage: {
      getItem: function (key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
      setItem: function (key, value) { store[key] = String(value); },
      removeItem: function (key) { delete store[key]; }
    },
    // Nothing in a test reaches the network: the page must stand up anyway.
    fetch: function () { return Promise.reject(new Error("offline")); }
  };
  return window;
}

module.exports = { Element, Text, Document, makeWindow };
