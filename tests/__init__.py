"""Test-suite bootstrap: point every live path somewhere that does not exist.

This package exists for one reason. On 2026-09-22 the suite was run on the
node, and four times over two hours it published to the real gallery: entry
1's page was overwritten with a fixture, fixture entries 9281 and 9282 were
added, and index.html, kiosk.json, swipe.json, pairs.json, rejections.html
and compare.html were re-rendered from the temporary test database, which
left the public gallery showing "Nothing here yet." over 1047 published
entries. Recovering it took a full re-render.

Nothing in the suite was wrong about its own intent. The tests that take the
real publish path assume it will refuse, and on a laptop it does — there is
no ~/sketchgen/gallery there, and the comment in test_web.py's
test_a_rejection_whose_push_fails_stays_pending says so in as many words. On
the node that assumption inverts: the checkout is there, ~/.ssh/sketchgen-
gallery is a deploy key with write access, and publish.py reads both from the
environment (DEFAULT_GALLERY_DIR, DEFAULT_KEY_PATH) with the live paths as
their defaults. The suite did exactly what it was asked to.

So the assumption is made true everywhere instead of being relied on. These
paths do not exist and are never created: a publisher that reaches for one
refuses with "no gallery checkout at ...", which is the behaviour every test
here was already written against. Set before the first `from sketchgen
import ...` anywhere in the suite, because publish.py reads the environment
at import time and binds the result as a default argument, where a later
setenv cannot reach it — which is also why this belongs in __init__.py and
not in a setUp.

These are assigned, not set as defaults. A default would let an operator who
happens to export SKETCHGEN_GALLERY in their shell — the one person most
likely to have a live checkout in it — run the suite straight back into the
incident above. A test that wants a real gallery still builds one under a
temporary directory and passes the path, as test_publish.py does, and a
subprocess test still composes its own environment, as test_backup.py does
with SKETCHGEN_DB; both override an argument or a child env, neither of which
this touches.
"""

import os
import tempfile

#: Under the system temp directory, so it is obviously disposable, and named
#: for what it is, so a stray file there says where it came from. Nothing
#: creates it; "does not exist" is the whole point.
_NOWHERE = os.path.join(tempfile.gettempdir(), "sketchgen-tests-must-not-touch-this")

os.environ["SKETCHGEN_GALLERY"] = os.path.join(_NOWHERE, "gallery")
os.environ["SKETCHGEN_GALLERY_KEY"] = os.path.join(_NOWHERE, "deploy-key")
os.environ["SKETCHGEN_DB"] = os.path.join(_NOWHERE, "sketchgen.db")
