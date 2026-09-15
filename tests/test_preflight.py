"""Unit tests for sketchgen.preflight — both scans.

Run:  python3 -m unittest tests.test_preflight

Three of the fixtures are real sketches, copied out of the attempts that burned
their repair budget on this bug: job 16 on ``line``, job 34 and job 13 on
``scale``. Each has to yield exactly one finding, which is the whole test: all
three also declare a harmless ``hue`` or ``alpha`` over a p5 function they never
call, and a scan that reported those would hand the repair a second sentence
pointing at working code.

The two passing sketches under tests/fixtures/sketches/ are the gate's own
references. Nothing in them is shadowed and nothing may be reported.

The cost scan has its own three, under tests/fixtures/cost/, and they are the
two unsafe sketches themselves: job 166 attempt 3 and job 270 attempt 1 as the
executor wrote them (kept on the node as sketch.unsafe.js.txt beside the bounded
rewrites that replaced them), and the rewrite of job 270, which must come back
clean. If the first two ever stop tripping, this scan has stopped doing the one
thing it was built for.
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
COST = REPO_ROOT / "tests" / "fixtures" / "cost"

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


# ---------------------------------------------------------------------------
# The cost scan (2026-09-15, after job 166 and job 270)
# ---------------------------------------------------------------------------

WEBGL_HEAD = "function setup() { createCanvas(400, 400, WEBGL); }\n"


def rules(source):
    return sorted({finding.rule for finding in preflight.scan_cost(source)})


class UnsafeSketchTests(unittest.TestCase):
    """The two sketches that actually took the operator's laptop down."""

    def read(self, name):
        return (COST / name).read_text(encoding="utf-8")

    def test_job166_attempt3_trips_all_three_of_its_rules(self):
        found = preflight.scan_cost(self.read("job166-attempt3.unsafe.js"))
        self.assertEqual(
            ["all_pairs", "geometry_in_loop", "immediate_in_loop"],
            sorted({f.rule for f in found}),
        )
        # And each one points at the line that does it, not at the top of draw().
        by_rule = {f.rule: f for f in found}
        self.assertIn("sphere", by_rule["geometry_in_loop"].name)
        self.assertIn("line", by_rule["immediate_in_loop"].name)

    def test_job270_attempt1_trips_even_though_its_pair_loop_is_in_setup(self):
        # The all-pairs walk that built `connections` is in setup(), where it is
        # paid once; what draw() does is a sphere per particle and three batches
        # of immediate-mode line(). The scan must say the second and not the
        # first, or the repair moves the wrong loop.
        found = preflight.scan_cost(self.read("job270-attempt1.unsafe.js"))
        self.assertEqual(
            ["geometry_in_loop", "immediate_in_loop"], sorted({f.rule for f in found})
        )

    def test_the_bounded_rewrite_of_job270_is_clean(self):
        # Same picture, three shapes a frame: one point cloud, one capped batch
        # of neighbour lines, one batch of grid lines. If this ever starts
        # tripping, the scan has learned to refuse the fix as well as the bug.
        self.assertEqual([], preflight.scan_cost(self.read("job270-attempt1.neutralised.js")))

    def test_neither_unsafe_sketch_shadows_anything(self):
        # The two scans are independent, and these two sketches are the case
        # where the cost scan has something to say and the shadow scan does not.
        for name in ("job166-attempt3.unsafe.js", "job270-attempt1.unsafe.js"):
            self.assertEqual([], preflight.scan(self.read(name)), name)

    def test_the_findings_reach_the_evidence_as_one_sentence_each(self):
        lines = preflight.evidence_lines(
            preflight.scan_cost(self.read("job166-attempt3.unsafe.js"))
        )
        self.assertTrue(lines)
        for line in lines:
            self.assertTrue(line.startswith("preflight: "))
            self.assertIn("sketch.js line ", line)
            self.assertEqual(1, line.count("\n") + 1)


