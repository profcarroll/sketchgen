"""Web copies of the gate's frames: a WebP beside each PNG, for the gallery.

The gate's PNGs are evidence. The judge and the critic look at them, the pair
hash is taken over their bytes (pairs.py), and nothing here touches them. What
the *gallery* needs is a picture a visitor can download, and on 2026-09-22 the
published tree was 1.2 GB against GitHub Pages' 1 GB cap, 1,166 MB of it these
PNGs: a strip is about 2 MB, lossless, of 5,132 × 900 pixels of canvas. A WebP
at quality 0.8 of the same strip is about a tenth of that
(docs/plans/gallery-hub.md, Step 0).

The copy is made once and kept beside its PNG (``strip.png`` → ``strip.webp``
in the gate's own directory), so a render only has to look for it: the
renderer stays stdlib and never starts a browser, and ``render-all`` over 1,200
entries costs one ``stat`` each. Making the copy needs an encoder, and the
node already has one it is guaranteed to have: the Chromium the gate runs,
through Playwright, in the node's venv. It runs in a child process
(``python -m sketchgen.webimg``), because Playwright's sync API is bound to the
thread that started it and the publisher is called from the web server's
request threads.

No encoder — the laptop, a test, a venv without Playwright — is not an error.
Nothing is made, the renderer publishes the PNG as it always has, and
``sketchgen web-frames`` says how many are still missing.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

#: Chromium's WebP quality, 0–1. 0.8 keeps the thin lines most sketches are
#: made of; a trial of two strips at the equivalent setting in ffmpeg came to
#: 230–340 KB from 3.2 MB.
QUALITY = 0.8

#: The suffix the web copy takes in place of ``.png``.
SUFFIX = ".webp"

#: A generous ceiling for the child: a strip takes well under a second once
#: the browser is up, and the browser takes a second or two to start.
SECONDS_PER_FILE = 10
STARTUP_SECONDS = 60

#: Set to anything to make :func:`available` say no, whatever is installed —
#: for the test suite, which must never start a browser.
DISABLE_ENV = "SKETCHGEN_NO_WEB_FRAMES"


def web_path(png: Path) -> Path:
    """Where ``png``'s web copy lives: beside it, same stem."""
    return png.with_suffix(SUFFIX)


def cached(png: Path | None) -> Path | None:
    """The web copy of ``png`` if one exists and is not older than the PNG.

    Older means the PNG was rewritten after the copy was made, and a picture
    of a different frame is worse than a large one.
    """
    if png is None:
        return None
    web = web_path(png)
    try:
        if web.stat().st_mtime >= png.stat().st_mtime and web.stat().st_size > 0:
            return web
    except OSError:
        return None
    return None


def web_or_png(png: Path | None) -> Path | None:
    """What the gallery publishes for a gate frame: its web copy, else itself."""
    if png is None:
        return None
    return cached(png) or png


def available() -> bool:
    """Whether this interpreter can make web copies at all."""
    if os.environ.get(DISABLE_ENV):
        return False
    return importlib.util.find_spec("playwright") is not None


def ensure(pngs: Iterable[Path | None], *, quality: float = QUALITY) -> dict[str, str]:
    """Make the web copy of each PNG that lacks a fresh one.

    Returns ``{png path: outcome}``, where outcome is ``"cached"``, ``"made"``
    or a sentence saying why not. Never raises: a publish that could not make
    a web copy publishes the PNG, which is what every entry did before this.
    """
    outcome: dict[str, str] = {}
    todo: list[Path] = []
    for png in pngs:
        if png is None:
            continue
        if not png.is_file():
            outcome[str(png)] = "no such file"
        elif cached(png) is not None:
            outcome[str(png)] = "cached"
        else:
            todo.append(png)
    if not todo:
        return outcome
    if not available():
        for png in todo:
            outcome[str(png)] = "no encoder: Playwright is not importable here"
        return outcome
    try:
        done = subprocess.run(
            [sys.executable, "-m", "sketchgen.webimg", "--quality", str(quality),
             *[str(p) for p in todo]],
            capture_output=True,
            text=True,
            timeout=STARTUP_SECONDS + SECONDS_PER_FILE * len(todo),
            cwd=str(Path(__file__).resolve().parent.parent),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        for png in todo:
            outcome[str(png)] = f"encoder did not run: {exc}"
        return outcome
    reported: dict[str, str] = {}
    for line in done.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and "png" in item:
            reported[str(item["png"])] = str(item.get("outcome") or "")
    tail = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1]
    for png in todo:
        outcome[str(png)] = reported.get(str(png)) or f"encoder failed: {tail}"
    return outcome


# ---------------------------------------------------------------------------
# The child: one browser, every file
# ---------------------------------------------------------------------------

#: Decode the PNG, draw it, encode it. ``convertToBlob`` falls back to PNG for
#: a type the browser cannot write, so the type is checked, not assumed.
_ENCODE_JS = """
async ([b64, quality]) => {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const bmp = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
  const canvas = new OffscreenCanvas(bmp.width, bmp.height);
  canvas.getContext('2d').drawImage(bmp, 0, 0);
  const blob = await canvas.convertToBlob({type: 'image/webp', quality: quality});
  if (blob.type !== 'image/webp') return null;
  const out = new Uint8Array(await blob.arrayBuffer());
  let s = '';
  for (let i = 0; i < out.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, out.subarray(i, i + 0x8000));
  }
  return btoa(s);
}
"""


def _encode_all(paths: list[Path], quality: float) -> int:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 - the node's venv only

    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            for png in paths:
                try:
                    b64 = page.evaluate(
                        _ENCODE_JS, [base64.b64encode(png.read_bytes()).decode("ascii"), quality]
                    )
                    if not b64:
                        raise ValueError("this Chromium cannot write WebP")
                    web = web_path(png)
                    # Written aside and renamed, so a render never publishes a
                    # half-written copy that cached() would take as fresh.
                    part = web.with_suffix(SUFFIX + ".part")
                    part.write_bytes(base64.b64decode(b64))
                    os.replace(part, web)
                    result = "made"
                except Exception as exc:  # noqa: BLE001 - one bad file is one line
                    failures += 1
                    result = f"encoder failed: {exc}".splitlines()[0]
                print(json.dumps({"png": str(png), "outcome": result}), flush=True)
        finally:
            browser.close()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sketchgen.webimg",
        description="Write a WebP beside each PNG, with the node's Chromium.",
    )
    parser.add_argument("png", nargs="+", type=Path)
    parser.add_argument("--quality", type=float, default=QUALITY)
    args = parser.parse_args(argv)
    return _encode_all(args.png, args.quality)


if __name__ == "__main__":
    sys.exit(main())
