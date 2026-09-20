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
    } else if ((match = /^#([A-Za-z0-9_-]+)/.exec(rest))) {
      part.attrs.push(["id", match[1]]);
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

/* ---- inline style ---------------------------------------------------------
 *
 * el.style reads and writes the style attribute, in both directions: gallery.js
 * reads a mark's left off an attribute the generator wrote, and kiosk.js writes
 * two custom properties onto the stage. A Proxy rather than a fixed list of
 * properties, because neither script should have to declare in advance which
 * ones it uses.
 */

function parseStyle(text) {
  const out = {};
  String(text || "").split(";").forEach(function (piece) {
    const cut = piece.indexOf(":");
    if (cut === -1) { return; }
    const name = piece.slice(0, cut).trim();
    if (name) { out[name] = piece.slice(cut + 1).trim(); }
  });
  return out;
}

function writeStyle(el, declarations) {
  const text = Object.keys(declarations)
    .map(function (name) { return name + ": " + declarations[name]; }).join("; ");
  if (text) { el.setAttribute("style", text); } else { el.removeAttribute("style"); }
}

function dashed(name) {
  return name.indexOf("--") === 0
    ? name : name.replace(/[A-Z]/g, function (ch) { return "-" + ch.toLowerCase(); });
}

function styleOf(el) {
  function setProperty(name, value) {
    const declarations = parseStyle(el.getAttribute("style"));
    declarations[name] = String(value);
    writeStyle(el, declarations);
  }
  return new Proxy({}, {
    get(target, prop) {
      if (typeof prop !== "string") { return undefined; }
      if (prop === "setProperty") { return setProperty; }
      if (prop === "getPropertyValue") {
        return function (name) {
          var found = parseStyle(el.getAttribute("style"))[name];
          return found === undefined ? "" : found;
        };
      }
      if (prop === "removeProperty") {
        return function (name) {
          const declarations = parseStyle(el.getAttribute("style"));
          delete declarations[name];
          writeStyle(el, declarations);
        };
      }
      return parseStyle(el.getAttribute("style"))[dashed(prop)];
    },
    set(target, prop, value) {
      if (typeof prop !== "string") { return true; }
      if (value === "") {
        const declarations = parseStyle(el.getAttribute("style"));
        delete declarations[dashed(prop)];
        writeStyle(el, declarations);
        return true;
      }
      setProperty(dashed(prop), value);
      return true;
    }
  });
}

/* ---- innerHTML ------------------------------------------------------------
 *
 * The kiosk paints its caption, its menu rows and its source listing by
 * assigning markup, so the stub has to turn that markup back into elements or
 * the tests would be reading a string and calling it a DOM. A parser for the
 * subset the scripts actually write — open tag with quoted or bare attributes,
 * text, close tag — which throws on anything else rather than dropping it.
 */

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: "\"", "#39": "'", apos: "'", nbsp: " " };

function decodeEntities(text) {
  return text.replace(/&(#?[A-Za-z0-9]+);/g, function (whole, name) {
    if (Object.prototype.hasOwnProperty.call(ENTITIES, name)) { return ENTITIES[name]; }
    throw new Error("dom.js does not know the entity &" + name + ";");
  });
}

function encodeEntities(text) {
  return text.replace(/[&<>]/g, function (ch) {
    return ch === "&" ? "&amp;" : (ch === "<" ? "&lt;" : "&gt;");
  });
}

function parseAttributes(blob) {
  const attrs = [];
  let rest = blob;
  let match;
  while (rest.trim().length) {
    match = /^\s+([A-Za-z_:][A-Za-z0-9_:.-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/.exec(rest);
    if (!match) { throw new Error("dom.js cannot parse attributes: " + blob); }
    const value = match[2] !== undefined ? match[2]
      : (match[3] !== undefined ? match[3] : (match[4] !== undefined ? match[4] : ""));
    attrs.push([match[1], decodeEntities(value)]);
    rest = rest.slice(match[0].length);
  }
  return attrs;
}

/* The HTML elements that never have a closing tag. The kiosk's caption writes
 * an <img> for the QR code (qr.md §5.3) and a parser that pushed it onto the
 * stack would swallow everything after it. Only the ones a browser actually
 * treats as void: an unknown tag still has to be closed, as it does in a
 * browser, so a typo in the script is still a loud failure here. */
const VOID_TAGS = [
  "area", "base", "br", "col", "embed", "hr", "img", "input",
  "link", "meta", "source", "track", "wbr"
];

function parseHTML(html, into) {
  const stack = [into];
  let rest = String(html);
  let match;
  while (rest.length) {
    if (rest.charAt(0) === "<") {
      if ((match = /^<\/([A-Za-z][A-Za-z0-9]*)\s*>/.exec(rest))) {
        const closed = stack.pop();
        if (stack.length === 0 || closed.tagName !== match[1].toLowerCase()) {
          throw new Error("dom.js: innerHTML closes </" + match[1] + "> that is not open");
        }
      } else if ((match = /^<([A-Za-z][A-Za-z0-9]*)((?:[^<>"']|"[^"]*"|'[^']*')*?)(\/?)>/.exec(rest))) {
        const el = new Element(match[1]);
        parseAttributes(match[2]).forEach(function (pair) { el.setAttribute(pair[0], pair[1]); });
        stack[stack.length - 1].appendChild(el);
        if (!match[3] && VOID_TAGS.indexOf(el.tagName) === -1) { stack.push(el); }
      } else {
        throw new Error("dom.js cannot parse innerHTML at: " + rest.slice(0, 40));
      }
    } else {
      const cut = rest.indexOf("<");
      const text = cut === -1 ? rest : rest.slice(0, cut);
      match = [text];
      stack[stack.length - 1].appendChild(new Text(decodeEntities(text)));
    }
    rest = rest.slice(match[0].length);
  }
  if (stack.length !== 1) {
    throw new Error("dom.js: innerHTML left <" + stack[stack.length - 1].tagName + "> open");
  }
}

function serialise(node) {
  if (node.nodeType === 3) { return encodeEntities(node.data); }
  const attrs = Object.keys(node.attributes)
    .map(function (name) { return " " + name + "=\"" + encodeEntities(node.attributes[name]) + "\""; })
    .join("");
  return "<" + node.tagName + attrs + ">" +
    node.childNodes.map(serialise).join("") + "</" + node.tagName + ">";
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
    // Node lays nothing out, so every box starts at zero: a script that
    // divides by a dimension has to cope with an element it cannot measure,
    // which is a hidden element in a browser too. They are plain properties
    // so a test can say how big a box is — set stage.clientWidth and
    // clientHeight and the fitting arithmetic becomes checkable.
    this.clientWidth = 0;
    this.clientHeight = 0;
    this.scrollHeight = 0;
    // The sheets on the swipe page only take a drag-to-close from the top of
    // their own scroll, so they read this before they start following a
    // finger. Nothing lays out here, so it starts at the top and stays there
    // unless a test says otherwise.
    this.scrollTop = 0;
  }

  /* Pointer capture, as a pair of no-ops. swipe.js captures the pointer on
   * pointerdown so that a drag which leaves the shield still ends on it; node
   * has no pointer to capture, and a test dispatches the whole sequence at the
   * element anyway, so there is nothing for these to do but exist. */
  setPointerCapture() {}
  releasePointerCapture() {}

  /* The nearest ancestor-or-self matching the selector, including this one.
   * The swipe page delegates the settings sheet's order rows and its sign-in
   * offer to the container, which is how a list repainted from innerHTML keeps
   * working without rewiring every row. */
  closest(selector) {
    var parts = parse(selector);
    var walk = this;
    while (walk && walk.nodeType === 1) {
      if (matches(walk, parts)) { return walk; }
      walk = walk.parentNode;
    }
    return null;
  }

  get className() { return this.attributes["class"] || ""; }
  set className(value) { this.attributes["class"] = String(value); }

  get classList() {
    const el = this;
    function names() {
      return String(el.attributes["class"] || "").split(/\s+/).filter(Boolean);
    }
    return {
      add: function (name) {
        const have = names();
        if (have.indexOf(name) === -1) { have.push(name); }
        el.attributes["class"] = have.join(" ");
      },
      remove: function (name) {
        el.attributes["class"] = names().filter(function (one) { return one !== name; }).join(" ");
      },
      contains: function (name) { return names().indexOf(name) !== -1; },
      toggle: function (name) {
        if (this.contains(name)) { this.remove(name); return false; }
        this.add(name);
        return true;
      }
    };
  }

  get style() { return styleOf(this); }

  get innerHTML() { return this.childNodes.map(serialise).join(""); }
  set innerHTML(value) {
    this.childNodes.forEach(function (child) { child.parentNode = null; });
    this.childNodes = [];
    parseHTML(value, this);
  }

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
    const el = this;
    (this.listeners.click || []).forEach(function (fn) {
      // target as well as preventDefault: a delegating handler reads it, and a
      // click on the element itself is its own target in a browser too.
      fn({ target: el, preventDefault: function () {} });
    });
  }

  /* One event at one element, with the fields the test wants on it. There is
   * no bubbling here and none is wanted: a test that means to exercise a
   * delegating handler dispatches at the container with the real target on the
   * event, which is what a browser would have handed it. Pointer events come
   * through here too — pointerdown, pointermove, pointerup — with clientX,
   * clientY and pointerId, because the swipe page's six gestures are pointer
   * events and there is no other way to drive them. */
  dispatch(name, event) {
    const detail = event || {};
    if (detail.target === undefined) { detail.target = this; }
    if (detail.preventDefault === undefined) { detail.preventDefault = function () {}; }
    (this.listeners[name] || []).forEach(function (fn) { fn(detail); });
    return detail;
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
    // A tab nobody is looking at counts no views on the swipe page, so a test
    // that wants that case sets this to "hidden".
    this.visibilityState = "visible";
    // A page has these two whatever else it has; kiosk.js puts a class on the
    // body and asks the root element about full screen.
    this.documentElement = new Element("html");
    this.appendChild(this.documentElement);
    this.body = new Element("body");
    this.documentElement.appendChild(this.body);
  }
  createElement(tag) { return new Element(tag); }
  createTextNode(value) { return new Text(value); }
  getElementById(id) { return this.querySelector('[id="' + id + '"]'); }
}

function makeWindow() {
  var store = {};
  var document = new Document();
  // The frames a script asked for, held rather than run: a test steps the
  // clock by hand, so a timer counted off rAF deltas is deterministic.
  var frames = [];
  var window = {
    document: document,
    // replace() records where the page meant to send somebody and goes
    // nowhere, as history.replaceState below does: node has no address bar to
    // follow it with, and a test that wants to know reads it back.
    location: {
      hash: "", search: "", pathname: "/e/82/",
      lastReplaced: null,
      replace: function (url) { window.location.lastReplaced = String(url); }
    },
    // Records what it was handed as well as doing nothing with it, so a test
    // can read back the address bar the script means to leave behind.
    history: {
      lastUrl: null,
      replaceState: function (state, title, url) { window.history.lastUrl = url; }
    },
    localStorage: {
      getItem: function (key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
      setItem: function (key, value) { store[key] = String(value); },
      removeItem: function (key) { delete store[key]; }
    },
    // Nothing in a test reaches the network: the page must stand up anyway.
    fetch: function () { return Promise.reject(new Error("offline")); },
    // Enough of a navigator for the swipe page's hold, which buzzes a phone
    // that can be buzzed. Every call is kept, so a test can say the hold did
    // it and the other five gestures did not.
    navigator: {
      buzzed: [],
      vibrate: function (ms) { window.navigator.buzzed.push(ms); return true; }
    },
    listeners: {},
    addEventListener: function (name, fn) {
      (window.listeners[name] = window.listeners[name] || []).push(fn);
    },
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval,
    clock: 0,
    performance: { now: function () { return window.clock; } },
    requestAnimationFrame: function (fn) { return frames.push(fn); },
    // Advance the clock by ms and run every frame that was waiting on it.
    step: function (ms) {
      var due = frames;
      frames = [];
      window.clock += ms;
      due.forEach(function (fn) { fn(window.clock); });
    }
  };
  return window;
}

module.exports = { Element, Text, Document, makeWindow };
