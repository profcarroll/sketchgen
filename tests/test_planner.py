"""Unit tests for sketchgen.planner — parsing, the closed vocabulary, the CLI.

This is the control the build plan's section 0 rule requires: an ACCEPT that
asserts something about a model's output has to come with a control that passes
before any model is asked. Every test here runs against a saved response in
``tests/fixtures/planner/``; nothing in this file calls a model.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sketchgen import planner  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "planner"
CLI = REPO_ROOT / "bin" / "sketchgen"


def load(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def parse_and_validate(name: str):
    brief, words = planner.parse_response(load(name))
    ok, rejected, defaulted = planner.validate_detailed(words)
    return brief, ok, rejected, defaulted


def run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


class ParsingTests(unittest.TestCase):
    """One test per saved response: the brief and the assertion list it must give."""

    def test_clean(self):
        brief, ok, rejected, defaulted = parse_and_validate("clean")
        self.assertTrue(brief.startswith("A field of small translucent circles"))
        self.assertTrue(brief.endswith("where two circles overlap."))
        self.assertEqual(ok, ["motion(idle)", "responds(click)", "size(800,600)"])
        self.assertEqual(rejected, [])
        self.assertEqual(defaulted, [])

    def test_made_up_words_land_in_rejected(self):
        brief, ok, rejected, defaulted = parse_and_validate("made-up")
        self.assertTrue(brief.startswith("Thin vertical lines stand across"))
        self.assertEqual(ok, ["motion(idle)", "responds(drag)"])
        self.assertEqual(
            [item["word"] for item in rejected],
            ["responds(keyboard)", "looks_nice"],
        )
        for item in rejected:
            self.assertIn("not in the vocabulary", item["reason"])
        self.assertEqual(defaulted, [])

    def test_none_defaults_motion_idle(self):
        brief, ok, rejected, defaulted = parse_and_validate("none")
        self.assertTrue(brief.startswith("A single grey square"))
        self.assertEqual(ok, ["motion(idle)"])
        self.assertEqual(rejected, [])
        self.assertEqual(defaulted, ["motion(idle)"])

    def test_conflict_keeps_the_first_and_records_the_second(self):
        brief, ok, rejected, defaulted = parse_and_validate("conflict")
        self.assertTrue(brief.startswith("Concentric rings of dots"))
        self.assertEqual(ok, ["motion(idle)", "responds(click)"])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["word"], "no_motion")
        self.assertIn("mutually exclusive with motion(idle)", rejected[0]["reason"])
        self.assertEqual(defaulted, [])

    def test_chatty_still_parses(self):
        """Preamble, bolded headings, bullets and trailing commentary all survive."""
        brief, ok, rejected, defaulted = parse_and_validate("chatty")
        self.assertTrue(brief.startswith("A dense mesh of pale lines"))
        self.assertTrue(brief.endswith("from wherever it was left."))
        self.assertNotIn("Sure!", brief)
        self.assertEqual(ok, ["motion(idle)", "responds(drag)", "uses(webgl)"])
        self.assertEqual(rejected, [])
        self.assertEqual(defaulted, [])

    def test_no_brief_heading_raises(self):
        with self.assertRaises(planner.PlannerFailed) as caught:
            planner.parse_response("Assertions\nmotion(idle)\n")
        self.assertIn("motion(idle)", caught.exception.raw)


class ValidatorTests(unittest.TestCase):

    def test_size_with_integers_is_accepted_and_canonicalised(self):
        ok, rejected = planner.validate(["size(800,600)", "size( 640 , 480 )"])
        self.assertIn("size(800,600)", ok)
        self.assertIn("size(640,480)", ok)
        self.assertEqual(rejected, [])

    def test_size_with_a_word_is_rejected(self):
        ok, rejected = planner.validate(["size(big)", "motion(idle)"])
        self.assertEqual(ok, ["motion(idle)"])
        self.assertEqual([item["word"] for item in rejected], ["size(big)"])

    def test_the_literal_placeholder_is_rejected(self):
        ok, rejected = planner.validate(["size(w,h)", "no_motion"])
        self.assertEqual(ok, ["no_motion"])
        self.assertEqual([item["word"] for item in rejected], ["size(w,h)"])

    def test_duplicates_are_dropped(self):
        ok, rejected = planner.validate(["responds(click)", "responds(click)"])
        self.assertEqual(ok, ["responds(click)", "motion(idle)"])
        self.assertEqual([item["reason"] for item in rejected],
                         ["duplicate of an earlier line"])

    def test_no_motion_alone_is_not_overridden(self):
        ok, rejected = planner.validate(["no_motion"])
        self.assertEqual(ok, ["no_motion"])
        self.assertEqual(rejected, [])

    def test_every_vocabulary_word_validates(self):
        for word in planner.VOCAB:
            candidate = "size(320,240)" if word == "size(w,h)" else word
            ok, rejected = planner.validate([candidate])
            self.assertIn(candidate, ok, word)
            self.assertEqual(rejected, [], word)


class PlanTests(unittest.TestCase):

    def test_stub_plan_reports_the_prompt_version_from_the_file(self):
        result = planner.plan(
            "circles that drift",
            "gemma4:e4b",
            stub=FIXTURES / "clean.txt",
            by="someone",
        )
        self.assertEqual(result.prompt_version, "planner-v1")
        self.assertEqual(result.assertions,
                         ["motion(idle)", "responds(click)", "size(800,600)"])
        self.assertEqual(result.raw, load("clean"))
        self.assertEqual(result.tokens, {})

    def test_a_missing_stub_is_a_refusal(self):
        with self.assertRaises(planner.PlannerRefused):
            planner.plan("x", "gemma4:e4b", stub=FIXTURES / "no-such-file.txt")

    def test_the_prompt_template_carries_the_whole_vocabulary(self):
        rendered = planner.build_prompt("circles", "someone")
        for word in planner.VOCAB:
            self.assertIn(word, rendered)
        self.assertIn("circles", rendered)
        self.assertNotIn("prompt_version:", rendered)


class CliTests(unittest.TestCase):

    def test_plan_writes_both_files_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli(
                "plan", "--prompt", "x", "--by", "someone", "--model", "gemma4:e4b",
                "--stub", str(FIXTURES / "conflict.txt"), "--out", tmp, "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            self.assertEqual(document["assertions"],
                             ["motion(idle)", "responds(click)"])
            self.assertEqual(document["prompt_version"], "planner-v1")
            self.assertEqual(document["model"], "gemma4:e4b")
            self.assertTrue(document["started_utc"].endswith("Z"))
            on_disk = json.loads((Path(tmp) / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk, document)
            self.assertEqual((Path(tmp) / "response.txt").read_text(encoding="utf-8"),
                             load("conflict"))

    def test_a_response_with_no_brief_heading_exits_one_and_saves_the_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "headless.txt"
            stub.write_text("motion(idle)\nresponds(click)\n", encoding="utf-8")
            out = Path(tmp) / "out"
            result = run_cli(
                "plan", "--prompt", "x", "--by", "someone", "--model", "gemma4:e4b",
                "--stub", str(stub), "--out", str(out),
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Brief", result.stderr)
            self.assertFalse((out / "plan.json").exists())
            self.assertEqual((out / "response.txt").read_text(encoding="utf-8"),
                             "motion(idle)\nresponds(click)\n")

    def test_a_missing_stub_exits_three_with_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli(
                "plan", "--prompt", "x", "--by", "someone", "--model", "gemma4:e4b",
                "--stub", str(Path(tmp) / "nope"), "--out", str(Path(tmp) / "out"),
            )
            self.assertEqual(result.returncode, 3)
            self.assertEqual(len(result.stderr.strip().splitlines()), 1)
            self.assertEqual(result.stdout, "")

    def test_plan_help(self):
        result = run_cli("plan", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--stub", result.stdout)


class TestLenientParse(unittest.TestCase):
    """The rescue the worker reaches for after every strict try has failed."""

    def test_prose_without_a_heading_becomes_the_brief(self):
        raw = ("Sure! Here is the plan:\n\n"
               "Sixty circles drift across a dark field.\n\n"
               "Assertions:\n- motion(idle)\n- responds(click)\n")
        brief, words = planner.parse_response_lenient(raw)
        self.assertEqual("Sixty circles drift across a dark field.", brief)
        self.assertEqual(["motion(idle)", "responds(click)"], words)

    def test_recover_marks_the_version_and_still_validates_the_words(self):
        plan = planner.recover("A still grid of grey rectangles.\n\n"
                               "Assertions:\n- motion(idle)\n- glows(softly)\n")
        self.assertTrue(plan.prompt_version.endswith(planner.LENIENT_MARK))
        self.assertEqual(["motion(idle)"], plan.assertions)  # the invention is dropped
        self.assertEqual(["glows(softly)"], [r["word"] for r in plan.rejected])

    def test_a_reply_with_no_prose_is_still_a_failure(self):
        with self.assertRaises(planner.PlannerFailed):
            planner.parse_response_lenient("Assertions:\n- motion(idle)\n")
        with self.assertRaises(planner.PlannerFailed):
            planner.parse_response_lenient("")

    def test_the_strict_parser_is_unchanged_by_any_of_this(self):
        brief, words = planner.parse_response(
            "Brief\nA field of dots.\n\nAssertions\nmotion(idle)\n"
        )
        self.assertEqual("A field of dots.", brief)
        self.assertEqual(["motion(idle)"], words)
        with self.assertRaises(planner.PlannerFailed):
            planner.parse_response("A field of dots, with no heading at all.\n")


if __name__ == "__main__":
    unittest.main()
