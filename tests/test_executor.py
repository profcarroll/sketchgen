"""Unit tests for sketchgen.executor — the output contract and the refusals.

These are the control the build plan's section 0 rule requires: the parser has
to pass them on saved responses before any model is asked, so that a failure
later is known to be the model's and not the parser's.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import executor, ghostshim, soundshim  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "executor"

# The statement in clean.txt, exactly as written there, em dash and all.
CLEAN_STATEMENT = (
    "Sixty small suns wander a night field, each on its own private path through a\n"
    "noise landscape, so the group never quite agrees on a direction. A click is a\n"
    "shove: everything near the cursor is thrown outward at once and then, as the\n"
    "damping takes hold, forgets it and returns to drifting. I wanted the recovery to\n"
    "be the subject rather than the scatter — the piece is about how quickly a crowd\n"
    "goes back to what it was doing."
)

SWAPPED_STATEMENT = (
    "A ring of bars listens to the room and leans toward whatever is loudest; when\n"
    "nothing is happening it settles into a slow tide instead of going flat, because\n"
    "silence should still look alive. The colour is one warm hue pushed through a\n"
    "narrow range of brightness, so the motion is the only thing that changes."
)


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="sketchgen-exec-"))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def replay(self, fixture, **kwargs):
        kwargs.setdefault("brief", "a test brief")
        kwargs.setdefault("assertions", ["motion(idle)", "responds(click)"])
        kwargs.setdefault("rules_file", "treatment")
        return executor.run(
            out_dir=self.out, stub=FIXTURES / fixture, **kwargs
        )

    def written(self):
        return sorted(p.name for p in self.out.iterdir())


class TestCleanResponse(ExecutorTestCase):
    """js block, no html block, a statement: the shape the contract asks for."""

    def test_expected_files_and_blocks(self):
        result = self.replay("clean.txt")
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        self.assertEqual(result.blocks, ["js", "statement"])
        self.assertEqual(
            self.written(),
            [
                "index.html",
                "prompt.txt",
                "response.txt",
                "result.json",
                "sketch.js",
                "statement.md",
            ],
        )

    def test_sketch_js_is_the_whole_block(self):
        self.replay("clean.txt")
        js = (self.out / "sketch.js").read_text(encoding="utf-8")
        self.assertTrue(js.startswith("// Sixty circles drifting"))
        self.assertIn("function mousePressed()", js)
        self.assertTrue(js.endswith("}\n"))
        self.assertNotIn("```", js)

    def test_default_index_html_when_no_html_block(self):
        result = self.replay("clean.txt")
        self.assertEqual(result.index_source, "default")
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html, ghostshim.with_shim(executor.DEFAULT_INDEX_HTML))
        self.assertIn("p5.js/1.11.3/p5.min.js", html)
        self.assertNotIn("p5.sound", html)
        # The ghost pointer is on every page the executor writes, sound or
        # not, and it is after the sketch, because p5 attaches its handlers
        # when sketch.js runs and the shim only dispatches (ghostshim.py).
        self.assertEqual(1, html.count(ghostshim.MARKER))
        self.assertLess(html.index('src="sketch.js"'), html.index(ghostshim.MARKER))
        # Inert, though, until something puts ?ghost= in the URL: this is the
        # page the entry page and the swipe feed load too.
        self.assertIn('param("ghost")', html)

    def test_the_fallback_index_loads_p5_sound_when_the_sketch_needs_it(self):
        """The library trap, closed.

        prompts/rules/treatment.md warns about this in its loudest section and
        25 of the 27 treatment-arm sketches that reached for p5.sound did not
        get it. Every gate run that ever satisfied responds(audio) came from a
        sketch that never touched the library.
        """
        for source in ("let mic = new p5.AudioIn();",
                       "let fft = new p5.FFT();",
                       "let osc = new p5.Oscillator();",
                       "song = loadSound('x.mp3');"):
            with self.subTest(source=source):
                html, how = executor.index_html_for(source)
                self.assertEqual("default+p5.sound", how)
                self.assertIn("addons/p5.sound.min.js", html)
                # the addon goes UNDER p5 itself, which has to load first
                self.assertLess(html.index("p5.min.js"), html.index("p5.sound.min.js"))
                # and the shim that lets it start inside a sandboxed frame on
                # WebKit goes under the addon and before the sketch
                # (soundshim.py)
                self.assertEqual(1, html.count(soundshim.MARKER))
                self.assertLess(html.index("p5.sound.min.js"), html.index(soundshim.MARKER))
                self.assertLess(html.index(soundshim.MARKER), html.index('src="sketch.js"'))
                # and the ghost pointer goes after the sketch, so the whole
                # order is p5 -> addon -> sound marker -> sketch.js -> ghost
                # marker (auto-mouse.md §3.4)
                self.assertEqual(1, html.count(ghostshim.MARKER))
                self.assertLess(html.index('src="sketch.js"'), html.index(ghostshim.MARKER))

    def test_a_sketch_with_no_sound_gets_the_index_byte_for_byte(self):
        # DEFAULT_INDEX_HTML is the gate's own fixtures/good-motion index; a
        # sketch that never mentions sound must still get exactly those bytes,
        # and the only thing added to them is the ghost pointer, which every
        # page carries and which does nothing without ?ghost= in the URL.
        html, how = executor.index_html_for("function setup(){ createCanvas(9, 9); }")
        self.assertEqual("default", how)
        self.assertEqual(ghostshim.with_shim(executor.DEFAULT_INDEX_HTML), html)
        self.assertEqual(
            executor.DEFAULT_INDEX_HTML,
            html.replace(ghostshim.SHIM, ""),
            "the ghost shim is the only difference, and it comes out cleanly",
        )

    def test_the_sound_names_are_the_gate_s_own(self):
        """If one list moves the other must; the gate is the authority."""
        gate = (REPO_ROOT / "gate" / "sketch_gate.py").read_text(encoding="utf-8")
        self.assertIn(executor.SOUND_RE.pattern, gate)

    def test_statement_is_stored_verbatim(self):
        self.replay("clean.txt")
        statement = (self.out / "statement.md").read_text(encoding="utf-8")
        self.assertEqual(statement, CLEAN_STATEMENT + "\n")

    def test_raw_response_and_result_json_always_written(self):
        result = self.replay("clean.txt")
        raw = (self.out / "response.txt").read_text(encoding="utf-8")
        self.assertEqual(raw, (FIXTURES / "clean.txt").read_text(encoding="utf-8"))
        record = json.loads((self.out / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(record["prompt_version"], "executor-v3")
        self.assertEqual(record["blocks"], ["js", "statement"])
        self.assertEqual(record["seed"], executor.DEFAULT_SEED)
        self.assertEqual(record["num_ctx"], executor.DEFAULT_NUM_CTX)
        self.assertEqual(record["assertions"], ["motion(idle)", "responds(click)"])
        self.assertEqual(record["sketch_js_lines"], result.sketch_js_lines)

    def test_stub_run_reports_no_rates_or_tokens(self):
        result = self.replay("clean.txt")
        self.assertEqual(
            result.tokens, {"prompt_eval_count": None, "eval_count": None}
        )
        self.assertEqual(
            result.rates, {"prefill_tok_s": None, "decode_tok_s": None}
        )
        self.assertIsNone(result.durations_ns["eval_duration"])


class TestNoStatement(ExecutorTestCase):
    """Code only. Still a usable attempt; the entry simply has no statement."""

    def test_files_and_blocks(self):
        result = self.replay("no-statement.txt")
        self.assertTrue(result.ok)
        self.assertEqual(result.blocks, ["js"])
        self.assertEqual(
            self.written(),
            ["index.html", "prompt.txt", "response.txt", "result.json", "sketch.js"],
        )
        self.assertFalse((self.out / "statement.md").exists())

    def test_default_index_used(self):
        result = self.replay("no-statement.txt")
        self.assertEqual(result.index_source, "default")


class TestSwappedOrder(ExecutorTestCase):
    """Statement first, then html, then js, with chatter around all three."""

    def test_all_three_found_despite_order(self):
        result = self.replay("swapped.txt")
        self.assertTrue(result.ok)
        self.assertEqual(result.blocks, ["js", "html", "statement"])

    def test_model_index_html_is_used_and_carries_the_addon(self):
        result = self.replay("swapped.txt")
        self.assertEqual(result.index_source, "model")
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("addons/p5.sound.min.js", html)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertTrue(html.rstrip().endswith("</html>"))

    def test_chatter_is_not_in_the_sketch(self):
        self.replay("swapped.txt")
        js = (self.out / "sketch.js").read_text(encoding="utf-8")
        self.assertTrue(js.startswith("// A ring of bars"))
        self.assertNotIn("let me know if you want", js)
        self.assertNotIn("<!DOCTYPE", js)

    def test_statement_stops_before_the_chatter(self):
        self.replay("swapped.txt")
        statement = (self.out / "statement.md").read_text(encoding="utf-8")
        self.assertEqual(statement, SWAPPED_STATEMENT + "\n")


class TestGhostBlock(ExecutorTestCase):
    """The optional pointer script (auto-mouse.md §4.1, DECIDE[ghost-dataset]).

    The thing to keep true here is that it is optional in both directions: a
    reply that carries one is the same reply plus a file, and a reply whose
    block is nonsense is still the reply. An attempt is one of three and the
    sketch is what it is for.
    """

    def test_the_sketch_is_the_same_sketch_and_the_script_is_beside_it(self):
        result = self.replay("ghost.txt")
        self.assertTrue(result.ok)
        self.assertEqual(result.blocks, ["js", "ghost", "statement"])
        self.assertEqual(result.ghost, {"events": 6})
        self.assertIn("ghost.json", self.written())
        self.assertIn("ghost.json", result.files)
        js = (self.out / "sketch.js").read_text(encoding="utf-8")
        self.assertTrue(js.startswith("// A jigsaw"))
        self.assertEqual(result.index_source, "default")

    def test_the_script_is_written_sorted_by_t(self):
        # The shim schedules one setTimeout per event, so a list that runs
        # backwards plays backwards and nothing says so. This fixture's
        # second event is its earliest, on purpose.
        self.replay("ghost.txt")
        text = (self.out / "ghost.json").read_text(encoding="utf-8")
        events = json.loads(text)
        self.assertEqual([400, 1200, 1400, 1900, 2100, 2800],
                         [e["t"] for e in events])
        self.assertEqual({"t", "type", "x", "y"}, set(events[0]))
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.dumps(events, indent=2, sort_keys=True) + "\n", text)

    def test_result_json_says_how_many_events_it_kept(self):
        self.replay("ghost.txt")
        record = json.loads((self.out / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(record["ghost"], {"events": 6})
        self.assertEqual(record["extra_blocks"]["ghost"], 0)

    def test_an_invalid_script_is_dropped_and_the_reply_still_stands(self):
        # The whole of DECIDE[ghost-dataset]'s "optionally": rejecting a
        # sketch over an extra it did not have to write would spend one of
        # three attempts for nothing, and there is a built-in script waiting.
        result = self.replay("ghost-invalid.txt")
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        self.assertEqual(result.ghost, {"rejected": "event 2: x is 1.4, outside [0, 1]"})
        self.assertFalse((self.out / "ghost.json").exists())
        self.assertTrue((self.out / "sketch.js").is_file())
        record = json.loads((self.out / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(record["ghost"]["rejected"],
                         "event 2: x is 1.4, outside [0, 1]")

    def test_a_second_script_is_counted_and_the_first_is_the_one(self):
        result = self.replay("ghost-twice.txt")
        self.assertTrue(result.ok)
        self.assertEqual(result.extra_blocks["ghost"], 1)
        self.assertEqual(result.ghost, {"events": 2})
        events = json.loads((self.out / "ghost.json").read_text(encoding="utf-8"))
        self.assertEqual([0.25, 0.25], [events[0]["x"], events[1]["x"]])

    def test_a_reply_with_no_ghost_block_says_so_in_result_json(self):
        result = self.replay("clean.txt")
        self.assertIsNone(result.ghost)
        self.assertEqual(result.extra_blocks["ghost"], 0)
        self.assertFalse((self.out / "ghost.json").exists())

    def test_a_ghost_block_with_no_js_block_is_rejected_as_it_always_was(self):
        # The ghost is an extra on an answer, never the answer.
        bad = self.out / "ghost-only.txt"
        bad.write_text(
            "```ghost\n"
            '[{"t": 100, "type": "move", "x": 0.5, "y": 0.5}]\n'
            "```\n",
            encoding="utf-8",
        )
        result = executor.run(
            brief="a test brief", assertions=["motion(idle)"],
            rules_file="treatment", out_dir=self.out, stub=bad,
        )
        self.assertFalse(result.ok)
        self.assertIn("no fenced js block", result.error)
        self.assertFalse((self.out / "ghost.json").exists())
        self.assertIsNone(result.ghost)


class TestValidateGhost(unittest.TestCase):
    """One reason per refusal, written for the model that wrote the block."""

    GOOD = '[{"t": 100, "type": "move", "x": 0.5, "y": 0.25}]'

    def test_a_good_script_comes_back_as_events(self):
        events, why = executor.validate_ghost(self.GOOD)
        self.assertIsNone(why)
        self.assertEqual([{"t": 100, "type": "move", "x": 0.5, "y": 0.25}], events)

    def test_every_type_the_shim_knows_is_accepted(self):
        for kind in ghostshim.TYPES:
            with self.subTest(type=kind):
                events, why = executor.validate_ghost(
                    '[{"t": 0, "type": "%s", "x": 0, "y": 1}]' % kind)
                self.assertIsNone(why)
                self.assertEqual(kind, events[0]["type"])

    def test_the_edges_of_the_ranges_are_inside_them(self):
        events, why = executor.validate_ghost(
            '[{"t": %d, "type": "up", "x": 1, "y": 0}]' % ghostshim.MAX_MS)
        self.assertIsNone(why)
        self.assertEqual(ghostshim.MAX_MS, events[0]["t"])

    def test_the_caps_are_the_shim_s_own_and_a_long_script_says_both_numbers(self):
        one = '{"t": 1, "type": "move", "x": 0, "y": 0}'
        at_cap = "[%s]" % ",".join([one] * ghostshim.MAX_EVENTS)
        self.assertIsNone(executor.validate_ghost(at_cap)[1])
        over = "[%s]" % ",".join([one] * (ghostshim.MAX_EVENTS + 7))
        self.assertEqual(
            "%d events, more than %d" % (ghostshim.MAX_EVENTS + 7, ghostshim.MAX_EVENTS),
            executor.validate_ghost(over)[1],
        )

    def test_the_reasons(self):
        for text, reason in (
            ("not json at all", "not a JSON list"),
            ('{"t": 1}', "not a JSON list"),
            ("[]", "an empty list, so there is nothing to play"),
            ("[42]", "event 1: not an object"),
            ('[{"t": 1, "type": "move", "x": 0}]',
             "event 1: its keys are t, type, x, not t/type/x/y"),
            ('[{"t": 1, "type": "move", "x": 0, "y": 0, "button": 1}]',
             "event 1: its keys are button, t, type, x, y, not t/type/x/y"),
            ('[{"t": "soon", "type": "move", "x": 0, "y": 0}]',
             'event 1: t is "soon", not a number'),
            ('[{"t": -1, "type": "move", "x": 0, "y": 0}]',
             "event 1: t is -1, outside [0, 8000] ms"),
            ('[{"t": 12000, "type": "move", "x": 0, "y": 0}]',
             "event 1: t is 12000, outside [0, 8000] ms"),
            ('[{"t": 1, "type": "hover", "x": 0, "y": 0}]',
             'event 1: type is "hover", not one of move/down/up/click'),
            ('[{"t": 1, "type": "move", "x": 0, "y": 0}, '
             '{"t": 2, "type": "move", "x": 1.4, "y": 0}]',
             "event 2: x is 1.4, outside [0, 1]"),
            ('[{"t": 1, "type": "move", "x": 0, "y": -0.5}]',
             "event 1: y is -0.5, outside [0, 1]"),
            # true is an int in Python and would otherwise be an x at the
            # right-hand edge of the canvas. It is a typo, not a coordinate.
            ('[{"t": 1, "type": "move", "x": true, "y": 0}]',
             "event 1: x is true, not a number"),
        ):
            with self.subTest(text=text):
                events, why = executor.validate_ghost(text)
                self.assertIsNone(events)
                self.assertEqual(reason, why)

    def test_a_refusal_is_one_line_and_names_the_event(self):
        for text in ('[{"t": 1, "type": "move", "x": 9, "y": 0}]', "[]", "{}"):
            with self.subTest(text=text):
                why = executor.validate_ghost(text)[1]
                self.assertEqual([why], why.splitlines())

    def test_events_at_the_same_millisecond_keep_their_order(self):
        events, why = executor.validate_ghost(
            '[{"t": 5, "type": "down", "x": 0.1, "y": 0.1},'
            ' {"t": 5, "type": "up", "x": 0.2, "y": 0.2},'
            ' {"t": 1, "type": "move", "x": 0.3, "y": 0.3}]'
        )
        self.assertIsNone(why)
        self.assertEqual(["move", "down", "up"], [e["type"] for e in events])

    def test_the_built_ins_are_scripts_this_would_accept(self):
        # The floor under every entry has to clear the bar the executor's own
        # scripts are held to, or the default would be a script the gate
        # would refuse from a model.
        for name, events in ghostshim.BUILTINS.items():
            with self.subTest(script=name):
                back, why = executor.validate_ghost(json.dumps(events))
                self.assertIsNone(why)
                self.assertEqual(events, back)


class TestMalformedResponse(ExecutorTestCase):
    """No js block: failure, not a crash, and the raw text is kept."""

    def test_no_js_block_is_a_failure_with_evidence(self):
        bad = self.out / "bad.txt"
        bad.write_text(
            "I cannot write that sketch without knowing the canvas size.\n"
            "\n"
            "## Statement\n"
            "\n"
            "Nothing was made.\n",
            encoding="utf-8",
        )
        result = executor.run(
            brief="a test brief",
            assertions=["motion(idle)"],
            rules_file="treatment",
            out_dir=self.out,
            stub=bad,
        )
        self.assertFalse(result.ok)
        self.assertIn("no fenced js block", result.error)
        self.assertFalse((self.out / "sketch.js").exists())
        self.assertFalse((self.out / "index.html").exists())
        record = json.loads((self.out / "result.json").read_text(encoding="utf-8"))
        self.assertFalse(record["ok"])
        self.assertIn("no fenced js block", record["error"])
        self.assertIn(
            "I cannot write that sketch",
            (self.out / "response.txt").read_text(encoding="utf-8"),
        )


class TestRefusals(ExecutorTestCase):
    """Exit 3 territory: a word the gate cannot evaluate, a rules file that is not there."""

    def test_unknown_assertion_word_is_refused(self):
        with self.assertRaises(executor.ExecutorRefused) as caught:
            self.replay("clean.txt", assertions=["bogus()"])
        self.assertIn("bogus()", str(caught.exception))

    def test_vocabulary_words_are_accepted(self):
        accepted = executor.normalise_assertions(
            ["motion(idle)", "responds(click)", "responds(drag)", "responds(audio)",
             "uses(webgl)", "no_motion", "size(800,600)"]
        )
        self.assertEqual(len(accepted), 7)
        self.assertEqual(accepted[-1], ("size(800,600)", "size", (800, 600)))

    def test_near_miss_words_are_still_refused(self):
        for word in ("motion", "motion(click)", "responds()", "size(800)", "Motion(idle)"):
            with self.subTest(word=word):
                with self.assertRaises(executor.ExecutorRefused):
                    executor.normalise_assertions([word])

    def test_missing_rules_file_is_refused(self):
        with self.assertRaises(executor.ExecutorRefused):
            self.replay("clean.txt", rules_file=str(self.out / "no-such-rules.md"))

    def test_missing_stub_file_is_refused(self):
        with self.assertRaises(executor.ExecutorRefused):
            executor.run(
                brief="a test brief",
                assertions=["motion(idle)"],
                rules_file="treatment",
                out_dir=self.out,
                stub=self.out / "no-such-response.txt",
            )


class TestPrompt(unittest.TestCase):
    """The template is versioned, the code reads the version, and it is filled."""

    def test_version_line_is_read_from_the_template(self):
        self.assertEqual(executor.prompt_version(), "executor-v3")

    def test_rendered_prompt_carries_rules_brief_assertions_and_seed(self):
        rules = executor.resolve_rules("treatment").read_text(encoding="utf-8")
        rendered = executor.render_prompt(
            "a field of circles",
            executor.normalise_assertions(["motion(idle)", "size(800,600)"]),
            rules,
            7,
        )
        self.assertIn("a field of circles", rendered)
        self.assertIn("The library trap", rendered)
        self.assertIn("`motion(idle)` — the canvas must change on its own", rendered)
        self.assertIn("exactly 800 by 600 pixels", rendered)
        self.assertIn("seeds p5 with 7", rendered)
        self.assertNotIn("${", rendered)

    def test_rendering_is_deterministic(self):
        rules = executor.resolve_rules("control").read_text(encoding="utf-8")
        args = ("a brief", executor.normalise_assertions(["no_motion"]), rules, 1)
        self.assertEqual(executor.render_prompt(*args), executor.render_prompt(*args))

    def test_template_is_under_eighty_lines(self):
        lines = executor.TEMPLATE_PATH.read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 80)


if __name__ == "__main__":
    unittest.main()
