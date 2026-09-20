"""soundshim.py: the script under p5.sound that lets a sketch start inside a
sandboxed frame on WebKit, and the two places it is put."""

from __future__ import annotations

import re
import unittest

from sketchgen import executor, soundshim


class WithShimTests(unittest.TestCase):
    def test_a_page_without_the_addon_is_untouched(self):
        self.assertEqual(executor.DEFAULT_INDEX_HTML,
                         soundshim.with_shim(executor.DEFAULT_INDEX_HTML))

    def test_the_shim_goes_under_the_addon_and_before_the_sketch(self):
        page = executor.DEFAULT_INDEX_HTML.replace(
            executor._P5_TAG, executor._P5_TAG + soundshim.P5_SOUND_TAG)
        out = soundshim.with_shim(page)
        self.assertEqual(1, out.count(soundshim.MARKER))
        tag = out.index("p5.sound.min.js")
        self.assertLess(tag, out.index(soundshim.MARKER))
        self.assertLess(out.index(soundshim.MARKER), out.index('src="sketch.js"'))

    def test_a_page_that_has_it_is_left_alone(self):
        page = executor.DEFAULT_INDEX_HTML.replace(
            executor._P5_TAG, executor._P5_TAG + soundshim.P5_SOUND_TAG)
        once = soundshim.with_shim(page)
        self.assertEqual(once, soundshim.with_shim(once))

    def test_the_shim_tries_the_blob_first_and_falls_back_to_a_data_url(self):
        # Chrome and the gate keep p5.sound's own path; only a refusal takes
        # the fallback, and a module that fails both ways is let go.
        self.assertIn("add.call(worklet, url, options).catch(", soundshim.SHIM)
        self.assertIn('"data:application/javascript;charset=utf-8," +', soundshim.SHIM)
        self.assertIn("then(resolve, resolve)", soundshim.SHIM)
        self.assertIn("reader.onerror = function () { resolve(); }", soundshim.SHIM)

    def test_the_shim_is_es5_like_the_rest_of_the_gallery(self):
        self.assertNotIn("=>", soundshim.SHIM)
        self.assertNotIn("`", soundshim.SHIM)
        self.assertIsNone(re.search(r"\b(?:let|const)\s+\w+\s*=", soundshim.SHIM))


if __name__ == "__main__":
    unittest.main()
