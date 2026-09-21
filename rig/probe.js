// probe.js — paste into the rig tab's console, after the sketch is running.
//
//   await __probe.all()        everything below, in order
//   __probe.errors             what this hook has caught since it was installed
//
// The five things that broke a sketch on the laptop on 2026-09-21 (job 1286)
// and would have broken it on the node: a 0x0 window, a 1x1 window, a portrait
// window, a very wide one, and ten clicks in quick succession. Each is run by
// lying to the sketch about the window the way a resize does, stepping a few
// frames, and reading back whether anything threw and whether the sketch's own
// numbers are still finite.
//
// Why the error hook is here at all: the pane's console tool keeps errors
// across reloads, so a stale error reads as a new one and an innocent sketch
// looks broken. This hook is installed by hand, after the reload, and stamps
// what it catches, so its list is the only one that can be trusted.
//
// The rig is advisory. The gate's number is the number — see rig/README.md.

(function () {
    "use strict";

    const P = (window.__probe = window.__probe || {});
    P.errors = P.errors || [];
    P.installedAt = P.installedAt || new Date().toISOString();

    if (!P._hooked) {
        // Both hooks: a p5 sketch that throws inside draw() surfaces as an
        // onerror, and one that rejects a loadImage promise does not.
        window.addEventListener("error", (e) => {
            P.errors.push({ at: new Date().toISOString(), message: String(e.message || e.error) });
        });
        window.addEventListener("unhandledrejection", (e) => {
            P.errors.push({ at: new Date().toISOString(), message: "unhandled rejection: " + String(e.reason) });
        });
        P._hooked = true;
    }

    const canvas = () => document.querySelector("canvas");
    const frames = (n) =>
        new Promise((resolve) => {
            let left = n;
            const tick = () => (--left <= 0 ? resolve() : requestAnimationFrame(tick));
            requestAnimationFrame(tick);
        });

    // p5 reads windowWidth/windowHeight, which it only updates from a real
    // resize event; the rig cannot resize the pane, so the sketch is told the
    // size directly and asked to lay itself out again, which is what
    // windowResized() does on the node when the viewport is 1280x900.
    async function atSize(w, h, steps) {
        const before = P.errors.length;
        const wasW = window.windowWidth, wasH = window.windowHeight;
        window.windowWidth = w;
        window.windowHeight = h;
        try {
            if (typeof window.resizeCanvas === "function") window.resizeCanvas(w, h, true);
            if (typeof window.windowResized === "function") window.windowResized();
        } catch (err) {
            P.errors.push({ at: new Date().toISOString(), message: "resize threw: " + err });
        }
        await frames(steps || 12);
        const threw = P.errors.slice(before);
        window.windowWidth = wasW;
        window.windowHeight = wasH;
        return threw;
    }

    function nonFinite() {
        // A NaN in the sketch's own state is the failure mode a 0x0 window
        // causes: the canvas keeps its size, nothing throws, and every shape
        // lands at NaN for the rest of the run. One level deep over the
        // sketch's globals catches the arrays and the little state objects
        // that hold it; anything p5 or the browser owns is skipped.
        const skip = /^(p5|_|window|document|location|navigator|performance|frameCount|pixels)/;
        const bad = [];
        const look = (path, v, depth) => {
            if (typeof v === "number") {
                if (!Number.isFinite(v)) bad.push(path + " = " + v);
                return;
            }
            if (depth <= 0 || v === null || typeof v !== "object") return;
            if (ArrayBuffer.isView(v)) {
                for (let i = 0; i < v.length; i++) {
                    if (!Number.isFinite(v[i])) { bad.push(path + "[" + i + "] = " + v[i]); return; }
                }
                return;
            }
            const keys = Array.isArray(v) ? v.keys() : Object.keys(v);
            let seen = 0;
            for (const k of keys) {
                if (++seen > 200) return;            // a particle array is enough of a sample
                try { look(path + "." + k, v[k], depth - 1); } catch (e) { /* getters may throw */ }
            }
        };
        for (const k of Object.keys(window)) {
            if (skip.test(k)) continue;
            let v;
            try { v = window[k]; } catch (e) { continue; }
            if (typeof v === "function") continue;
            look(k, v, 3);
        }
        return bad;
    }

    async function clicks(n) {
        // Dispatched to the canvas ONLY. A synthetic click sent to the canvas
        // and to window fires p5's mouseClicked twice, and a sketch that looks
        // like it double-counts is usually the probe's fault (trap 6).
        const c = canvas();
        if (!c) return ["no canvas"];
        const before = P.errors.length;
        const box = c.getBoundingClientRect();
        for (let i = 0; i < n; i++) {
            const opts = {
                bubbles: true, cancelable: true, view: window,
                clientX: box.left + box.width / 2, clientY: box.top + box.height / 2,
            };
            c.dispatchEvent(new MouseEvent("mousedown", opts));
            c.dispatchEvent(new MouseEvent("mouseup", opts));
            c.dispatchEvent(new MouseEvent("click", opts));
            await frames(1);
        }
        await frames(10);                             // the gate steps 30 after its one click
        return P.errors.slice(before);
    }

    function report(name, problems) {
        const ok = problems.length === 0;
        console.log((ok ? "PASS  " : "FAIL  ") + name + (ok ? "" : "\n      " + problems.map(
            (p) => (typeof p === "string" ? p : p.message)).join("\n      ")));
        return ok;
    }

    P.all = async function all() {
        if (!window.p5 || !canvas()) {
            console.log("FAIL  no p5 canvas on this page — is the tab selected and p5.min.js fetched?");
            return false;
        }
        const w = window.windowWidth, h = window.windowHeight;
        let ok = true;
        ok = report("0x0 window", await atSize(0, 0)) && ok;
        ok = report("1x1 window", await atSize(1, 1)) && ok;
        ok = report("portrait 500x900", await atSize(500, 900)) && ok;
        ok = report("wide 1900x420", await atSize(1900, 420)) && ok;
        ok = report("back to " + w + "x" + h, await atSize(w, h, 20)) && ok;
        ok = report("ten rapid clicks", await clicks(10)) && ok;
        ok = report("finite values", nonFinite()) && ok;
        console.log(ok
            ? "probe clean since " + P.installedAt + " — now run bench.js"
            : "probe found something; fix it before the gate does");
        return ok;
    };

    console.log("probe installed at " + P.installedAt + "; run:  await __probe.all()");
})();
