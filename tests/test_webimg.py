"""The web copies of the gate's frames (gallery-hub.md, Step 0).

No browser: the encoder is the node's Chromium in a child process, and here
the child is a stub or is refused. What is tested is the part that decides —
which copy is fresh, what happens with no encoder, how the child's report is
read — and the verb that runs the backfill.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_gallery import PNG_BYTES, build_db  # noqa: E402

from sketchgen import webimg  # noqa: E402
from sketchgen.cli import webframes  # noqa: E402


class CachedTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.png = Path(tmp.name) / "strip.png"
        self.png.write_bytes(PNG_BYTES)

    def test_the_copy_lives_beside_its_png(self):
        self.assertEqual(self.png.parent / "strip.webp", webimg.web_path(self.png))

    def test_no_copy_means_the_png(self):
        self.assertIsNone(webimg.cached(self.png))
        self.assertEqual(self.png, webimg.web_or_png(self.png))
        self.assertIsNone(webimg.web_or_png(None))

    def test_a_fresh_copy_is_used(self):
        webimg.web_path(self.png).write_bytes(b"RIFF....WEBP")
        self.assertEqual(webimg.web_path(self.png), webimg.web_or_png(self.png))

    def test_a_stale_or_empty_copy_is_not(self):
        web = webimg.web_path(self.png)
        web.write_bytes(b"RIFF....WEBP")
        os.utime(web, (1, 1))
        self.assertIsNone(webimg.cached(self.png))
        web.write_bytes(b"")
        self.assertIsNone(webimg.cached(self.png))


class EnsureTests(CachedTests):

    def test_no_encoder_writes_nothing_and_says_why(self):
        with mock.patch.dict(os.environ, {webimg.DISABLE_ENV: "1"}):
            self.assertFalse(webimg.available())
            outcome = webimg.ensure([self.png])
        self.assertIn("no encoder", outcome[str(self.png)])
        self.assertFalse(webimg.web_path(self.png).exists())

    def test_a_cached_copy_starts_no_child(self):
        webimg.web_path(self.png).write_bytes(b"RIFF....WEBP")
        with mock.patch.object(subprocess, "run") as run:
            outcome = webimg.ensure([self.png, None])
        run.assert_not_called()
        self.assertEqual({str(self.png): "cached"}, outcome)

    def test_a_missing_png_is_named(self):
        gone = self.png.parent / "gone.png"
        self.assertEqual({str(gone): "no such file"}, webimg.ensure([gone]))

    def test_the_childs_lines_are_the_outcome(self):
        other = self.png.parent / "ghost.png"
        other.write_bytes(PNG_BYTES)
        stdout = "\n".join([
            json.dumps({"png": str(self.png), "outcome": "made"}),
            "a line Playwright printed",
        ])
        done = subprocess.CompletedProcess([], 1, stdout=stdout, stderr="boom\nlast words\n")
        with mock.patch.object(webimg, "available", return_value=True), \
                mock.patch.object(subprocess, "run", return_value=done) as run:
            outcome = webimg.ensure([self.png, other])
        self.assertEqual("made", outcome[str(self.png)])
        # A file the child never reported gets the child's last words.
        self.assertEqual("encoder failed: last words", outcome[str(other)])
        argv = run.call_args.args[0]
        self.assertEqual(["-m", "sketchgen.webimg"], argv[1:3])
        self.assertEqual([str(self.png), str(other)], argv[-2:])

    def test_a_child_that_cannot_start_is_not_an_error(self):
        with mock.patch.object(webimg, "available", return_value=True), \
                mock.patch.object(subprocess, "run", side_effect=OSError("no python")):
            outcome = webimg.ensure([self.png])
        self.assertIn("encoder did not run", outcome[str(self.png)])


class WebFramesVerbTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.conn, self.ids = build_db(self.tmp)
        self.addCleanup(self.conn.close)

    def run_verb(self, *ids, all_=False, dry_run=False):
        args = argparse.Namespace(entry_id=list(ids), all=all_, dry_run=dry_run,
                                  json=True, db=str(self.tmp / "sketchgen.db"))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = webframes.cmd(args)
        return code, out.getvalue(), err.getvalue()

    def test_nothing_named_is_refused(self):
        code, _out, err = self.run_verb()
        self.assertEqual(webframes.EXIT_REFUSED, code)
        self.assertIn("--all", err)

    def test_a_dry_run_counts_and_writes_nothing(self):
        code, out, _err = self.run_verb(all_=True, dry_run=True)
        self.assertEqual(webframes.EXIT_OK, code)
        summary = json.loads(out)
        self.assertEqual(summary["frames"], summary["missing"])
        self.assertGreater(summary["frames"], 0)
        self.assertEqual([], list(self.tmp.rglob("*.webp")))

    def test_no_encoder_is_a_refusal(self):
        with mock.patch.dict(os.environ, {webimg.DISABLE_ENV: "1"}):
            code, _out, err = self.run_verb(all_=True)
        self.assertEqual(webframes.EXIT_REFUSED, code)
        self.assertIn("node's venv", err)

    def test_every_frame_already_copied_is_a_no_op(self):
        pngs = [p for i in self.ids for p in webframes.gallery.frame_pngs(self.conn, i)]
        for png in pngs:
            webimg.web_path(png).write_bytes(b"RIFF....WEBP")
        code, out, _err = self.run_verb(*self.ids)
        self.assertEqual(webframes.EXIT_OK, code)
        self.assertEqual(0, json.loads(out)["missing"])


if __name__ == "__main__":
    unittest.main()
