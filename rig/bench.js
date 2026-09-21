// bench.js — paste into the rig tab's console, after probe.js is clean.
//
//   await __bench.run()        120 frames, the gate's idle window
//   await __bench.run(300)     longer, if the number is not settling
//
// **Do not time the pane's canvas.** It is GPU-backed: a getImageData after
// each frame adds a flush floor of about 20 ms, and on 2026-09-21 two runs of
// one identical sketch read 14.7 ms and 38 ms. The number that matters is CPU
// raster, because the node's headless Chromium has no GPU, so this swaps the
// renderer's 2d context for an offscreen one created with willReadFrequently
// and times the same draw() into that. On job 1286 the swap immediately showed
// a full-canvas gradient backdrop costing 5.7 ms a frame, which the GPU canvas
// had hidden entirely.
//
// The proxy is a floor, not the verdict: entry 1279 read 6.4 ms here and 9.8 ms
// on the gate, so what is printed below is also multiplied by 1.5 before being
// compared with the gate's budget. rig/README.md has the rest.

(function () {
    "use strict";

    const B = (window.__bench = window.__bench || {});

    // gate/sketch_gate.py: DEFAULT_FRAME_BUDGET_MS, EARLY_TRIP_FACTOR, IDLE_FRAMES.
    B.BUDGET_MS = 100.0;
    B.EARLY_TRIP = 2.0;
    B.IDLE_FRAMES = 120;
    // MEASURE[rig-proxy-vs-gate], one point: 9.8 / 6.4 for entry 1279.
    B.NODE_FACTOR = 1.5;

    const median = (a) => {
        const s = [...a].sort((x, y) => x - y);
        return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
    };

    B.run = async function run(n) {
        n = n || B.IDLE_FRAMES;
        const instance = window.p5 && window.p5.instance;
        const renderer = instance && instance._renderer;
        if (!renderer || !renderer.drawingContext || !renderer.canvas) {
            console.log("no 2d p5 renderer on this page (WEBGL sketches cannot be timed here;"
                + " keep them well under budget and use paid try when it lands)");
            return null;
        }

        const real = renderer.drawingContext;
        const density = (typeof window.pixelDensity === "function" ? window.pixelDensity() : 1) || 1;
        const off = document.createElement("canvas");
        off.width = renderer.canvas.width;
        off.height = renderer.canvas.height;
        const ctx = off.getContext("2d", { willReadFrequently: true });
        if (!ctx) { console.log("no offscreen 2d context"); return null; }
        ctx.setTransform(density, 0, 0, density, 0, 0);   // p5 scales by density at setup

        const looping = typeof window.isLooping === "function" ? window.isLooping() : true;
        if (typeof window.noLoop === "function") window.noLoop();
        renderer.drawingContext = ctx;
        window.drawingContext = ctx;                      // sketches reach for it by name

        const ms = [];
        try {
            for (let i = 0; i < 10; i++) window.redraw();  // warm up: first frames build state
            for (let i = 0; i < n; i++) {
                const t0 = performance.now();
                window.redraw();
                ctx.getImageData(0, 0, 1, 1);              // one pixel: makes the raster finish
                ms.push(performance.now() - t0);
            }
        } catch (err) {
            console.log("threw while benching: " + err);
        } finally {
            renderer.drawingContext = real;
            window.drawingContext = real;
            if (looping && typeof window.loop === "function") window.loop();
        }

        if (!ms.length) return null;
        const mean = ms.reduce((a, b) => a + b, 0) / ms.length;
        const projected = mean * B.NODE_FACTOR;
        const verdict = projected > B.BUDGET_MS
            ? "OVER the gate's budget — frame_budget would fail"
            : projected > B.BUDGET_MS / 4
                ? "under budget, but with less than 4x headroom — look again"
                : "comfortably under budget";
        const r = {
            frames: ms.length,
            mean_ms: +mean.toFixed(2),
            median_ms: +median(ms).toFixed(2),
            min_ms: +Math.min(...ms).toFixed(2),
            max_ms: +Math.max(...ms).toFixed(2),
            projected_node_ms: +projected.toFixed(2),
            budget_ms: B.BUDGET_MS,
        };
        console.log(
            `${r.frames} frames on a CPU canvas: mean ${r.mean_ms} ms, median ${r.median_ms} ms `
            + `(min ${r.min_ms}, max ${r.max_ms})\n`
            + `x${B.NODE_FACTOR} for the node = ${r.projected_node_ms} ms against the gate's `
            + `${B.BUDGET_MS} ms budget (it trips early at ${B.BUDGET_MS * B.EARLY_TRIP} ms)\n`
            + verdict + ". The gate's number is the number.");
        B.last = r;
        return r;
    };

    console.log("bench ready; run:  await __bench.run()");
})();
