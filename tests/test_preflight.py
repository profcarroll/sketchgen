"""Unit tests for sketchgen.preflight — the p5 name a sketch hid from itself.

Run:  python3 -m unittest tests.test_preflight

Three of the fixtures are real sketches, copied out of the attempts that burned
their repair budget on this bug: job 16 on ``line``, job 34 and job 13 on
``scale``. Each has to yield exactly one finding, which is the whole test: all
three also declare a harmless ``hue`` or ``alpha`` over a p5 function they never
call, and a scan that reported those would hand the repair a second sentence
pointing at working code.

The two passing sketches under tests/fixtures/sketches/ are the gate's own
references. Nothing in them is shadowed and nothing may be reported.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sketchgen import db  # noqa: E402
from sketchgen import preflight  # noqa: E402
from sketchgen import worker  # noqa: E402

import test_worker  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"
SHADOWING = REPO_ROOT / "tests" / "fixtures" / "shadowing"
SKETCHES = REPO_ROOT / "tests" / "fixtures" / "sketches"

LINE_SENTENCE = "your variable `line` hides p5's `line()` function; rename it"
SCALE_SENTENCE = "your variable `scale` hides p5's `scale()` function; rename it"
WIDTH_SENTENCE = "your variable `width` hides p5's `width`; rename it"


def run_cli(*args: str):
    """Run the CLI in a subprocess so the exit codes are the real ones."""
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class RealSketchTests(unittest.TestCase):
    """The three attempts that actually crashed, each with one thing wrong."""

    def one(self, name):
        source = (SHADOWING / name).read_text(encoding="utf-8")
        found = preflight.scan(source)
        self.assertEqual(1, len(found), found)
        return found[0]

    def test_job16_line(self):
        shadow = self.one("job16-line.js")
        self.assertEqual("line", shadow.name)
        self.assertEqual(31, shadow.line)
        self.assertEqual("function", shadow.kind)
        self.assertEqual(LINE_SENTENCE, shadow.sentence)

    def test_job34_scale(self):
        shadow = self.one("job34-scale.js")
        self.assertEqual("scale", shadow.name)
        self.assertEqual(47, shadow.line)
        self.assertEqual("function", shadow.kind)
        self.assertEqual(SCALE_SENTENCE, shadow.sentence)

    def test_job13_scale(self):
        shadow = self.one("job13-scale.js")
        self.assertEqual("scale", shadow.name)
        self.assertEqual(31, shadow.line)
        self.assertEqual("function", shadow.kind)
        self.assertEqual(SCALE_SENTENCE, shadow.sentence)

    def test_the_harmless_hue_beside_the_crash_is_not_reported(self):
        # job16-line.js line 38 is `let hue = map(...)`, which hides p5's hue()
        # and never calls it. Reporting it would be a second sentence about
        # code that works.
        source = (SHADOWING / "job16-line.js").read_text(encoding="utf-8")
        self.assertIn("let hue =", source)
        self.assertEqual(["line"], [s.name for s in preflight.scan(source)])

    def test_evidence_line_names_the_file_and_the_line(self):
        found = preflight.scan((SHADOWING / "job16-line.js").read_text())
        self.assertEqual(
            [f"preflight: {LINE_SENTENCE} (sketch.js line 31)"],
            preflight.evidence_lines(found),
        )


class PassingSketchTests(unittest.TestCase):
    """The gate's reference sketches. Nothing here may be flagged."""

    def test_good_motion_is_clean(self):
        self.assertEqual([], preflight.scan_dir(SKETCHES / "good-motion"))

    def test_good_static_noloop_is_clean(self):
        self.assertEqual([], preflight.scan_dir(SKETCHES / "good-static-noloop"))


