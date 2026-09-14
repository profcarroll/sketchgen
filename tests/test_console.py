"""Unit tests for sketchgen.console — the collector's document and its contract.

Run:  python3 -m unittest discover -s tests -v

The document's key names are a contract between packet 4.1 (this collector) and
packet 4.2 (the web server), so the first test is the one that matters: the
committed fixture tests/fixtures/console/sample.json and a live collect() on a
seeded temporary database must have the *same keys, recursively*. A rename on
either side fails here rather than in the browser.

Nothing in this file calls a model, starts a server or touches the node. The
Ollama host is pointed at a port nothing listens on, which is also the test for
the unreachable-host path.
"""

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import console  # noqa: E402
from sketchgen import db  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "console" / "sample.json"

#: Nothing listens here; urllib refuses at once. Both the "Ollama is not
#: running" path and the "this test must not touch the real one" rule.
DEAD_HOST = "http://127.0.0.1:1"

SKETCH = "function setup() {\n  createCanvas(800, 600);\n}\n\nfunction draw() {}\n"


def gate_report(total_s=4.12, launch_s=0.66):
    """A report.json in sketch_gate.py's schema, cut to what the console reads."""
    return {
        "sketch_dir": "unused",
        "seed": 1,
        "timings": {"launch_s": launch_s, "load_s": 0.31, "total_s": total_s},
        "checks": {"console_clean": True, "is_looping": True},
        "assertions": {},
        "notes": [],
        "console": [],
        "exit": 0,
    }