class CostRuleTests(unittest.TestCase):
    """Each rule on its own, and the near miss it must not report."""

    def test_geometry_in_a_draw_loop_over_an_array(self):
        source = WEBGL_HEAD + (
            "function draw() { for (let i = 0; i < things.length; i++)"
            " { push(); sphere(3); pop(); } }\n"
        )
        self.assertEqual(["geometry_in_loop"], rules(source))

    def test_geometry_in_a_small_countable_loop_is_left_alone(self):
        source = WEBGL_HEAD + (
            "function draw() { for (let i = 0; i < 8; i++)"
            " { push(); box(10); pop(); } }\n"
        )
        self.assertEqual([], rules(source))

    def test_nested_small_loops_multiply_up_to_the_threshold(self):
        few = WEBGL_HEAD + (
            "function draw() { for (let i = 0; i < 8; i++)"
            " { for (let j = 0; j < 20; j++) { box(2); } } }\n"
        )
        many = WEBGL_HEAD + (
            "function draw() { for (let i = 0; i < 40; i++)"
            " { for (let j = 0; j < 40; j++) { box(2); } } }\n"
        )
        self.assertEqual([], rules(few))          # 160, under the budget
        self.assertEqual(["geometry_in_loop"], rules(many))   # 1,600, over it

    def test_a_named_constant_is_resolved_like_a_literal(self):
        source = ("const N = 12;\n" + WEBGL_HEAD +
                  "function draw() { for (let i = 0; i < N; i++) { sphere(2); } }\n")
        self.assertEqual([], rules(source))

    def test_geometry_outside_a_loop_is_not_a_finding(self):
        source = WEBGL_HEAD + "function draw() { push(); sphere(50); pop(); }\n"
        self.assertEqual([], rules(source))

    def test_geometry_in_a_loop_in_setup_is_not_a_finding(self):
        source = ("function setup() { createCanvas(9, 9, WEBGL);"
                  " for (let i = 0; i < things.length; i++) { sphere(1); } }\n"
                  "function draw() { background(0); }\n")
        self.assertEqual([], rules(source))

    def test_a_2d_sketch_is_not_judged_on_webgl_rules(self):
        source = ("function setup() { createCanvas(400, 400); }\n"
                  "function draw() { for (let i = 0; i < pts.length; i++)"
                  " { line(0, 0, pts[i].x, pts[i].y); } }\n")
        self.assertEqual([], rules(source))

    def test_line_in_a_webgl_draw_loop_is_immediate_mode(self):
        source = WEBGL_HEAD + (
            "function draw() { for (let i = 0; i < pts.length; i++)"
            " { line(0, 0, 0, pts[i].x, pts[i].y, pts[i].z); } }\n"
        )
        self.assertEqual(["immediate_in_loop"], rules(source))
        self.assertIn("beginShape(LINES)", preflight.scan_cost(source)[0].sentence)

    def test_point_gets_the_points_batch_not_the_lines_one(self):
        source = WEBGL_HEAD + (
            "function draw() { for (let i = 0; i < pts.length; i++)"
            " { point(pts[i].x, pts[i].y, pts[i].z); } }\n"
        )
        self.assertIn("beginShape(POINTS)", preflight.scan_cost(source)[0].sentence)

    def test_vertex_inside_a_begin_shape_is_the_fix_and_is_never_reported(self):
        source = WEBGL_HEAD + (
            "function draw() { beginShape(LINES);"
            " for (let i = 0; i < pts.length; i++) { vertex(pts[i].x, pts[i].y); }"
            " endShape(); }\n"
        )
        self.assertEqual([], rules(source))

    def test_the_classic_all_pairs_header(self):
        source = ("function draw() { for (let i = 0; i < ps.length; i++)"
                  " { for (let j = i + 1; j < ps.length; j++) { d(i, j); } } }\n")
        self.assertEqual(["all_pairs"], rules(source))

    def test_two_loops_over_the_same_array(self):
        source = ("function draw() { for (let i = 0; i < ps.length; i++)"
                  " { for (let k = 0; k < ps.length; k++) { d(i, k); } } }\n")
        self.assertEqual(["all_pairs"], rules(source))

    def test_a_grid_is_not_an_all_pairs_loop(self):
        # Two nested loops over the same integer bound is how every grid in this
        # corpus is drawn, and none of them compares every item with every
        # other. Only the `.length` of one array counts as "the same array".
        source = ("const GRID = 20;\n"
                  "function draw() { for (let i = 0; i < GRID; i++)"
                  " { for (let j = 0; j < GRID; j++) { g(i, j); } } }\n")
        self.assertEqual([], rules(source))

    def test_two_loops_over_different_arrays_are_not_all_pairs(self):
        source = ("function draw() { for (let i = 0; i < ps.length; i++)"
                  " { for (let k = 0; k < qs.length; k++) { d(i, k); } } }\n")
        self.assertEqual([], rules(source))

    def test_an_all_pairs_loop_in_setup_is_paid_once(self):
        source = ("function setup() { for (let i = 0; i < ps.length; i++)"
                  " { for (let j = i + 1; j < ps.length; j++) { d(i, j); } } }\n"
                  "function draw() { background(0); }\n")
        self.assertEqual([], rules(source))

    def test_allocation_in_draw_needs_no_loop(self):
        source = "function draw() { let g = createGraphics(200, 200); image(g, 0, 0); }\n"
        self.assertEqual(["allocation_in_draw"], rules(source))

    def test_allocation_in_setup_is_where_it_belongs(self):
        source = ("function setup() { buffer = createGraphics(200, 200); }\n"
                  "function draw() { image(buffer, 0, 0); }\n")
        self.assertEqual([], rules(source))

    def test_filter_in_a_loop_is_a_full_canvas_pass_per_iteration(self):
        # Job 45: twelve BLUR passes a frame, 504 s of gate time, and nobody
        # noticed because the gate said yes.
        source = ("function draw() { for (let i = 0; i < 12; i++)"
                  " { filter(BLUR, i); } }\n")
        self.assertEqual(["filter_in_loop"], rules(source))

    def test_filter_once_a_frame_is_fine(self):
        source = "function draw() { background(0); filter(BLUR, 3); }\n"
        self.assertEqual([], rules(source))

    def test_an_arrays_own_filter_method_is_not_p5s(self):
        source = ("function draw() { for (let i = 0; i < 12; i++)"
                  " { live = items.filter(function (x) { return x.on; }); } }\n")
        self.assertEqual([], rules(source))

    def test_an_instance_mode_draw_is_read_too(self):
        source = ("let s = function (p) { p.setup = function () "
                  "{ p.createCanvas(9, 9, p.WEBGL); };\n"
                  " p.draw = function () { for (let i = 0; i < ps.length; i++)"
                  " { p.push(); sphere(2); p.pop(); } }; };\n")
        self.assertEqual(["geometry_in_loop"], rules(source))

    def test_a_comment_describing_the_bug_is_not_the_bug(self):
        source = WEBGL_HEAD + (
            "function draw() {\n"
            "  // do NOT do: for (...) { sphere(p.size); }\n"
            "  background(0);\n"
            "}\n"
        )
        self.assertEqual([], rules(source))


