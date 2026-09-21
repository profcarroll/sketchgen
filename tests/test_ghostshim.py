"""ghostshim.py: where the ghost pointer goes on a page, and what it carries.

What it *does* once it is there is behaviour, not text, and is run for real in
node by ``tests/js/ghostshim.js`` under ``tests/test_gallery_js.py``. This file
is the injection, the caps and the one thing the two have to agree on: the
built-ins are Python data, rendered into the script rather than written twice.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import executor, ghostshim, soundshim  # noqa: E402


class WithShimTests(unittest.TestCase):
    def test_the_shim_goes_after_the_sketch_tag(self):
        # After, not before: p5 attaches its mouse handlers to window when
        # sketch.js runs, and this script only dispatches. A shim that ran
        # first would dispatch into a page with nothing listening.
        out = ghostshim.with_shim(executor.DEFAULT_INDEX_HTML)
        self.assertEqual(1, out.count(ghostshim.MARKER))
        self.assertLess(out.index('src="sketch.js"'), out.index(ghostshim.MARKER))
        self.assertTrue(out.rstrip().endswith("</html>"))

    def test_a_page_that_has_it_is_left_alone(self):
        once = ghostshim.with_shim(executor.DEFAULT_INDEX_HTML)
        self.assertEqual(once, ghostshim.with_shim(once))

    def test_a_page_with_no_sketch_tag_is_untouched(self):
        # The render walks every published entry; a page that does not load a
        # sketch.js is not one this can help, and rewriting it would be a
        # change to somebody's file for nothing.
        for page in ("<!DOCTYPE html>\n<html><body></body></html>\n",
                     '<html><body><script>console.log("inline");</script></body></html>',
                     '<html><body><script src="other.js"></script></body></html>'):
            with self.subTest(page=page[:40]):
                self.assertEqual(page, ghostshim.with_shim(page))

    def test_an_executor_s_own_page_gets_it_however_it_spelled_the_tag(self):
        # index_html_for never runs when the model wrote its own html block,
        # so the gallery's copy is where those pages pick the shim up, and a
        # model writes the tag however it likes.
        for tag in ('<script src="sketch.js"></script>',
                    "<script src='sketch.js'></script>",
                    '<script src="./sketch.js"></script>',
                    '<script defer src="sketch.js"></script>',
                    '<script src="sketch.js" type="text/javascript"></script>',
                    '<SCRIPT SRC="sketch.js"></SCRIPT>'):
            with self.subTest(tag=tag):
                page = "<html><body>\n%s\n</body></html>\n" % tag
                out = ghostshim.with_shim(page)
                self.assertEqual(1, out.count(ghostshim.MARKER), tag)
                self.assertLess(out.index(tag), out.index(ghostshim.MARKER))

    def test_a_tag_that_is_not_the_sketch_is_not_the_one(self):
        page = '<html><body><script src="notsketch.js"></script></body></html>'
        self.assertEqual(page, ghostshim.with_shim(page))

    def test_both_shims_on_one_page_come_out_in_the_gate_s_order(self):
        # p5 -> addon -> sound marker -> sketch.js -> ghost marker. The sound
        # shim patches addModule before p5 starts; the ghost dispatches after
        # the sketch is listening. Neither can move (auto-mouse.md §3.4).
        page = executor.DEFAULT_INDEX_HTML.replace(
            executor._P5_TAG, executor._P5_TAG + soundshim.P5_SOUND_TAG)
        out = ghostshim.with_shim(soundshim.with_shim(page))
        order = [out.index("p5.min.js"),
                 out.index("p5.sound.min.js"),
                 out.index(soundshim.MARKER),
                 out.index('src="sketch.js"'),
                 out.index(ghostshim.MARKER)]
        self.assertEqual(sorted(order), order)
        # and in either order of application, because the gallery applies the
        # sound shim first and a page may arrive with one of them already on
        self.assertEqual(out, soundshim.with_shim(ghostshim.with_shim(page)))

    def test_the_shim_is_es5_like_the_rest_of_the_gallery(self):
        self.assertNotIn("=>", ghostshim.SHIM)
        self.assertNotIn("`", ghostshim.SHIM)
        self.assertIsNone(re.search(r"\b(?:let|const)\s+\w+\s*=", ghostshim.SHIM))

    def test_it_says_nothing_at_error(self):
        # console_clean in the gate fails a sketch whose console has an error
        # in it. A shim the sketch did not ask for must not be able to trip
        # that, so the whole player is in one try and failures are debug.
        self.assertNotIn("console.error", ghostshim.SHIM)
        self.assertNotIn("console.warn", ghostshim.SHIM)
        self.assertIn("console.debug", ghostshim.SHIM)

    def test_it_is_inert_before_it_touches_the_dom(self):
        # The one guarantee the entry page, swipe and the preview rest on: the
        # parameter is read first and the return is before any listener, any
        # query and any timer.
        body = ghostshim.script_js()
        reads = body.index('var want = param("ghost");')
        returns = body.index('if (!want || want === "0") { return; }')
        self.assertLess(reads, returns)
        # Nothing that touches the page is even mentioned before the return,
        # so there is no call site to reach without the parameter.
        for call in ("addEventListener", "querySelector", "setTimeout(",
                     "dispatchEvent", "MouseEvent"):
            with self.subTest(call=call):
                self.assertIn(call, body)
                self.assertGreater(body.index(call), returns)


class BuiltinTests(unittest.TestCase):
    """The three default scripts (DECIDE[ghost-script])."""

    def test_the_three_are_the_three(self):
        self.assertEqual(["click", "drag", "wander"], sorted(ghostshim.BUILTINS))

    def test_every_event_is_inside_the_caps(self):
        for name, events in ghostshim.BUILTINS.items():
            with self.subTest(script=name):
                self.assertLessEqual(len(events), ghostshim.MAX_EVENTS)
                self.assertTrue(events)
                for event in events:
                    self.assertEqual({"t", "type", "x", "y"}, set(event))
                    self.assertIn(event["type"], ghostshim.TYPES)
                    self.assertIsInstance(event["t"], int)
                    self.assertGreaterEqual(event["t"], 0)
                    self.assertLessEqual(event["t"], ghostshim.MAX_MS)
                    self.assertGreaterEqual(event["x"], 0.0)
                    self.assertLessEqual(event["x"], 1.0)
                    self.assertGreaterEqual(event["y"], 0.0)
                    self.assertLessEqual(event["y"], 1.0)

    def test_every_script_runs_forward_in_time(self):
        # The shim schedules one setTimeout per event, so an out-of-order t
        # would play out of order and nothing would say so.
        for name, events in ghostshim.BUILTINS.items():
            with self.subTest(script=name):
                stamps = [event["t"] for event in events]
                self.assertEqual(sorted(stamps), stamps)

    def test_a_press_is_always_released(self):
        # A sketch left with mouseIsPressed true would stay dragging for the
        # rest of its minute on the projector.
        for name, events in ghostshim.BUILTINS.items():
            with self.subTest(script=name):
                down = 0
                for event in events:
                    if event["type"] == "down":
                        down += 1
                    elif event["type"] == "up":
                        down -= 1
                    self.assertGreaterEqual(down, 0, "an up with no down")
                self.assertEqual(0, down, "a down with no up")

    def test_click_presses_at_the_centre_and_then_three_places_apart(self):
        events = ghostshim.BUILTINS["click"]
        presses = [e for e in events if e["type"] in ("down", "click")]
        self.assertEqual(4, len(presses))
        self.assertEqual((0.5, 0.5), (presses[0]["x"], presses[0]["y"]))
        places = {(e["x"], e["y"]) for e in presses}
        self.assertEqual(4, len(places), "four presses, four places")

    def test_drag_is_two_legs_that_cross(self):
        events = ghostshim.BUILTINS["drag"]
        self.assertEqual(2, len([e for e in events if e["type"] == "down"]))
        across = [e for e in events if e["type"] == "move" and e["t"] < 1500]
        down = [e for e in events if e["type"] == "move" and e["t"] > 1500]
        self.assertEqual({0.5}, {e["y"] for e in across})
        self.assertEqual({0.5}, {e["x"] for e in down})

    def test_wander_never_presses(self):
        self.assertEqual({"move"}, {e["type"] for e in ghostshim.BUILTINS["wander"]})


class RenderedTests(unittest.TestCase):
    """One definition, two readers (auto-mouse.md §5.1).

    Packet 15's gate is a standalone script with its own copy of these, and the
    test that pins the two lives there. This is the other half: the JavaScript
    the browser runs has to be these exact events and not a transcription of
    them, or the pinning has nothing to pin against.
    """

    def test_the_script_carries_the_built_ins_verbatim(self):
        for name, events in ghostshim.BUILTINS.items():
            with self.subTest(script=name):
                rendered = json.dumps(events, sort_keys=True, separators=(",", ":"))
                self.assertIn('"%s": %s' % (name, rendered), ghostshim.SHIM)

    def test_rendering_is_deterministic(self):
        # A render-all rewrites 910 pages, and the publisher commits what
        # changed. If the rendering moved between two runs of the same code —
        # a dict that iterated differently, a float that repr'd differently —
        # every entry in the gallery would show up as a diff for nothing.
        self.assertEqual(ghostshim._render_builtins(), ghostshim._render_builtins())
        page = '<script src="sketch.js"></script>\n'
        self.assertEqual(ghostshim.with_shim(page), ghostshim.with_shim(page))
        self.assertNotIn("e-", ghostshim._render_builtins(),
                         "a coordinate in exponent notation is a rounding bug")

    def test_the_caps_in_the_script_are_the_caps_in_the_module(self):
        self.assertIn("var MAX_EVENTS = %d;" % ghostshim.MAX_EVENTS, ghostshim.SHIM)
        self.assertIn("var MAX_MS = %d;" % ghostshim.MAX_MS, ghostshim.SHIM)
        self.assertIn("var LOOP_MS = %d;" % ghostshim.DEFAULT_LOOP_MS, ghostshim.SHIM)

    def test_script_js_is_the_shim_without_its_tags(self):
        body = ghostshim.script_js()
        self.assertNotIn("<script", body)
        self.assertNotIn("</script>", body)
        self.assertIn("/* ghost pointer */", body)


if __name__ == "__main__":
    unittest.main()
