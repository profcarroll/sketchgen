"""The one script a sketch page carries beside p5.sound, and why.

p5.sound loads its AudioWorklet processors from ``blob:`` URLs and gates p5's
preload on them — ``registerMethod("init")`` increments the preload count and
decrements it only when every ``addModule`` has resolved, with no error path.
Inside a sandboxed frame, which is how every gallery page runs a sketch
(``sandbox="allow-scripts"``, an opaque origin), WebKit refuses to fetch a
``blob:`` URL. So on an iPhone a sketch that loads p5.sound says *Loading...*
for ever, on the entry page, on the kiosk's frame and on the swipe page alike,
while the same page opened on its own draws. Found on the swipe page, 20
September 2026, and reproduced in WebKitGTK 2.52 with a sandboxed frame.

The shim tries the blob first, exactly as p5.sound wrote it, so Chrome and the
gate never leave their native path; only a refusal is answered with the same
source as a ``data:`` URL, which no origin check can refuse; and a module that
fails both ways is let go rather than holding the sketch back. It goes
immediately under the addon's script tag and before ``sketch.js``: p5.sound's
init runs when p5 starts, after every script, so patching ``addModule`` after
the addon has loaded is early enough, and patching it before the addon would
miss the polyfill p5.sound installs where ``AudioWorklet`` is absent.

Two callers: the executor's fallback page for a sketch that needs the addon,
so the gate runs what the gallery will publish, and the gallery's entry copy,
so the entries published before this existed pick it up on the next
``render-all``. A page that already carries the marker is left alone.
"""

from __future__ import annotations

#: The addon's script tag, character for character as executor.py writes it.
P5_SOUND_TAG = ('    <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/'
                '1.11.3/addons/p5.sound.min.js"></script>\n')

#: The first line of the shim, which is also how a page that has it is known.
MARKER = "    <script>/* p5.sound in a sandboxed frame */"

SHIM = MARKER + """
    (function () {
      var W = self.AudioWorklet;
      var make = URL.createObjectURL;
      var blobs = {};
      if (!W || !W.prototype || !W.prototype.addModule) { return; }
      var add = W.prototype.addModule;
      URL.createObjectURL = function (obj) {
        var url = make.apply(this, arguments);
        if (obj instanceof Blob) { blobs[url] = obj; }
        return url;
      };
      W.prototype.addModule = function (url, options) {
        var worklet = this;
        var blob = blobs[url];
        return add.call(worklet, url, options).catch(function () {
          if (!blob) { return; }
          return new Promise(function (resolve) {
            var reader = new FileReader();
            reader.onload = function () {
              add.call(worklet, "data:application/javascript;charset=utf-8," +
                encodeURIComponent(reader.result), options).then(resolve, resolve);
            };
            reader.onerror = function () { resolve(); };
            reader.readAsText(blob);
          });
        });
      };
    })();
    </script>
"""


def with_shim(html: str) -> str:
    """``html`` with the shim under its p5.sound tag; unchanged when it has no
    tag, or already carries the shim."""
    if P5_SOUND_TAG not in html or MARKER in html:
        return html
    return html.replace(P5_SOUND_TAG, P5_SOUND_TAG + SHIM, 1)