class DeclarationTests(unittest.TestCase):
    """Where a declaration can hide: the forms the executor actually writes."""

    def names(self, source):
        return [(s.name, s.line, s.kind) for s in preflight.scan(source)]

    def test_comma_separated_declarator(self):
        self.assertEqual(
            [("line", 1, "function")],
            self.names("let a = 1, line = 2;\nline(0, 0, 1, 1);\n"),
        )

    def test_for_of_header(self):
        self.assertEqual(
            [("line", 1, "function")],
            self.names("for (let line of lines) {\n  line(1, 2, 3, 4);\n}\n"),
        )

    def test_for_in_header(self):
        self.assertEqual(
            [("scale", 1, "function")],
            self.names("for (const scale in x) {\n  scale(scale);\n}\n"),
        )

    def test_function_parameter(self):
        self.assertEqual(
            [("line", 1, "function")],
            self.names("function draw(line, x) {\n  line(x, 0, x, 10);\n}\n"),
        )

    def test_arrow_parameter_in_parentheses(self):
        self.assertEqual(
            [("line", 1, "function")],
            self.names("const f = (line) => { line(0, 0, 1, 1); };\n"),
        )

    def test_bare_arrow_parameter(self):
        self.assertEqual(
            [("line", 1, "function")],
            self.names("const f = line => { line(0, 0, 1, 1); };\n"),
        )

    def test_function_declaration_over_a_p5_name(self):
        self.assertEqual(
            [("line", 1, "function")],
            self.names("function line(a, b) {}\nfunction draw() { line(1, 2); }\n"),
        )

    def test_a_variable_global_needs_no_call(self):
        self.assertEqual([("width", 1, "variable")], self.names("let width = 5;\n"))
        self.assertEqual(
            f"preflight: {WIDTH_SENTENCE} (sketch.js line 1)",
            preflight.evidence_lines(preflight.scan("let width = 5;\n"))[0],
        )

    def test_line_numbers_are_the_declarations_own(self):
        source = "let a = 1;\n\n\nfor (let line of xs) {\n  line(1, 2, 3, 4);\n}\n"
        self.assertEqual([("line", 4, "function")], self.names(source))

    def test_findings_come_back_in_line_order(self):
        source = (
            "function draw() {\n"
            "  const scale = 2;\n"
            "  scale(scale);\n"
            "  let width = 5;\n"
            "}\n"
        )
        self.assertEqual([2, 4], [s.line for s in preflight.scan(source)])

    def test_the_same_name_declared_twice_is_reported_once_per_line(self):
        source = "let width = 5, height = 6;\nlet width = 7;\n"
        self.assertEqual(
            [("height", 1, "variable"), ("width", 1, "variable"),
             ("width", 2, "variable")],
            self.names(source),
        )

    def test_one_declaration_inside_a_loop_body_is_one_finding(self):
        source = (
            "for (let i = 0; i < 10; i++) {\n"
            "  let width = i;\n"
            "  rect(width, width, 2, 2);\n"
            "}\n"
        )
        self.assertEqual([("width", 2, "variable")], self.names(source))


class NotShadowingTests(unittest.TestCase):
    """The four shapes that look like a declaration and are not."""

    def test_a_line_comment_declares_nothing(self):
        self.assertEqual([], preflight.scan("// let line\nline(0, 0, 1, 1);\n"))

    def test_a_block_comment_declares_nothing(self):
        self.assertEqual([], preflight.scan("/*\nlet line = 1;\n*/\nline(1, 2);\n"))

    def test_a_string_literal_declares_nothing(self):
        self.assertEqual([], preflight.scan('const s = "let line";\nline(1, 2);\n'))

    def test_property_assignment_declares_nothing(self):
        self.assertEqual([], preflight.scan("obj.line = 1;\nobj.line(1, 2);\n"))

    def test_an_object_key_declares_nothing(self):
        self.assertEqual([], preflight.scan("const o = { line: 1 };\nline(1, 2);\n"))

    def test_property_access_on_a_loop_variable_declares_nothing(self):
        source = "for (let seg of segs) {\n  line(seg.x1, seg.y1, seg.x2, seg.y2);\n}\n"
        self.assertEqual([], preflight.scan(source))

    def test_a_hidden_function_nobody_calls_is_left_alone(self):
        self.assertEqual([], preflight.scan("let hue = 200;\nfill(hue, 90, 80);\n"))


class ScanDirTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_a_directory_with_no_sketch_is_empty_not_an_error(self):
        self.assertEqual([], preflight.scan_dir(self.dir))

    def test_a_directory_that_does_not_exist_is_empty_not_an_error(self):
        self.assertEqual([], preflight.scan_dir(self.dir / "nope"))

    def test_it_reads_the_file_the_executor_writes(self):
        self.assertEqual("sketch.js", preflight.SKETCH_FILE)
        (self.dir / "sketch.js").write_text(
            "for (let line of xs) { line(1, 2, 3, 4); }\n", encoding="utf-8"
        )
        self.assertEqual(["line"], [s.name for s in preflight.scan_dir(self.dir)])


