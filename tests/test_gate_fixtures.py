"""The gate, run over its own fixtures — when there is a browser to run it in.

`gate/accept.sh` is the real harness and has been since packet 1.2: it runs
`sketch_gate.py` twice per fixture, once plain and once with that fixture's
assertions, and compares both against `gate/fixtures/expected.json`. This file
does not reimplement any of that. It runs `accept.sh` and asserts it exits 0,
so that moving the gate into the repo (packet 0, spec §3.2) cannot quietly break
it and nobody notices until a job fails on the node.

It needs three things the ordinary developer machine does not have: the
Playwright package, the Chromium it downloads, and the network (the fixtures
load p5.js from a CDN). Each missing one is a **skip**, not a failure — a laptop
with no venv must still be able to run `python3 -m unittest discover -s tests`
and get a clean result.

A skip here is not a pass. After changing anything under `gate/`, run the
harness where it can actually run:

    ssh sld-cloud '. ~/sketchgen/.venv/bin/activate && ~/sketchgen/app/gate/accept.sh'
"""

import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_DIR = REPO_ROOT / "gate"
GATE = GATE_DIR / "sketch_gate.py"
ACCEPT = GATE_DIR / "accept.sh"
FIXTURES = GATE_DIR / "fixtures"
HOOK_HARNESS = REPO_ROOT / "tests" / "js" / "gate_hook.js"

sys.path.insert(0, str(REPO_ROOT))
from sketchgen import executor, ghostshim  # noqa: E402

#: sha256 of the gate in this repo. Three copies — here, the node, the
#: operator's course repo — one hash: that is the whole claim spec §3.2 makes,
#: and it stops a well-meaning reformat of the referee from landing without
#: somebody deciding to change this line.
#:
#: Changed 2026-09-16 by ResourceLog (entry 429): the gate now records every
#: resource the sketch asked for from outside SKETCH_ORIGIN and did not get, and
#: reports them as notes and under `resources`. It adds no check and fails no
#: run — reaching outside the sketch stays allowed. It exists because a failed
#: image is not a page error, so `console_clean` stayed true while the canvas
#: stayed blank, and the only evidence the executor got was three assertions
#: reading zero pixels changed. The previous value, which is the one the node
#: and the course repo carry until this is deployed, was
#: 2cd5f5156f3669ee558f051d69939655a4a7bc82707b1469ffcb48cb54d1a85e.
#:
#: Changed 2026-09-15 by the frame budget (job 166, job 270): the gate now times
#: its own idle window and fails `frame_budget` above --frame-budget-ms, or
#: above the --budget-s wall ceiling.
#:
#: Changed 2026-09-21 by the ghost window (entry 1103, auto-mouse.md §5): after
#: every check and every assertion has been decided, the gate plays a pointer
#: script through page.mouse and writes ghost.png beside strip.png. It adds no
#: check and fails no run — strip.png and gate.png are byte-for-byte what they
#: were, the assertions are evaluated before it opens, and console_clean is
#: read at the moment it opens.
#:
#: Changed 2026-09-22 by `loads(image)` (entries 429 and 1103,
#: media-assertion.md §3.1): the vocabulary gains an eighth word, ResourceLog
#: records the images that DID arrive as `resources_loaded`, and the wait for a
#: canvas runs to the whole --timeout when the word is asserted, with
#: `timings.preload_s` saying how much of it went by. It adds no check: an
#: image arriving fails nothing, and one not arriving fails nothing either
#: unless a planner asked for a picture.
#:
#: That wait also stopped being free. It polled on requestAnimationFrame, which
#: this file replaces with a queue nothing drains until the run steps it, so it
#: had never once ended early: entry 429's reports on the node read load_s
#: 10.19 s on nine of ten attempts, the cap to the millisecond, for a
#: photograph that arrives in a tenth of a second. It polls on a timer now, so
#: every preload() sketch gates about ten seconds faster. The previous value,
#: which is the one the node and the course repo carry until this is deployed,
#: was
#: f64ac446bac40ebce61bfc2811684087b23c824f70220de1dd373b56928bfcbc.
#:
#: Changed 2026-09-24 by job 1542 (entry 1531): the hook that captures the
#: sketch's p5 instance also fired for every createGraphics() buffer, because
#: p5 1.11's Graphics constructor runs the same prototype method on itself, and
#: it kept the last one. So a sketch that made a buffer read is_looping false
#: and skipped frame_advancing though it never called noLoop(), and took
#: uses(webgl) and size(w,h) off the buffer. The hook now keeps only a p5. The
#: previous value, which is the one the node and the course repo carry until
#: this is deployed, was
#: 4fac3c1635269d408cc74ffd3ad02c5c2b42316e0edcb4fdf31a921ebb779925.
GATE_SHA256 = "f5991aacb816ed674afcb47b42811d50bf24d29f9cf69ff8cd59c435694eb540"