def keyshape(value):
    """The recursive key skeleton of a document: dicts keep their keys, lists
    keep the skeleton of their elements, everything else collapses to None.

    An empty list matches anything: a live collect() against a dead host has no
    resident models, and the fixture is allowed to show one.
    """
    if isinstance(value, dict):
        return {key: keyshape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        shapes = [keyshape(item) for item in value]
        if not shapes:
            return "list:empty"
        first = shapes[0]
        for other in shapes[1:]:
            if other != first:  # pragma: no cover - a heterogeneous list is a bug
                raise AssertionError(f"mixed shapes in list: {first} vs {other}")
        return ["list:of", first]
    return None


def same_shape(left, right):
    """keyshape equality, with an empty list on either side matching a full one."""
    if left == "list:empty" or right == "list:empty":
        return True
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(same_shape(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return same_shape(left[1], right[1])
    return left == right


def numbers(value, path="$"):
    """Every number in the document, with the path that found it."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from numbers(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from numbers(item, f"{path}[{index}]")


class ConsoleTestCase(unittest.TestCase):
    """A temp database with one finished job and two attempts behind it.

    Two attempts is the smallest population that makes the whole document
    non-trivial: the first fails its gate and the second passes, so the funnel
    has a revision in it, ``model.instant`` has rates to divide and per_sketch
    has a wall time, a gate time and a line count to average.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = str(self.root / "console.db")
        self.jobs = self.root / "jobs"
        db.init(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.job_id = self.seed()

    def seed(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat",
                            brief="a field of sixty circles", assertions=["motion"])
        db.transition(self.conn, job_id, "executing")
        for n, (gate_exit, wall) in enumerate(((1, 130.9), (0, 67.9)), start=1):
            attempt_dir = self.jobs / str(job_id) / f"attempt-{n}"
            (attempt_dir / ".gate").mkdir(parents=True, exist_ok=True)
            (attempt_dir / "sketch.js").write_text(SKETCH, encoding="utf-8")
            report_path = attempt_dir / ".gate" / "report.json"
            report_path.write_text(json.dumps(gate_report()), encoding="utf-8")
            db.add_attempt(
                self.conn,
                job_id,
                n,
                started_utc=db.utc_now(),
                finished_utc=db.utc_now(),
                model="qwen3-coder:30b-a3b-q4_K_M",
                rules_file="treatment",
                prompt_tokens=1066,
                completion_tokens=628,
                prefill_s=11.2,
                decode_s=24.7,
                wall_s=wall,
                source_dir=str(attempt_dir),
                gate_exit=gate_exit,
                gate_report_path=str(report_path),
            )
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "held")
        db.create_entry(self.conn, job_id, state="held", attempts=2, wall_s=198.8)
        return job_id

    def collect(self, **kwargs):
        kwargs.setdefault("host_url", DEAD_HOST)
        return console.collect(self.conn, self.jobs, **kwargs)


class TestContract(ConsoleTestCase):
    def test_live_document_has_the_fixture_s_keys(self):
        sample = json.loads(FIXTURE.read_text(encoding="utf-8"))
        live = self.collect()
        self.assertTrue(
            same_shape(keyshape(sample), keyshape(live)),
            "sample.json and collect() disagree about the document's keys:\n"
            f"sample: {json.dumps(keyshape(sample), sort_keys=True)}\n"
            f"live:   {json.dumps(keyshape(live), sort_keys=True)}",
        )

    def test_the_fixture_and_the_document_both_hold_only_finite_numbers(self):
        for label, document in (
            ("sample.json", json.loads(FIXTURE.read_text(encoding="utf-8"))),
            ("collect()", self.collect()),
        ):
            for path, value in numbers(document):
                with self.subTest(document=label, path=path):
                    self.assertTrue(math.isfinite(value), f"{path} = {value!r}")

    def test_cpu_pct_has_one_entry_per_core(self):
        document = self.collect()
        self.assertEqual(
            document["node"]["cores"], len(document["node"]["cpu_pct"])
        )
        self.assertGreater(document["node"]["cores"], 0)

    def test_the_seeded_database_fills_the_derived_blocks(self):
        document = self.collect()
        instant = document["model"]["instant"]
        self.assertIsNotNone(instant)
        self.assertAlmostEqual(round(1066 / 11.2, 1), instant["prefill_tok_s"])
        self.assertAlmostEqual(round(628 / 24.7, 1), instant["decode_tok_s"])
        self.assertEqual(0.66, document["model"]["gate_launch_s"])
        self.assertEqual(2, document["funnel"]["generated"]["total"])
        self.assertEqual(1, document["funnel"]["revised"]["total"])
        self.assertEqual(1, document["funnel"]["passed_gate"]["total"])
        self.assertEqual(1, document["funnel"]["held"]["total"])
        last = document["per_sketch"]["last"]
        self.assertEqual(2.0, last["attempts_to_pass"])
        self.assertEqual(round(130.9 + 67.9, 1), last["wall_s"])
        self.assertEqual(round(4.12 * 2, 2), last["gate_s"])
        self.assertEqual(float(len(SKETCH.splitlines())), last["sketch_lines"])
        self.assertEqual(0.0, last["first_attempt_pass_rate"])
        self.assertEqual(
            round(last["wall_s"] / 3600.0 * console.RATE_PER_HOUR_16_96, 4),
            last["cost_usd_16_96"],
        )
        # The Always Free shape is free at any duty cycle, by definition.
        self.assertEqual(0.0, last["cost_usd_4_24"])

    def test_collector_reports_its_own_cost(self):
        document = self.collect()
        self.assertGreater(document["collector_ms"], 0.0)
        self.assertTrue(document["utc"].endswith("Z"))


class TestRedaction(ConsoleTestCase):
    def test_an_argument_containing_token_never_reaches_the_document(self):
        line = console.redact_command(
            ["/usr/bin/python3", "serve.py", "--auth-token", "hunter2secretvalue",
             "--port", "8081"]
        )
        self.assertNotIn("token", line.lower())
        self.assertNotIn("hunter2secretvalue", line)
        self.assertTrue(line.startswith("python3 serve.py"))
        self.assertIn("--port 8081", line)

    def test_a_key_flag_hides_its_value_too_and_the_line_is_bounded(self):
        line = console.redact_command(
            ["/opt/x/bin/agent", "--api-key", "sk-live-0123456789", "--model", "x"]
        )
        self.assertNotIn("sk-live-0123456789", line)
        self.assertEqual(2, line.count(console.REDACTED))
        long = console.redact_command(["/bin/thing"] + ["argument"] * 40)
        self.assertLessEqual(len(long), len("thing ") + console.CMD_MAX_CHARS)

    def test_an_inline_secret_is_hidden_without_eating_the_next_argument(self):
        line = console.redact_command(
            ["node", "--password=hunter2", "--seed", "1"]
        )
        self.assertNotIn("hunter2", line)
        self.assertIn("--seed 1", line)


class TestDegradedSources(ConsoleTestCase):
    def test_an_unreachable_ollama_is_an_empty_model_block_not_an_exception(self):
        document = self.collect()
        self.assertEqual([], document["model"]["resident"])
        self.assertIsNone(document["model"]["ollama_version"])
        self.assertIn(document["model"]["slot"]["state"], ("free", "busy", "ours"))

    def test_session_equals_total_when_meta_has_no_start_stamp(self):
        self.assertIsNone(db.get_meta(self.conn, "worker_started_utc"))
        document = self.collect()
        session = document["odometer"]["session"]
        total = document["odometer"]["total"]
        for key in ("in", "out", "wall_s"):
            self.assertEqual(total[key], session[key])
        self.assertIsNone(session["since_utc"])
        self.assertIsNone(document["worker"]["started_utc"])
        for name in console.FUNNEL_NAMES:
            cell = document["funnel"][name]
            self.assertEqual(cell["total"], cell["session"], name)

    def test_a_database_still_on_migration_001_still_renders(self):
        """The node's DB from packet 2.3 has no meta table; that is not an error."""
        self.conn.execute("DROP TABLE meta")
        document = self.collect()
        self.assertIsNone(document["worker"]["started_utc"])
        self.assertEqual(
            document["odometer"]["total"]["in"], document["odometer"]["session"]["in"]
        )

    def test_a_started_stamp_narrows_the_session_to_nothing(self):
        db.set_meta(self.conn, "worker_started_utc", "2999-01-01T00:00:00Z")
        document = self.collect()
        self.assertEqual(0, document["odometer"]["session"]["in"])
        self.assertGreater(document["odometer"]["total"]["in"], 0)
        self.assertEqual(0, document["funnel"]["generated"]["session"])
        self.assertEqual(2, document["funnel"]["generated"]["total"])

    def test_prev_reuses_the_cached_sample_instead_of_sleeping_again(self):
        first = self.collect()
        second = self.collect(prev=first)
        self.assertEqual(first["node"]["cores"], len(second["node"]["cpu_pct"]))
        self.assertLess(second["collector_ms"], first["collector_ms"])


class TestResidentEntry(unittest.TestCase):
    """The /api/ps row, tested as a pure function: nothing here calls Ollama."""

    def test_a_cpu_only_model_reads_as_the_ollama_ps_column_does(self):
        entry = console.resident_entry(
            {
                "name": "qwen3-coder:30b-a3b-q4_K_M",
                "size": 22024237874,
                "size_vram": 0,
                "context_length": 32768,
                "expires_at": "2026-09-14T04:28:06.636797191Z",
            }
        )
        self.assertEqual("100% CPU", entry["processor"])
        self.assertEqual(32768, entry["context"])
        self.assertEqual("2026-09-14T04:28:06Z", entry["until_utc"])
        self.assertEqual(20.51, entry["size_gb"])
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(
            set(fixture["model"]["resident"][0]), set(entry),
            "sample.json's resident row and resident_entry() disagree on keys",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