class CliTests(unittest.TestCase):
    """0 clean, 1 shadowed, 2 no sketch.js."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_clean_sketch_exits_zero_and_says_nothing(self):
        result = run_cli("preflight", str(SKETCHES / "good-motion"))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())

    def test_shadowed_sketch_exits_one_and_prints_the_sentence(self):
        (self.dir / "sketch.js").write_text(
            (SHADOWING / "job16-line.js").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        result = run_cli("preflight", str(self.dir))
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn(LINE_SENTENCE, result.stdout)
        self.assertIn("sketch.js line 31", result.stdout)

    def test_a_directory_with_no_sketch_exits_two(self):
        result = run_cli("preflight", str(self.dir))
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("no sketch.js", result.stderr)


class WorkerEvidenceTests(test_worker.WorkerTestCase):
    """What a repair attempt is actually handed, with the gate stubbed."""

    SHADOWING_JS = "for (let line of lines) {\n  line(1, 2, 3, 4);\n}\n"

    def evidence_of_first_attempt(self, *, sketch_js, verdicts):
        job_id = self.enqueue(max_attempts=1)
        run = self.make_worker(
            executor_fn=test_worker.StubExecutor(sketch_js=sketch_js),
            gate_fn=test_worker.StubGate(verdicts),
        )
        self.assertEqual(0, run.run_once())
        return job_id, db.list_attempts(self.conn, job_id)[0].evidence

    def test_a_failing_gate_puts_the_preflight_above_what_the_gate_said(self):
        job_id, evidence = self.evidence_of_first_attempt(
            sketch_js=self.SHADOWING_JS, verdicts=[1]
        )
        lines = evidence.splitlines()
        self.assertEqual(worker.PREFLIGHT_HEADING, lines[0])
        self.assertEqual(
            f"preflight: {LINE_SENTENCE} (sketch.js line 1)", lines[1]
        )
        # The gate is still the authority, and still says everything it said.
        self.assertLess(evidence.index("preflight:"), evidence.index("gate exit 1"))
        self.assertIn("frame_advancing = false", evidence)
        # ...and its summary, not the heading, is what the job failed with.
        job = db.get_job(self.conn, job_id)
        self.assertTrue(job.last_error.startswith("gate exit 1:"), job.last_error)

    def test_a_failing_gate_on_a_clean_sketch_adds_nothing(self):
        _, evidence = self.evidence_of_first_attempt(
            sketch_js="function setup() { createCanvas(100, 100); }\n", verdicts=[1]
        )
        self.assertNotIn(worker.PREFLIGHT_HEADING, evidence)
        self.assertNotIn("preflight:", evidence)
        self.assertTrue(evidence.startswith("gate exit 1:"), evidence)

    def test_a_passing_gate_adds_nothing(self):
        _, evidence = self.evidence_of_first_attempt(
            sketch_js=self.SHADOWING_JS, verdicts=[0]
        )
        self.assertIsNone(evidence)

    def test_the_repair_brief_carries_the_sentence(self):
        executor_fn = test_worker.StubExecutor(sketch_js=self.SHADOWING_JS)
        self.enqueue(max_attempts=2)
        run = self.make_worker(
            executor_fn=executor_fn, gate_fn=test_worker.StubGate([1, 0])
        )
        self.assertEqual(0, run.run_once())
        self.assertEqual(2, len(executor_fn.calls))
        self.assertNotIn("preflight:", executor_fn.calls[0]["brief"])
        self.assertIn(LINE_SENTENCE, executor_fn.calls[1]["brief"])

    def test_the_findings_are_in_the_job_log(self):
        stream = test_worker.io.StringIO()
        self.enqueue(max_attempts=1)
        run = self.make_worker(
            executor_fn=test_worker.StubExecutor(sketch_js=self.SHADOWING_JS),
            gate_fn=test_worker.StubGate([1]),
            log_stream=stream,
        )
        self.assertEqual(0, run.run_once())
        self.assertIn(f"preflight: {LINE_SENTENCE}", stream.getvalue())

    def test_a_scan_that_blows_up_is_dropped_not_raised(self):
        def explode(_sketch_dir):
            raise RuntimeError("the scan fell over")

        self.enqueue(max_attempts=1)
        stream = test_worker.io.StringIO()
        run = self.make_worker(
            executor_fn=test_worker.StubExecutor(sketch_js=self.SHADOWING_JS),
            gate_fn=test_worker.StubGate([1]),
            log_stream=stream,
        )
        with test_worker.mock.patch.object(preflight, "scan_dir", explode):
            self.assertEqual(0, run.run_once())
        self.assertIn("preflight scan failed", stream.getvalue())
        job = db.get_job(self.conn, 1)
        self.assertEqual("failed", job.state)
        self.assertTrue(job.last_error.startswith("gate exit 1:"), job.last_error)


if __name__ == "__main__":
    unittest.main()