#: How long the fourteen fixtures are allowed to take together. On the node a
#: single gate run is about four seconds and the harness does twenty-four of
#: them — except bad-frame-budget, which is a third of a second a frame by
#: design and costs tens of seconds before the budget stops it. That fixture is
#: why this number is 900 and not 600, and the three image fixtures
#: (2026-09-22) are why it is 1200 now: they fetch from a real host, and the one
#: that asserts loads(image) will wait out the whole 60 s timeout for a canvas
#: if that host is having a bad morning. The three createGraphics fixtures
#: (2026-09-24) add six ordinary runs and did not move it: all fourteen took
#: 82 s on the laptop.
ACCEPT_TIMEOUT_S = 1200


def _have_playwright() -> bool:
    """True when the package imports, without importing it."""
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


class GateFilesTests(unittest.TestCase):
    """What must be in the repo, checked everywhere, browser or no browser."""

    def test_the_gate_and_its_harness_are_tracked(self):
        for path in (GATE, ACCEPT, FIXTURES / "expected.json", GATE_DIR / "README.md"):
            self.assertTrue(path.is_file(), f"missing from the repo: {path}")

    def test_the_gate_is_executable_and_unmodified(self):
        self.assertTrue(os.access(GATE, os.X_OK), f"{GATE} is not executable")
        self.assertTrue(os.access(ACCEPT, os.X_OK), f"{ACCEPT} is not executable")
        digest = hashlib.sha256(GATE.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            GATE_SHA256,
            "sketch_gate.py differs from the copy on the node and in the course "
            "repo; if that is deliberate, change GATE_SHA256 here in the same "
            "commit and say why",
        )

    def test_every_fixture_has_an_expectation(self):
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        directories = sorted(
            p.name for p in FIXTURES.iterdir()
            if p.is_dir() and (p / "index.html").is_file()
        )
        self.assertTrue(directories, "no fixture directories were copied")
        for name in directories:
            self.assertIn(name, expected, f"fixture {name} has no entry in expected.json")
            self.assertIn("exit", expected[name])
            self.assertIn("checks", expected[name])
            self.assertTrue(
                (FIXTURES / name / "sketch.js").is_file(),
                f"fixture {name} has no sketch.js",
            )


class AcceptHarnessTests(unittest.TestCase):
    """The harness itself, which only runs where Playwright does."""

    @classmethod
    def setUpClass(cls):
        if not _have_playwright():
            raise unittest.SkipTest(
                "playwright is not importable — build the venv from "
                "requirements.txt and run `playwright install chromium` to run "
                "the gate here; gate/accept.sh on the node is the real check"
            )

    def test_accept_passes_every_fixture(self):
        with tempfile.TemporaryDirectory() as artefacts:
            try:
                result = subprocess.run(
                    [str(ACCEPT), str(FIXTURES), artefacts],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=ACCEPT_TIMEOUT_S,
                    cwd=str(REPO_ROOT),
                )
            except subprocess.TimeoutExpired:
                self.fail(
                    f"gate/accept.sh did not finish within {ACCEPT_TIMEOUT_S}s"
                )
        output = result.stdout + result.stderr
        # Exit 3 is the gate's refusal, and the only refusals reachable from
        # here are environmental: no browser downloaded yet. Skip rather than
        # fail — the same rule as a missing package.
        if result.returncode == 3 or "Executable doesn't exist" in output:
            raise unittest.SkipTest(
                "the gate refused for want of a browser — run "
                "`playwright install chromium`:\n" + output[-800:]
            )
        self.assertEqual(
            result.returncode,
            0,
            "gate/accept.sh reported a mismatch:\n" + output[-4000:],
        )
        self.assertIn("0 mismatch(es)", output)