class ScanAllTests(unittest.TestCase):
    """Both scans, one list, in line order — which is what the worker reads."""

    def test_a_sketch_with_both_kinds_of_finding_reports_both_in_order(self):
        source = (
            "function setup() { createCanvas(9, 9, WEBGL); }\n"
            "function draw() {\n"
            "  let width = 3;\n"
            "  for (let i = 0; i < ps.length; i++) { sphere(width); }\n"
            "}\n"
        )
        found = preflight.scan_all(source)
        self.assertEqual([3, 4], [f.line for f in found])
        self.assertIn("hides p5's `width`", found[0].sentence)
        self.assertIn("one lit mesh per item per frame", found[1].sentence)

    def test_scan_dir_runs_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "sketch.js").write_text(
                WEBGL_HEAD + "function draw() { for (let i = 0; i < ps.length; i++)"
                " { sphere(2); } }\n",
                encoding="utf-8",
            )
            lines = preflight.evidence_lines(preflight.scan_dir(directory))
            self.assertEqual(1, len(lines))
            self.assertIn("sphere()", lines[0])

    def test_the_gates_own_clean_fixtures_stay_clean(self):
        for name in ("good-motion", "good-static-noloop"):
            source = (SKETCHES / name / "sketch.js").read_text(encoding="utf-8")
            self.assertEqual([], preflight.scan_cost(source), name)
