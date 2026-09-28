"""serve-gallery.py — the wall's own copy of the gallery, on its own loopback.

Run by org.sketchgen.gallery through serve-gallery.sh (docs/plans/local-gallery.md §3).
Serves ~/Library/sketchgen-kiosk/gallery — a symlink sync-local.sh swaps from one
release to the next — on 127.0.0.1:8090 and nowhere else. It is python3 -m http.server
with three differences, each a reason not to use that line in the plist:

- It will not bind anything but loopback. The tree is model-written sketches and a
  wall's Mac sits on a lab's Wi-Fi; nobody else in the room should be able to load them
  from it, and a flag typed wrong must not change that.
- It does not log a line per request. A wall fetches a sketch, its source and its
  frames every slot, all day; http.server's access log would be the biggest file on the
  Mac within a month, and nothing reads it. Errors still go to stderr.
- The manifests and config.json are sent no-store, so a sync is seen on the kiosk's next
  refresh and not whenever a cache lets go of it. kiosk.js asks for no-store as well;
  this is for anything that does not.

Python 3.9 and the standard library only: it is the Command Line Tools' python3, the
one the Mac has.
"""

import argparse
import functools
import http.server
import os
import socket
import sys

LOOPBACK = ("127.0.0.1", "::1", "localhost")
PORT = 8090
ROOT = os.path.expanduser("~/Library/sketchgen-kiosk/gallery")
NO_STORE = (".json",)


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        if self.path.split("?", 1)[0].endswith(NO_STORE):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):  # noqa: A002 - http.server's own name
        pass

    def log_error(self, format, *args):  # noqa: A002
        sys.stderr.write("serve-gallery: " + (format % args) + "\n")


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve the wall's gallery on loopback.")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--directory", default=ROOT)
    args = parser.parse_args(argv)
    if args.bind not in LOOPBACK:
        sys.stderr.write("serve-gallery: refused: --bind %s is not loopback\n" % args.bind)
        return 3
    # The directory is joined on every request, not resolved once, so the symlink
    # sync-local.sh swaps is followed to the new release on the next request.
    handler = functools.partial(Handler, directory=args.directory)
    Server.address_family = socket.AF_INET6 if ":" in args.bind else socket.AF_INET
    with Server((args.bind, args.port), handler) as server:
        sys.stderr.write("serve-gallery: %s on http://%s:%d\n" % (args.directory, args.bind, args.port))
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