def _gate_module():
    """Import sketch_gate.py by path. It is a script, not a package member."""
    spec = importlib.util.spec_from_file_location("sketch_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FrameBudgetTests(unittest.TestCase):
    """The budget's arithmetic and its sentences, with no browser in sight.

    `accept.sh` is what proves the budget fails a real sketch; these are the
    parts that can be checked on a laptop, and the sentence is one of them —
    the worker feeds it to REPAIR verbatim, so what it says is the interface.
    """

    @classmethod
    def setUpClass(cls):
        cls.gate = _gate_module()

    def budget(self, *, ms=100.0, seconds=90.0, age=0.0):
        b = self.gate.Budget(ms, seconds, time.time() - age)
        b.counting = True
        return b

    def test_frame_budget_is_failable(self):
        self.assertIn("frame_budget", self.gate.FAILABLE_CHECKS)

    def test_the_defaults_are_the_calibrated_ones(self):
        self.assertEqual(self.gate.DEFAULT_FRAME_BUDGET_MS, 100.0)
        self.assertEqual(self.gate.DEFAULT_BUDGET_S, 90.0)

    def test_an_ordinary_sketch_passes_the_whole_window(self):
        b = self.budget()
        for frames in (42, 42, 36):
            b.record(frames, frames * 0.012)
        passed, note = b.verdict()
        self.assertTrue(passed)
        self.assertEqual(b.ms_per_frame, 12.0)
        self.assertIn("120-frame idle window", note)

    def test_a_sketch_just_over_the_budget_finishes_the_window_and_fails(self):
        # 150 ms a frame is over the budget but under twice it, so the run is
        # not cut short: the check is about the mean over the window.
        b = self.budget()
        for frames in (42, 42, 36):
            b.record(frames, frames * 0.150)
        passed, note = b.verdict()
        self.assertFalse(passed)
        self.assertIn("150 ms per frame over the 120-frame idle window", note)
        self.assertIn("budget 100 ms", note)

    def test_job_166_attempt_3_trips_early_and_says_so(self):
        b = self.budget()
        with self.assertRaises(self.gate.BudgetExceeded) as caught:
            b.record(30, 30 * 1.093)
        sentence = str(caught.exception)
        self.assertTrue(sentence.startswith("frame_budget: 1093 ms per frame"))
        self.assertIn("of the 120-frame idle window", sentence)
        self.assertIn("budget 100 ms", sentence)
        self.assertIn("batch points and lines into one shape", sentence)

    def test_the_wall_ceiling_stops_a_run_that_is_cheap_per_frame(self):
        # 13 ms a frame would pass forever; what it cannot do is keep running
        # past the ceiling, which is what a 284 s pass looked like.
        b = self.budget(age=91.0)
        with self.assertRaises(self.gate.BudgetExceeded) as caught:
            b.record(30, 0.4)
        sentence = str(caught.exception)
        self.assertIn("90 s wall ceiling", sentence)
        self.assertIn("budget 100 ms", sentence)

    def test_the_deadline_applies_outside_the_counted_window(self):
        b = self.budget(age=91.0)
        b.counting = False
        with self.assertRaises(self.gate.BudgetExceeded):
            b.record(30, 0.4)

    def test_nothing_stepped_is_not_a_verdict(self):
        b = self.budget()
        self.assertIsNone(b.ms_per_frame)
        self.assertEqual(b.verdict(), (None, None))

    def test_the_fixture_that_must_fail_the_budget_is_in_the_repo(self):
        fixture = FIXTURES / "bad-frame-budget"
        self.assertTrue((fixture / "sketch.js").is_file())
        self.assertTrue((fixture / "index.html").is_file())
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        self.assertEqual(expected["bad-frame-budget"]["exit"], 1)
        self.assertIs(expected["bad-frame-budget"]["checks"]["frame_budget"], False)

    def test_every_other_fixture_expects_to_clear_the_budget(self):
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        for name, want in expected.items():
            if name in ("assertions_expected", "bad-frame-budget"):
                continue
            self.assertIs(
                want["checks"].get("frame_budget"), True,
                f"{name} should clear the frame budget",
            )


class FakeResponse:
    """Enough of a Playwright Response for ResourceLog, and nothing more.

    The real one needs a browser; what ResourceLog reads off it is a URL, a
    status, the headers dict and — only from :meth:`ResourceLog.measure`, never
    from inside a handler — the body. So a fake is honest here, and it is the
    only way these four rules get checked on a laptop at all.
    """

    def __init__(self, url, status=200, headers=None, body=b"", request=None):
        self.url = url
        self.status = status
        self.headers = dict(headers or {})
        self._body = body
        self.request = request or FakeRequest(url)

    def body(self):
        return self._body


class FakeRequest:
    def __init__(self, url, resource_type="image", failure=None):
        self.url = url
        self.resource_type = resource_type
        self.failure = failure


class ResourceLogTests(unittest.TestCase):
    """What arrived and what did not, as the log records it.

    media-assertion.md §3.1. The list is evidence and never a check, so nothing
    here is about pass or fail; it is about the four rules that decide what goes
    in it — off the sketch's own origin, an `image/*` content type, a 2xx
    status, and at most MAX_RESOURCES_LOADED of them.
    """

    @classmethod
    def setUpClass(cls):
        cls.gate = _gate_module()

    def log(self):
        return self.gate.ResourceLog()

    def png(self, url, **kwargs):
        headers = {"content-type": "image/png", "content-length": "1234"}
        headers.update(kwargs.pop("headers", {}))
        return FakeResponse(url, headers=headers, **kwargs)

    def test_an_off_origin_image_is_an_arrival(self):
        log = self.log()
        log._on_response(self.png("https://picsum.photos/seed/x/400/300"))
        self.assertEqual(1, len(log.loaded))
        item = log.loaded[0]
        self.assertEqual("picsum.photos", item["host"])
        self.assertEqual("image/png", item["type"])
        self.assertEqual(1234, item["bytes"])
        self.assertEqual("https://picsum.photos/seed/x/400/300", item["url"])
        # Nothing failed, so the failures list is untouched: one response
        # cannot be both.
        self.assertEqual([], log.failures)

    def test_a_page_is_not_an_image(self):
        # The content type decides and nothing else does. p5 itself comes off
        # cdnjs on every single run and must never appear in this list.
        log = self.log()
        log._on_response(FakeResponse("https://example.test/index.html",
                                      headers={"content-type": "text/html"}))
        log._on_response(FakeResponse(
            "https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/p5.min.js",
            headers={"content-type": "application/javascript"}))
        self.assertEqual([], log.loaded)

    def test_a_missing_image_is_a_failure_and_not_an_arrival(self):
        log = self.log()
        log._on_response(FakeResponse("https://example.test/gone.jpg", status=404,
                                      headers={"content-type": "image/jpeg"}))
        self.assertEqual([], log.loaded)
        self.assertEqual(1, len(log.failures))
        self.assertEqual("HTTP 404", log.failures[0]["why"])
        self.assertIn("did not get it: HTTP 404", log.notes()[0])

    def test_the_sketch_s_own_files_are_not_arrivals(self):
        # They come off disk through the page.route() handler and cannot fail;
        # an image inside the sketch directory is not the sketch reaching
        # outside itself, which is what the word is about.
        log = self.log()
        log._on_response(self.png(self.gate.SKETCH_ORIGIN + "/tile.png"))
        self.assertEqual([], log.loaded)

    def test_a_data_uri_is_not_the_web(self):
        # DECIDE[image-pass], and the bad-image-data-uri fixture. Chromium
        # reports a response for a data: URL and for the blob: URL p5 hands the
        # <img> after it; counting either would pass the word on a sketch that
        # never left the page.
        log = self.log()
        log._on_response(self.png("data:image/png;base64,AAAA"))
        log._on_response(self.png("blob:http://sketch.localhost/0-0-0"))
        self.assertEqual([], log.loaded)

    def test_the_cap_holds(self):
        log = self.log()
        for at in range(self.gate.MAX_RESOURCES_LOADED + 4):
            log._on_response(self.png("https://example.test/%d.png" % at))
        self.assertEqual(self.gate.MAX_RESOURCES_LOADED, len(log.loaded))
        self.assertEqual("https://example.test/0.png", log.loaded[0]["url"])

    def test_the_same_url_twice_is_one_arrival(self):
        # p5 1.11.3 fetches the URL and then gives an <img> the same src, so
        # the same picture arrives twice for one loadImage().
        log = self.log()
        for _ in range(3):
            log._on_response(self.png("https://example.test/one.png"))
        self.assertEqual(1, len(log.loaded))

    def test_a_host_that_declares_no_size_is_measured_afterwards(self):
        # And measured OUTSIDE the response handler: a body read from inside
        # one is a round trip on the dispatcher's own greenlet.
        log = self.log()
        response = FakeResponse("https://example.test/plain.png",
                                headers={"content-type": "image/png"},
                                body=b"x" * 4096)
        log._on_response(response)
        self.assertIsNone(log.loaded[0]["bytes"])
        log.measure()
        self.assertEqual(4096, log.loaded[0]["bytes"])
        # Idempotent: the run calls it twice, once after load and once before
        # the assertions, because an image can arrive in setup() too.
        log.measure()
        self.assertEqual(4096, log.loaded[0]["bytes"])

    def test_an_unreadable_body_leaves_the_size_unknown_not_zero(self):
        class Unreadable(FakeResponse):
            def body(self):
                raise RuntimeError("the response is gone")

        log = self.log()
        log._on_response(Unreadable("https://example.test/plain.png",
                                    headers={"content-type": "image/png"}))
        log.measure()
        self.assertIsNone(log.loaded[0]["bytes"])
        self.assertIn("size unknown", log.arrival_notes()[0])

    def test_the_timing_is_from_the_request_leaving(self):
        log = self.log()
        request = FakeRequest("https://example.test/timed.png")
        log._on_request(request)
        log._on_response(self.png("https://example.test/timed.png",
                                  request=request))
        self.assertIsNotNone(log.loaded[0]["ms"])
        self.assertGreaterEqual(log.loaded[0]["ms"], 0)

    def test_an_arrival_nobody_timed_says_so(self):
        # A response whose request was never seen — the cap on the timing dict,
        # or a redirect chain — is null ms, not 0 ms. A blank is true and a
        # zero is a claim.
        log = self.log()
        log._on_response(self.png("https://example.test/untimed.png"))
        self.assertIsNone(log.loaded[0]["ms"])
        self.assertIn("timing unknown", log.arrival_notes()[0])

    def test_a_broken_response_object_is_ignored_not_raised(self):
        # This runs inside a Playwright event handler. An exception out of
        # there is a run that dies for a reason no report.json can name.
        class Broken:
            @property
            def url(self):
                raise RuntimeError("no")

        log = self.log()
        log._on_response(Broken())
        log._on_request(Broken())
        self.assertEqual([], log.loaded)
        self.assertEqual([], log.failures)


class LoadsImageVerdictTests(unittest.TestCase):
    """The four ways loads(image) misses, and the one way it passes.

    Each detail is read by the next attempt and acted on, which is why they are
    four different sentences and not one: entry 429 was handed "0 pixels
    changed" eight times and spent every attempt rewriting a click handler that
    already worked. `image_verdict` is factored out of `evaluate_assertion` so
    that what they say can be checked without a browser.
    """

    @classmethod
    def setUpClass(cls):
        cls.gate = _gate_module()

    ARRIVED = {"url": "https://picsum.photos/seed/x/800/600",
               "host": "picsum.photos", "type": "image/jpeg",
               "bytes": 61234, "ms": 340}
    LINES = ["the sketch asked for https://picsum.photos/400/400 (image) and "
             "did not get it: net::ERR_FAILED"]

    def test_a_picture_that_arrived_and_was_drawn_passes(self):
        got = self.gate.image_verdict([self.ARRIVED], [], False, True, 60.0)
        self.assertTrue(got["pass"])
        # The sentence media-assertion.md §3.1 writes out, to the comma.
        self.assertEqual("1 image arrived: picsum.photos (image/jpeg, 61 kB, "
                         "340 ms); canvas drawn", got["detail"])

    def test_a_picture_that_arrived_and_was_not_drawn_misses(self):
        got = self.gate.image_verdict([self.ARRIVED], [], True, True, 60.0)
        self.assertFalse(got["pass"])
        self.assertIn("picsum.photos", got["detail"])
        self.assertIn("one flat colour", got["detail"])

    def test_nothing_arrived_carries_the_resource_lines(self):
        got = self.gate.image_verdict([], self.LINES, False, True, 60.0)
        self.assertFalse(got["pass"])
        self.assertTrue(got["detail"].startswith(
            "no image arrived from outside the sketch"))
        self.assertIn("net::ERR_FAILED", got["detail"])

    def test_no_canvas_names_the_timeout_and_the_lines(self):
        got = self.gate.image_verdict([], self.LINES, None, True, 60.0)
        self.assertFalse(got["pass"])
        self.assertIn("no canvas after 60 s (preload never finished)",
                      got["detail"])
        self.assertIn("net::ERR_FAILED", got["detail"])

    def test_no_canvas_and_nothing_to_report_is_still_that_sentence(self):
        got = self.gate.image_verdict([], [], None, False, 45.0)
        self.assertFalse(got["pass"])
        self.assertEqual("no canvas after 45 s (preload never finished)",
                         got["detail"])

    def test_a_data_uri_is_told_from_a_network_fault(self):
        # Nothing arrived, nothing failed, the page asked nobody — and there is
        # something on the canvas. Saying "no image arrived" here would send
        # the next attempt after a fault that is not there.
        got = self.gate.image_verdict([], [], False, False, 60.0)
        self.assertFalse(got["pass"])
        self.assertIn("the only image is a data: URI, which is not the web",
                      got["detail"])

    def test_a_flat_canvas_with_no_request_is_not_the_data_uri_sentence(self):
        # A sketch that drew nothing and asked for nothing is the ordinary
        # miss: there is no picture to have been a data: URI.
        got = self.gate.image_verdict([], [], True, False, 60.0)
        self.assertFalse(got["pass"])
        self.assertEqual("no image arrived from outside the sketch",
                         got["detail"])

    def test_several_pictures_are_all_named(self):
        second = dict(self.ARRIVED, host="upload.wikimedia.org",
                      type="image/png", bytes=None, ms=None)
        got = self.gate.image_verdict([self.ARRIVED, second], [], False, True, 60.0)
        self.assertTrue(got["pass"])
        self.assertTrue(got["detail"].startswith("2 images arrived: "))
        self.assertIn("upload.wikimedia.org (image/png, size unknown, "
                      "timing unknown)", got["detail"])


class LoadsImageWiringTests(unittest.TestCase):
    """The word in the vocabulary, and the wait that moves when it is asked for."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _gate_module()

    def test_the_word_is_in_the_vocabulary_and_needs_no_parsing(self):
        self.assertIn("loads(image)", self.gate.VOCAB)
        self.assertIn("loads(image)", self.gate.SIMPLE_ASSERTIONS)
        self.assertEqual([("loads(image)", "loads(image)", None)],
                         self.gate.normalise_assertions(["loads(image)"]))
        # Eight words now, and the --help epilog is built from the same list,
        # so an agent reading it sees what the planner was given.
        self.assertEqual(8, len(self.gate.VOCAB))

    def test_an_unknown_word_is_still_a_refusal(self):
        with self.assertRaises(SystemExit) as caught:
            self.gate.normalise_assertions(["loads(video)"])
        self.assertEqual(3, caught.exception.code)

    def test_the_verdict_helper_is_what_the_branch_calls(self):
        """The branch reads the page and hands the facts on; a fake page is
        enough to prove which facts, and that they arrive in the right order."""
        gate = self.gate
        asked = []

        class FakePage:
            def evaluate(self, script, *args):
                asked.append(script)
                if "flat" in script:
                    return False
                if "fetched" in script:
                    return [{"name": "https://picsum.photos/seed/x/800/600",
                             "kind": "fetch"},
                            {"name": "blob:http://sketch.localhost/1",
                             "kind": "img"}]
                raise AssertionError("unexpected evaluate: %s" % script)

        log = gate.ResourceLog()
        log._on_response(FakeResponse("https://picsum.photos/seed/x/800/600",
                                      headers={"content-type": "image/jpeg",
                                               "content-length": "61234"}))
        got = gate.evaluate_assertion(FakePage(), "loads(image)", None, {},
                                      None, None, resources=log, timeout_s=60.0)
        self.assertTrue(got["pass"])
        self.assertIn("picsum.photos (image/jpeg, 61 kB", got["detail"])
        self.assertEqual(2, len(asked))

    def test_the_canvas_wait_is_a_parameter_with_the_old_default(self):
        # Every run that asserts nothing about images keeps the ten seconds it
        # always had; only the asserted run waits out the whole --timeout
        # (DECIDE[image-wait]).
        self.assertEqual(10000, self.gate.CANVAS_WAIT_MS)
        signature = inspect.signature(self.gate.load_sketch)
        self.assertEqual(self.gate.CANVAS_WAIT_MS,
                         signature.parameters["wait_for_canvas_ms"].default)

    def test_the_canvas_wait_polls_on_a_timer_and_not_on_a_frame(self):
        """The bug this packet found, pinned so it cannot come back.

        Playwright's `wait_for_function` defaults to `polling='raf'`, and this
        file replaces requestAnimationFrame with a queue that nothing drains
        until the run steps it — after this wait. So the predicate was
        evaluated once and the wait then sat out its whole cap: entry 429's
        reports on the node read `load_s` 10.19 s on nine of ten attempts, for
        a photograph that arrives in a tenth of a second. Dropping the `polling`
        argument in a tidy-up would restore that silently, and the only symptom
        would be every preload() sketch costing ten seconds again.
        """
        seen = {}

        class FakePage:
            def goto(self, url, **kwargs):
                seen["goto"] = (url, kwargs)

            def wait_for_function(self, expression, **kwargs):
                seen["wait"] = (expression, kwargs)

        load_s, preload_s = self.gate.load_sketch(FakePage(), 60000, 60000)
        self.assertEqual(self.gate.CANVAS_POLL_MS, seen["wait"][1]["polling"])
        self.assertEqual(60000, seen["wait"][1]["timeout"])
        self.assertIn("canvas()", seen["wait"][0])
        # And the wait it reports is a real measurement, not the cap.
        self.assertLess(preload_s, 1.0)
        self.assertGreaterEqual(load_s, preload_s)

    def test_the_wait_never_runs_longer_than_the_page_timeout(self):
        # --timeout is the ceiling on both halves: a caller asking for a 60 s
        # canvas wait on a 5 s timeout gets 5 s, which is what the old
        # min(timeout_ms, 10000) meant and still means.
        seen = {}

        class FakePage:
            def goto(self, url, **kwargs):
                pass

            def wait_for_function(self, expression, **kwargs):
                seen.update(kwargs)

        self.gate.load_sketch(FakePage(), 5000, 60000)
        self.assertEqual(5000, seen["timeout"])

    def test_the_three_image_fixtures_are_in_the_repo_and_expected(self):
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        for name, verdict in (("good-image", True),
                              ("bad-image-missing", False),
                              ("bad-image-data-uri", False)):
            with self.subTest(name):
                self.assertTrue((FIXTURES / name / "sketch.js").is_file())
                self.assertTrue((FIXTURES / name / "index.html").is_file())
                self.assertIs(
                    expected["assertions_expected"][name]["loads(image)"],
                    verdict)
                # Each one is about a different sentence, and accept.sh is what
                # compares it on the node.
                self.assertTrue(expected[name]["assertion_detail"]["loads(image)"])

    def test_the_good_image_fixture_uses_a_seeded_url(self):
        # DECIDE[image-determinism]: an unseeded picsum URL is allowed by the
        # gate and would make this fixture's own picture a different one every
        # run, so the strip could never be compared with itself.
        source = (FIXTURES / "good-image" / "sketch.js").read_text(encoding="utf-8")
        self.assertIn("https://picsum.photos/seed/sketchgen/400/300", source)
        self.assertIn("function preload()", source)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class InstanceHookTests(unittest.TestCase):
    """The hook that finds the sketch's p5, run in node (job 1542, 2026-09-24).

    p5 1.11's Graphics constructor runs p5.prototype._initializeInstanceVariables
    on the buffer it is building, and the gate's hook on that method kept every
    `this` it saw. So from the first createGraphics() on, is_looping,
    frame_advancing, uses(webgl) and size(w,h) were read off a buffer: 92
    entries on the node, all but one reading is_looping false. The three
    fixtures are the real proof and need Playwright; this is the same three
    cases against the real INIT_JS, so a laptop without a browser still fails
    when the guard goes.
    """

    @classmethod
    def setUpClass(cls):
        gate = _gate_module()
        with tempfile.TemporaryDirectory() as tmp:
            init = Path(tmp) / "init.js"
            init.write_text(gate.INIT_JS.replace("__SEED__", "1"), encoding="utf-8")
            done = subprocess.run(
                [shutil.which("node"), str(HOOK_HARNESS), str(init)],
                capture_output=True, text=True, timeout=60,
            )
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.report = json.loads(done.stdout.strip().splitlines()[-1])

    def test_a_buffer_made_after_the_canvas_is_not_the_sketch(self):
        got = self.report["webgl_sketch_2d_buffer"]
        self.assertTrue(got["inst_is_sketch"])
        # The buffer itself has no draw loop, which is the whole bug: read off
        # it, a sketch that never called noLoop() declared itself static.
        self.assertFalse(got["buffer_is_looping"])
        self.assertIs(True, got["isLooping"])
        self.assertEqual(86, got["frameCount"])
        self.assertEqual((640, 480), (got["width"], got["height"]))

    def test_uses_webgl_reads_the_sketch_s_renderer_both_ways(self):
        self.assertIs(True, self.report["webgl_sketch_2d_buffer"]["webgl"])
        reverse = self.report["two_d_sketch_webgl_buffer"]
        self.assertTrue(reverse["inst_is_sketch"])
        self.assertIs(False, reverse["webgl"])
        self.assertEqual((640, 480), (reverse["width"], reverse["height"]))

    def test_the_seed_hook_is_the_sketch_s_alone(self):
        # Instance mode seeds through an accessor on `setup`; a buffer made
        # inside setup() must neither take the instance's place nor get one.
        got = self.report["instance_mode"]
        self.assertTrue(got["inst_is_sketch"])
        self.assertTrue(got["seeded"])
        self.assertEqual("p5 instance setup", got["hook"])
        self.assertTrue(got["accessor_on_sketch"])
        self.assertFalse(got["accessor_on_buffer"])

    def test_the_three_graphics_fixtures_are_in_the_repo_and_expected(self):
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        for name in ("good-graphics", "good-webgl-2d-buffer", "good-2d-webgl-buffer"):
            with self.subTest(name):
                source = (FIXTURES / name / "sketch.js").read_text(encoding="utf-8")
                code = "\n".join(line.split("//", 1)[0] for line in source.splitlines())
                # Their comments talk about noLoop(); their code never calls it,
                # which is what makes is_looping false a misread and not a
                # declaration.
                self.assertIn("createGraphics(", code)
                self.assertNotIn("noLoop(", code)
                self.assertIs(True, expected[name]["checks"]["is_looping"])
                self.assertIs(True, expected[name]["checks"]["frame_advancing"])
        wants = expected["assertions_expected"]
        self.assertIs(True, wants["good-graphics"]["size(640,480)"])
        self.assertIs(True, wants["good-webgl-2d-buffer"]["uses(webgl)"])
        self.assertIs(False, wants["good-2d-webgl-buffer"]["uses(webgl)"])


#: Scripts both validators are run over, and what each one is. The gate's copy
#: of the rules and `executor.validate_ghost` have to agree on every one of
#: them: the executor is what lets a script onto disk and the gate is what
#: plays it, so a gate that refused what the executor accepted would hand the
#: entry the built-in instead — which looks exactly like a working entry.
GHOST_SAMPLES = [
    ("one move", '[{"t": 0, "type": "move", "x": 0, "y": 0}]'),
    ("a click and a drag",
     (FIXTURES / "ghost-echo" / "ghost.json").read_text(encoding="utf-8")),
    ("out of order",
     '[{"t": 9, "type": "up", "x": 1, "y": 1},'
     ' {"t": 1, "type": "down", "x": 0, "y": 0}]'),
    ("not JSON", "the pointer goes left a bit"),
    ("not a list", '{"t": 0, "type": "move", "x": 0, "y": 0}'),
    ("empty", "[]"),
    ("too many", json.dumps([{"t": 1, "type": "move", "x": 0.5, "y": 0.5}] * 65)),
    ("not an object", '["move"]'),
    ("a fifth key",
     '[{"t": 1, "type": "move", "x": 0.5, "y": 0.5, "button": 0}]'),
    ("a missing key", '[{"t": 1, "type": "move", "x": 0.5}]'),
    ("t as a word", '[{"t": "soon", "type": "move", "x": 0.5, "y": 0.5}]'),
    ("t past the cap", '[{"t": 8001, "type": "move", "x": 0.5, "y": 0.5}]'),
    ("t negative", '[{"t": -1, "type": "move", "x": 0.5, "y": 0.5}]'),
    ("a type nobody plays",
     '[{"t": 1, "type": "wheel", "x": 0.5, "y": 0.5}]'),
    ("x off the canvas", '[{"t": 1, "type": "move", "x": 4, "y": 0.5}]'),
    ("y as true", '[{"t": 1, "type": "move", "x": 0.5, "y": true}]'),
]


class GhostWindowTests(unittest.TestCase):
    """The ghost window's pure-Python half (auto-mouse.md §5, 2026-09-21).

    Everything here is a thing two copies could disagree about. The gate is a
    standalone script that imports nothing from the package — it has its own
    copy on the node and a third in the course repo — so the caps, the four
    event types, the built-in scripts and the rule for what a script may be are
    written twice on purpose, and this is the file that says they are the same
    twice. `tests/test_executor.py`'s SOUND_RE parity test is the pattern.
    """

    @classmethod
    def setUpClass(cls):
        cls.gate = _gate_module()

    def test_the_built_in_scripts_are_the_shim_s(self):
        self.assertEqual(ghostshim.BUILTINS, self.gate.GHOST_BUILTINS)

    def test_the_caps_and_the_types_are_the_shim_s(self):
        self.assertEqual(ghostshim.MAX_EVENTS, self.gate.GHOST_MAX_EVENTS)
        self.assertEqual(ghostshim.MAX_MS, self.gate.GHOST_MAX_MS)
        self.assertEqual(ghostshim.TYPES, self.gate.GHOST_TYPES)
        self.assertEqual(executor.GHOST_KEYS, self.gate.GHOST_KEYS)

    def test_the_gap_between_two_built_ins_is_the_shim_s(self):
        # The one number that lives only in the shim's JavaScript: the shim
        # offsets a second built-in by this much, and a gate that used another
        # would play the same two scripts at different times.
        found = re.search(r"\bvar GAP_MS = (\d+);", ghostshim.script_js())
        self.assertIsNotNone(found, "the shim declares no GAP_MS")
        self.assertEqual(int(found.group(1)), self.gate.GHOST_GAP_MS)

    def test_the_two_validators_agree_on_every_sample(self):
        for what, text in GHOST_SAMPLES:
            with self.subTest(what):
                mine, why_mine = self.gate.validate_ghost(text)
                theirs, why_theirs = executor.validate_ghost(text)
                self.assertEqual(theirs, mine)
                self.assertEqual(why_theirs, why_mine)

    def test_a_list_out_of_order_is_played_in_order_not_refused(self):
        events, why = self.gate.validate_ghost(
            '[{"t": 9, "type": "up", "x": 1, "y": 1},'
            ' {"t": 1, "type": "down", "x": 0, "y": 0}]')
        self.assertIsNone(why)
        self.assertEqual([1, 9], [event["t"] for event in events])

    def wanted(self, *words):
        return [(word, word, None) for word in words]

    def test_the_default_script_follows_the_assertions(self):
        self.assertEqual("click",
                         self.gate.ghost_default_name(self.wanted("responds(click)")))
        self.assertEqual("drag",
                         self.gate.ghost_default_name(self.wanted("responds(drag)")))
        # Both, in that order: a sketch is clicked before it is dragged.
        self.assertEqual(
            "click,drag",
            self.gate.ghost_default_name(
                self.wanted("responds(drag)", "responds(click)")))
        # Every run gets a window, so a sketch that asked for no interaction
        # at all is crossed rather than left alone (DECIDE[ghost-who]).
        self.assertEqual("wander", self.gate.ghost_default_name([]))
        self.assertEqual("wander",
                         self.gate.ghost_default_name(self.wanted("motion(idle)")))

    def test_two_built_ins_are_joined_the_way_the_shim_joins_them(self):
        both = self.gate.ghost_builtin("click,drag")
        click = ghostshim.BUILTINS["click"]
        drag = ghostshim.BUILTINS["drag"]
        self.assertEqual(len(click) + len(drag), len(both))
        shift = max(event["t"] for event in click) + self.gate.GHOST_GAP_MS
        self.assertEqual([event["t"] + shift for event in drag],
                         [event["t"] for event in both[len(click):]])
        self.assertLessEqual(max(event["t"] for event in both),
                             self.gate.GHOST_MAX_MS)

    def test_a_joined_script_is_still_held_to_the_caps(self):
        # Nothing the gate plays may be longer than what the shim would play,
        # or the two players show the sketch different things.
        long = self.gate.ghost_builtin("wander,wander,wander")
        self.assertLessEqual(len(long), self.gate.GHOST_MAX_EVENTS)
        self.assertLessEqual(max(event["t"] for event in long),
                             self.gate.GHOST_MAX_MS)

    def test_a_sketch_with_no_script_of_its_own_gets_the_default(self):
        with tempfile.TemporaryDirectory() as empty:
            notes = []
            events, source, name = self.gate.ghost_script(
                empty, self.wanted("responds(click)"), notes)
            self.assertEqual(("default", "click"), (source, name))
            self.assertEqual(ghostshim.BUILTINS["click"], events)
            self.assertEqual([], notes)

    def test_an_invalid_script_is_a_note_and_the_default_not_a_refusal(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "ghost.json").write_text("[]", encoding="utf-8")
            notes = []
            events, source, name = self.gate.ghost_script(home, [], notes)
            self.assertEqual(("default", "wander"), (source, name))
            self.assertEqual(ghostshim.BUILTINS["wander"], events)
            self.assertEqual(1, len(notes))
            self.assertIn("nothing to play", notes[0])

    def test_the_echo_fixture_carries_the_script_the_expectation_counts(self):
        fixture = FIXTURES / "ghost-echo"
        events, why = self.gate.validate_ghost(
            (fixture / "ghost.json").read_text(encoding="utf-8"))
        self.assertIsNone(why)
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        want = expected["ghost-echo"]["ghost"]
        self.assertEqual({"source": "executor", "events": len(events),
                          "played": len(events)}, want)
        # And the gate reads it from the directory rather than being told.
        notes = []
        read, source, name = self.gate.ghost_script(fixture, [], notes)
        self.assertEqual((events, "executor", None, []), (read, source, name, notes))

    def test_the_console_is_read_up_to_the_window_and_not_through_it(self):
        # The promise HARNESS_VERSION 3 makes: nothing the gate fails changed.
        # A pointer clicking where the probe did not may not turn a sketch that
        # passed into a console_clean failure.
        rec = self.gate.Recorder()
        rec.entries = [{"t": "x", "type": "log", "text": "hello"},
                       {"t": "x", "type": "pageerror", "text": "boom"}]
        self.assertTrue(rec.clean_through(1))
        self.assertFalse(rec.clean_through())
        self.assertFalse(rec.clean)

    def test_the_ghost_window_can_be_turned_off(self):
        self.assertTrue(self.gate.parse_args(["dir"]).ghost)
        self.assertFalse(self.gate.parse_args(["dir", "--no-ghost"]).ghost)
