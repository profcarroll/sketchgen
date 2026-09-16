"""Unit tests for sketchgen.web — the operator's five screens, packet 4.2.

Run:  python3 -m unittest discover -s tests -v

The server is started in a thread on 127.0.0.1:0 against a temporary database
seeded with three jobs — one queued, one held with an entries row and a real
attempt directory (a .gate/ holding report.json in sketch_gate.py's schema and
PNG bytes that are a PNG), and one failed — and driven over the loopback with
urllib. No model, no browser, no node: every route is exercised against the
database and the jobs directory and nothing else.
"""

import base64
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen import web  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CONSOLE = REPO_ROOT / "tests" / "fixtures" / "console" / "sample.json"

#: A 1x1 PNG, so /jobs/… is asked to serve bytes that really are an image.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

#: What the executor writes into an attempt directory: p5 from cdnjs, which
#: the browser fetches for itself, and a sketch.js beside it referenced by a
#: relative path — which is the whole reason /preview/<job>/<n>/ has to be a
#: directory URL and not a single file.
SKETCH_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>sketch</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/p5.min.js"></script>
</head>
<body>
    <script src="sketch.js"></script>
</body>
</html>
"""
SKETCH_JS = """function setup() { createCanvas(windowWidth, windowHeight); }
function draw() { background(17); ellipse(width / 2, height / 2, 60 + sin(frameCount / 30) * 20); }
"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are the assertion here, not a step on the way somewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def report_json(sketch_dir, *, exit_code=0):
    """One report.json in sketch_gate.py's real schema (see its report dict)."""
    return {
        "sketch_dir": str(sketch_dir),
        "seed": 1,
        "started_utc": "2026-09-14T12:00:00.000Z",
        "chromium": "/home/ubuntu/.cache/ms-playwright/chromium-1234/chrome",
        "timings": {"launch_s": 0.66, "load_s": 0.31, "total_s": 4.2},
        "checks": {
            "console_clean": True,
            "is_looping": True,
            "frame_advancing": True,
            "sound_lib_ok": None,
            "audio_context_running": False,
        },
        "assertions": {
            "motion(idle)": {
                "pass": True,
                "detail": "idle 0->20 frames: mean abs diff 3.1102/255, "
                "9412 of 160000 pixels changed",
            },
            "responds(click)": {"pass": False, "detail": "no change after the click"},
        },
        "notes": ["frameCount advanced 0 -> 21 over 20 idle frames"],
        "console": [{"t": "00:00:01", "type": "error", "text": "boom"}],
        "artefacts": {"png": "gate.png", "strip": "strip.png", "log": "console.log"},
        "exit": exit_code,
    }


class WebTestCase(unittest.TestCase):
    """One temp database, one seeded jobs directory, one server on port 0."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="sketchgen-web-")
        root = Path(cls._tmp.name)
        cls.db_path = root / "sketchgen.db"
        cls.jobs_dir = root / "jobs"
        cls.jobs_dir.mkdir()
        db.init(cls.db_path)

        conn = db.connect(cls.db_path)
        try:
            cls.queued_id = db.enqueue(
                conn,
                "a slow field of dots that drift",
                "student-one",
                rules_file="treatment",
                max_attempts=3,
            )

            cls.held_id = db.enqueue(
                conn, "three circles breathing", "student-two", rules_file="control"
            )
            db.transition(conn, cls.held_id, "executing", executor="qwen3-coder:30b")
            db.transition(conn, cls.held_id, "gating")
            attempt_dir = cls.jobs_dir / str(cls.held_id) / "attempt-1"
            gate_dir = attempt_dir / ".gate"
            gate_dir.mkdir(parents=True)
            (gate_dir / "report.json").write_text(
                json.dumps(report_json(attempt_dir), indent=2), encoding="utf-8"
            )
            (gate_dir / "strip.png").write_bytes(PNG_BYTES)
            (gate_dir / "gate.png").write_bytes(PNG_BYTES)
            # what the executor writes beside .gate/, and what the preview runs
            (attempt_dir / "index.html").write_text(SKETCH_INDEX, encoding="utf-8")
            (attempt_dir / "sketch.js").write_text(SKETCH_JS, encoding="utf-8")
            (attempt_dir / "prompt.txt").write_text("the brief\n", encoding="utf-8")
            (cls.jobs_dir / str(cls.held_id) / "job.log").write_text(
                "".join(
                    f"2026-09-14T12:00:{n:02d}Z job {cls.held_id}: line {n}\n"
                    for n in range(40)
                ),
                encoding="utf-8",
            )
            db.add_attempt(
                conn,
                cls.held_id,
                1,
                started_utc="2026-09-14T12:00:00Z",
                finished_utc="2026-09-14T12:06:52Z",
                model="qwen3-coder:30b-a3b-q4_K_M",
                rules_file="control",
                prompt_version="executor-v1",
                prompt_tokens=812,
                completion_tokens=1204,
                wall_s=412.0,
                source_dir=str(attempt_dir),
                gate_exit=0,
                gate_report_path=str(gate_dir / "report.json"),
            )
            db.transition(conn, cls.held_id, "held")
            cls.entry_id = db.create_entry(
                conn,
                cls.held_id,
                "held",
                prompt="three circles breathing",
                executor="qwen3-coder:30b-a3b-q4_K_M",
                rules_file="control",
                attempts=1,
                strip_path=str(gate_dir / "strip.png"),
                png_path=str(gate_dir / "gate.png"),
                submitted_by="student-two",
            )

            cls.failed_id = db.enqueue(conn, "a sketch that never worked", "student-one")
            db.transition(
                conn, cls.failed_id, "failed", last_error="gate exit 1: checks failed"
            )
        finally:
            conn.close()

        cls.server = web.make_server(
            bind="127.0.0.1",
            port=0,
            db_path=str(cls.db_path),
            jobs_dir=str(cls.jobs_dir),
            once_for_test=True,
        )
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}
        )
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        cls._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, raw_path=False):
        """(status, content type, body bytes). ``raw_path`` skips urllib's parsing."""
        if raw_path:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                body = response.read()
                return response.status, response.headers.get("Content-Type", ""), body
            finally:
                conn.close()
        try:
            with urllib.request.urlopen(self.url(path), timeout=10) as response:
                return (
                    response.status,
                    response.headers.get("Content-Type", ""),
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers.get("Content-Type", ""), exc.read()

    def post(self, path, fields):
        """(status, Location). 303 is an outcome, not something to follow."""
        data = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        opener = urllib.request.build_opener(NoRedirect)
        request = urllib.request.Request(self.url(path), data=data, method="POST")
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, response.headers.get("Location", "")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers.get("Location", "")

    def db(self):
        return db.connect(self.db_path)

    def text(self, path):
        status, content_type, body = self.get(path)
        self.assertEqual(status, 200, path)
        self.assertIn("text/html", content_type, path)
        return body.decode("utf-8")


class TestPages(WebTestCase):
    def test_every_page_renders(self):
        for path in ("/", "/queue", "/new", "/held", f"/job/{self.held_id}"):
            with self.subTest(path=path):
                page = self.text(path)
                self.assertIn("<title>", page)
                # the control is on every screen, per the wireframe
                self.assertIn('id="worker-pill"', page)
                self.assertIn('value="stop"', page)
                self.assertIn("Gallery", page)

    def test_the_held_page_offers_kept_rejections_for_publishing(self):
        # A gate rejection the worker kept waits here too (spec §9): it can
        # be published onto the rejections page, but there is nothing to
        # reject. One already published has left the page.
        conn = self.db()
        try:
            job = db.enqueue(conn, "a kept rejection", "student-three")
            conn.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (job,))
            kept = db.create_entry(conn, job, "failed-kept", prompt="a kept rejection")
            done_job = db.enqueue(conn, "a published rejection", "student-three")
            conn.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (done_job,))
            done = db.create_entry(
                conn, done_job, "failed-kept", prompt="a published rejection",
                published_utc="2026-09-14T13:00:00Z",
            )
            conn.commit()
        finally:
            conn.close()
        try:
            page = self.text("/held")
            self.assertIn(f'action="/held/{kept}/publish"', page)
            self.assertNotIn(f'action="/held/{kept}/reject"', page)
            self.assertNotIn(f'action="/held/{done}/publish"', page)
            self.assertIn(f'action="/held/{self.entry_id}/reject"', page)
        finally:
            conn = self.db()
            conn.execute("DELETE FROM entries WHERE id IN (?, ?)", (kept, done))
            conn.execute("DELETE FROM jobs WHERE id IN (?, ?)", (job, done_job))
            conn.commit()
            conn.close()

    def test_console_json_is_json_and_says_its_source(self):
        status, content_type, body = self.get("/api/console.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        document = json.loads(body)
        self.assertIn(document["source"], ("live", "sample"))
        # the collector is importable on this branch and both machines that run
        # this suite read /proc, so the served document should be the real one
        if web.console is not None and sys.platform.startswith("linux"):
            self.assertEqual(document["source"], "live")
        else:
            self.assertEqual(document["source"], "sample")
        # either way it is the contract's document
        for key in ("node", "model", "worker", "odometer", "funnel", "per_sketch"):
            self.assertIn(key, document)

    def test_the_sample_is_the_contracts_document(self):
        """The fallback fixture is the collector's own, keys and all."""
        document = json.loads(SAMPLE_CONSOLE.read_text(encoding="utf-8"))
        self.assertEqual(len(document["node"]["cpu_pct"]), 16)
        self.assertEqual(len(document["node"]["load"]), 3)
        page = web.console_page(document)
        # the wireframe's sixteen per-core bars, one per core in the document
        self.assertEqual(page.count("data-core="), 16)
        self.assertEqual(page.count('data-bar="node.cpu_pct.'), 16)
        # and the blocks that are not per-core
        for path in (
            'data-bar-num="node.mem_mb.used"',
            'data-bar-num="node.disk_gb.used"',
            'data-bar-num="node.load.0"',
            'data-k="funnel.passed_gate.total"',
            'data-k="per_sketch.all.cost_usd_4_24"',
            'data-k="model.instant.decode_tok_s"',
            'data-k="worker.job_in_flight"',
            'data-k="odometer.session.slot_ours_pct"',
        ):
            self.assertIn(path, page)

    def test_console_renders_one_bar_per_core(self):
        """One bar per core in whatever document the server actually served."""
        status, _, body = self.get("/api/console.json")
        self.assertEqual(status, 200)
        cores = len(json.loads(body)["node"]["cpu_pct"])
        self.assertGreater(cores, 0)
        page = self.text("/")
        self.assertEqual(page.count("data-core="), cores)
        self.assertEqual(page.count('data-bar="node.cpu_pct.'), cores)

    def test_every_number_says_which_kind_it_is(self):
        # "Entry 48 — job 49" was read as one thing with two ids, twice, on
        # 2026-09-14. The card carries the entry number alone as its heading,
        # the job is named as a job on the meta line, every button still says
        # which entry it acts on — in its aria-label since packet 6, because
        # the id in four button labels was the same number said four more
        # times — the queue has an entry column, and the job page names its
        # entry.
        held = self.text("/held")
        self.assertIn(f'<span class="n">{self.entry_id}</span>', held)
        self.assertIn(f'job <a href="/job/{self.held_id}">{self.held_id}</a>', held)
        self.assertIn(f'aria-label="Publish entry {self.entry_id}"', held)
        self.assertIn(f'aria-label="Reject entry {self.entry_id}"', held)
        self.assertIn(f'aria-label="Spawn a child of entry {self.entry_id}"', held)
        self.assertNotIn("— job", held)
        queue = self.text("/queue")
        self.assertIn('<th class="n">job</th><th class="n">entry</th>', queue)
        self.assertIn(f'href="/held#entry-{self.entry_id}"', queue)
        job = self.text(f"/job/{self.held_id}")
        self.assertIn(f"entry {self.entry_id} (held)", job)
        no_entry = self.text(f"/job/{self.queued_id}")
        self.assertIn("(no entry yet)", no_entry)

    def test_queue_lists_the_jobs_newest_first(self):
        page = self.text("/queue")
        self.assertLess(
            page.index(f'href="/job/{self.failed_id}"'),
            page.index(f'href="/job/{self.queued_id}"'),
        )
        self.assertIn("three circles breathing", page)

    def test_job_page_shows_the_log_the_checks_and_the_artefacts(self):
        page = self.text(f"/job/{self.held_id}")
        self.assertIn(f'<pre id="log" data-job="{self.held_id}"', page)
        self.assertIn("line 39", page)  # the tail, not the head
        # Packet 4.3: the static tail is still the page's own content, and the
        # stream is what the script asks for on top of it.
        self.assertIn("/events/job/", page)
        self.assertIn("new EventSource", page)
        self.assertIn("console_clean", page)
        self.assertIn("responds(click)", page)
        self.assertIn(f"/jobs/{self.held_id}/attempt-1/.gate/strip.png", page)

    def test_unknown_path_is_404_and_wrong_method_is_405(self):
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get(f"/job/{self.held_id}/cancel")[0], 405)
        self.assertEqual(self.get("/control")[0], 405)


class TestHeaderMarks(WebTestCase):
    """The nav's live summaries: a meter on Console, counts on the rest.

    The seeded database is one queued job, one held job with an entry, one
    failed job and no ``worker_started_utc`` — so the header should read
    1 queued, 0 in flight, 0 failed this session, 1 held, 0 public.
    """

    METER = re.compile(r'data-nav="cpu" title="([^"]*)"[^>]*>(.*?)</span>', re.S)

    def marks(self, page):
        """(number, title) for each nav mark that carries a number."""
        found = {}
        for key in ("queued", "in-flight", "failed", "held", "public"):
            match = re.search(r'data-nav="%s" title="([^"]*)">(\d+)<' % key, page)
            self.assertIsNotNone(match, f"no {key} mark in the header")
            found[key] = (int(match.group(2)), match.group(1))
        return found

    def meter(self, page):
        """(title, lit segments, total segments) for the Console meter."""
        match = self.METER.search(page)
        self.assertIsNotNone(match, "no Console meter in the header")
        title, segments = match.group(1), match.group(2)
        return title, segments.count('<i class="on">'), segments.count("<i")

    def test_every_page_carries_the_five_marks(self):
        for path in ("/", "/queue", "/new", "/held", f"/job/{self.held_id}"):
            with self.subTest(path=path):
                page = self.text(path)
                title, lit, total = self.meter(page)
                self.assertEqual(total, 5)
                self.assertLessEqual(lit, total)
                self.assertIn("node CPU", title)
                self.assertIn("slot ", title)
                marks = self.marks(page)
                self.assertEqual(marks["queued"][0], 1)
                self.assertEqual(marks["in-flight"][0], 0)
                self.assertEqual(marks["failed"][0], 0)
                self.assertEqual(marks["held"][0], 1)
                self.assertEqual(marks["public"][0], 0)
                # every number says what it is, on hover
                self.assertEqual(marks["queued"][1], "jobs queued")
                self.assertEqual(marks["in-flight"][1], "jobs in flight")
                self.assertEqual(
                    marks["failed"][1], "jobs failed since the worker started"
                )
                self.assertEqual(
                    marks["held"][1], "1 waiting · 0 kept rejections below them"
                )
                self.assertEqual(marks["public"][1], "0 public · 0 published, 0 kept")
                # the marks sit inside the anchors: the whole thing is the link
                self.assertRegex(
                    page, r'<a href="/queue"[^>]*>Queue <span class="counts"'
                )
                self.assertRegex(
                    page, r'<a href="/held"[^>]*>Held<sup class="sup warn"'
                )

    def test_the_marks_move_with_the_database(self):
        # A job the worker has claimed is in flight; publishing the seeded
        # entry moves it off Held and onto Gallery.
        conn = self.db()
        try:
            moving = db.enqueue(conn, "in flight now", "student-one")
            conn.execute("UPDATE jobs SET state = 'executing' WHERE id = ?", (moving,))
            conn.execute(
                "UPDATE entries SET state = 'published', published_utc = ? "
                "WHERE id = ?",
                ("2026-09-14T13:00:00Z", self.entry_id),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            marks = self.marks(self.text("/queue"))
            self.assertEqual(marks["in-flight"][0], 1)
            self.assertEqual(marks["held"][0], 0)
            self.assertEqual(marks["public"][0], 1)
            self.assertEqual(marks["public"][1], "1 public · 1 published, 0 kept")
        finally:
            conn = self.db()
            try:
                conn.execute("DELETE FROM jobs WHERE id = ?", (moving,))
                conn.execute(
                    "UPDATE entries SET state = 'held', published_utc = NULL "
                    "WHERE id = ?",
                    (self.entry_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def test_the_red_number_is_failures_since_the_worker_started(self):
        # The all-time count only ever grows, so this one is the session's:
        # state failed, updated at or after meta.worker_started_utc.
        conn = self.db()
        try:
            db.set_meta(conn, "worker_started_utc", "2026-09-14T12:00:00Z")
            # the seeded failure happened before this worker came up
            conn.execute(
                "UPDATE jobs SET updated_utc = ? WHERE id = ?",
                ("2026-09-14T11:00:00Z", self.failed_id),
            )
            conn.commit()
        finally:
            conn.close()
        after = None
        try:
            self.assertEqual(self.marks(self.text("/"))["failed"][0], 0)
            conn = self.db()
            try:
                after = db.enqueue(conn, "failed after the restart", "student-one")
                conn.execute(
                    "UPDATE jobs SET state = 'failed', updated_utc = ? WHERE id = ?",
                    ("2026-09-14T12:30:00Z", after),
                )
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(self.marks(self.text("/"))["failed"][0], 1)
        finally:
            conn = self.db()
            try:
                if after is not None:
                    conn.execute("DELETE FROM jobs WHERE id = ?", (after,))
                conn.execute("DELETE FROM meta WHERE key = 'worker_started_utc'")
                conn.execute(
                    "UPDATE jobs SET updated_utc = ? WHERE id = ?",
                    ("2026-09-14T12:00:00Z", self.failed_id),
                )
                conn.commit()
            finally:
                conn.close()

    def test_control_json_carries_the_same_numbers(self):
        status, content_type, body = self.get("/api/control.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        nav = json.loads(body)["nav"]
        self.assertEqual(nav["queued"], 1)
        self.assertEqual(nav["in_flight"], 0)
        self.assertEqual(nav["failed_session"], 0)
        self.assertEqual(nav["held"], 1)
        self.assertEqual(nav["kept"], 0)
        self.assertEqual(nav["public"], 0)
        self.assertEqual(nav["published"], 0)
        self.assertIn(nav["slot_state"], ("ours", "busy", "free"))
        self.assertGreaterEqual(nav["cpu_pct"], 0.0)
        self.assertLessEqual(nav["cpu_pct"], 100.0)

    def test_no_proc_is_an_unlit_meter_and_not_an_error(self):
        # A machine without /proc — a Mac, a container — renders a dark meter
        # rather than a traceback.
        self.assertEqual(web.node_cpu_pct(Path(self._tmp.name) / "no-loadavg"), 0.0)
        original = web.node_cpu_pct
        web.node_cpu_pct = lambda *args, **kwargs: 0.0
        try:
            title, lit, total = self.meter(self.text("/"))
            self.assertEqual((lit, total), (0, 5))
            self.assertIn("node CPU 0%", title)
        finally:
            web.node_cpu_pct = original


class TestTokensGauge(WebTestCase):
    """The Console link's sparkline: two hours of completed tokens.

    The meter beside it is the node now; this is the session behind it. The
    seeded database has one attempt, with tokens but no rates — so the line
    has bins to draw and the decode number is a dash until an attempt with
    ``prefill_s`` and ``decode_s`` turns up.
    """

    SPARK = re.compile(
        r'<svg class="spark" data-nav="tokens" width="(\d+)" height="(\d+)"'
        r'[^>]*aria-label="([^"]*)"[^>]*>(.*?)</svg>',
        re.S,
    )

    def sparks(self, page):
        """(width, height, label, points) for every gauge on the page."""
        found = []
        for match in self.SPARK.finditer(page):
            line = re.search(r'<path class="spark-line" d="M([^"]*)"', match.group(4))
            self.assertIsNotNone(line, "a gauge with no line")
            found.append(
                (
                    int(match.group(1)),
                    int(match.group(2)),
                    match.group(3),
                    line.group(1).split(" L"),
                )
            )
        self.assertTrue(found, "no tokens gauge in the page")
        return found

    def decode(self, page):
        match = re.search(r'data-nav="decode">([^<]*)</b>', page)
        self.assertIsNotNone(match, "no decode number in the header")
        return match.group(1)

    def test_token_bins_are_five_minutes_each_oldest_first(self):
        now = datetime(2027, 3, 2, 9, 0, tzinfo=timezone.utc)

        def stamp(minutes_ago):
            return (now - timedelta(minutes=minutes_ago)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        conn = self.db()
        job = None
        try:
            job = db.enqueue(conn, "tokens for the gauge", "student-one")
            # two inside the window, one older than it, one still running
            db.add_attempt(
                conn, job, 1, finished_utc=stamp(3),
                prompt_tokens=800, completion_tokens=1200,
            )
            db.add_attempt(
                conn, job, 2, finished_utc=stamp(3),
                prompt_tokens=16, completion_tokens=0,
            )
            db.add_attempt(
                conn, job, 3, finished_utc=stamp(7),
                prompt_tokens=100, completion_tokens=20,
            )
            db.add_attempt(
                conn, job, 4, finished_utc=stamp(130),
                prompt_tokens=9999, completion_tokens=9999,
            )
            db.add_attempt(
                conn, job, 5, started_utc=stamp(1),
                prompt_tokens=7, completion_tokens=7,
            )
            conn.commit()
            bins = web.token_bins(conn, now=now)
            self.assertEqual(len(bins), 24)
            # the partial bin the clock is standing in is the last one, and
            # two attempts that finished in the same five minutes are one bar
            self.assertEqual(bins[-1], 800 + 1200 + 16)
            self.assertEqual(bins[-2], 120)
            # everything else in the window is idle, and so is zero: the 130
            # minute attempt is off the left edge, the running one is nowhere
            self.assertEqual(sum(bins), 800 + 1200 + 16 + 120)
            self.assertEqual(bins[:22], [0] * 22)
            # a window with nothing in it is a flat line on the baseline, not
            # a division by zero
            flat = web._spark_points([0] * 24, 72, 14)
            self.assertEqual(len(flat), 24)
            self.assertEqual({point.split(",")[1] for point in flat}, {"13.0"})
        finally:
            if job is not None:
                conn.execute("DELETE FROM attempts WHERE job_id = ?", (job,))
                conn.execute("DELETE FROM jobs WHERE id = ?", (job,))
                conn.commit()
            conn.close()

    def test_every_page_carries_the_gauge_and_the_rate(self):
        for path in ("/queue", "/new", "/held", f"/job/{self.held_id}"):
            with self.subTest(path=path):
                page = self.text(path)
                gauges = self.sparks(page)
                self.assertEqual(len(gauges), 1)
                width, height, label, points = gauges[0]
                self.assertEqual((width, height), (72, 14))
                # one point per bin, and the window says what it is
                self.assertEqual(len(points), 24)
                self.assertIn("tokens completed, last 2 h in 5-min bins", label)
                self.assertIn("peak ", label)
                # the seeded attempt has tokens but no seconds, so there is no
                # rate to divide out and the number is a dash
                self.assertEqual(self.decode(page), "\u2014")

    def test_an_attempt_with_rates_gives_the_header_its_number(self):
        conn = self.db()
        attempt = None
        try:
            attempt = db.add_attempt(
                conn,
                self.held_id,
                99,
                started_utc="2026-09-14T14:00:00Z",
                finished_utc="2026-09-14T14:05:00Z",
                prompt_tokens=812,
                completion_tokens=2530,
                prefill_s=2.0,
                decode_s=100.0,
            )
            conn.commit()
        finally:
            conn.close()
        try:
            page = self.text("/queue")
            self.assertEqual(self.decode(page), "25.3")
            self.assertIn("decode 25.3 tok/s", self.sparks(page)[0][2])
        finally:
            conn = self.db()
            try:
                conn.execute("DELETE FROM attempts WHERE id = ?", (attempt,))
                conn.commit()
            finally:
                conn.close()

    def test_control_json_carries_the_bins(self):
        status, content_type, body = self.get("/api/control.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        tokens = json.loads(body)["nav"]["tokens"]
        self.assertEqual(len(tokens["bins"]), 24)
        self.assertTrue(all(isinstance(n, int) for n in tokens["bins"]))
        self.assertEqual(tokens["bin_minutes"], 5)
        # no attempt has rates in the seeded database, and the odometer's
        # session is the whole table while the worker has never stamped it
        self.assertIsNone(tokens["decode_tok_s"])
        self.assertIsNone(tokens["prefill_tok_s"])
        self.assertEqual(tokens["session_in"], 812)
        self.assertEqual(tokens["session_out"], 1204)

    def test_the_console_page_carries_the_wider_gauge_too(self):
        page = self.text("/")
        gauges = self.sparks(page)
        # the header's, and the model panel's — one hook, so the poll repaints
        # both of them from the same bins
        self.assertEqual(len(gauges), 2)
        self.assertEqual([(g[0], g[1]) for g in gauges], [(72, 14), (240, 40)])
        for gauge in gauges:
            self.assertEqual(len(gauge[3]), 24)
        self.assertIn("last 2 h, 5-min bins", page)
        self.assertIn('data-nav="peak"', page)


class TestForms(WebTestCase):
    def test_post_new_queues_a_job_and_redirects(self):
        before = self._queued_ids()
        status, location = self.post(
            "/new",
            {
                "prompt": "a grid that ripples when you click it",
                "submitted_by": "student-three",
                "planner": "local",
                "rules": "random",
                "publication": "hold",
                "max_attempts": "2",
                "assert": ["responds(click)", "size"],
                "size_w": "600",
                "size_h": "400",
            },
        )
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/queue"), location)
        self.assertIn("Queued%20as", location)
        after = self._queued_ids()
        new_ids = after - before
        self.assertEqual(len(new_ids), 1)
        conn = self.db()
        try:
            job = db.get_job(conn, new_ids.pop())
        finally:
            conn.close()
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.submitted_by, "student-three")
        self.assertEqual(job.rules_file, "random")
        self.assertEqual(job.max_attempts, 2)
        self.assertEqual(sorted(job.assertions), ["motion(idle)", "responds(click)",
                                                  "size(600,400)"])

    def test_post_new_refuses_a_bad_username_without_writing(self):
        before = self._queued_ids()
        status, _ = self.post(
            "/new", {"prompt": "anything", "submitted_by": "someone@example.com"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(self._queued_ids(), before)

    def _queued_ids(self):
        conn = self.db()
        try:
            return {job.id for job in db.list_jobs(conn, "queued")}
        finally:
            conn.close()


class TestControl(WebTestCase):
    def tearDown(self):
        conn = self.db()
        try:
            db.set_control(conn, "running", None)
        finally:
            conn.close()

    def test_pause_flips_the_row_and_the_header_says_so(self):
        status, location = self.post("/control", {"action": "pause", "back": "/queue"})
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/queue"), location)
        conn = self.db()
        try:
            control = db.get_control(conn)
        finally:
            conn.close()
        self.assertEqual(control.state, "pausing")
        self.assertEqual(control.reason, "pause")
        for path in ("/", "/queue", "/new", "/held"):
            with self.subTest(path=path):
                self.assertIn("PAUSING", self.text(path))

    def test_paused_shows_paused_on_every_page(self):
        conn = self.db()
        try:
            db.set_control(conn, "paused", "the operator stopped it")
        finally:
            conn.close()
        for path in ("/", "/queue", f"/job/{self.held_id}"):
            with self.subTest(path=path):
                self.assertIn("PAUSED", self.text(path))

    def test_stop_is_the_workers_own_stop_now(self):
        self.post("/control", {"action": "stop", "back": "/"})
        conn = self.db()
        try:
            control = db.get_control(conn)
        finally:
            conn.close()
        # worker.is_stop_now() is the only reader that matters here
        from sketchgen import worker

        self.assertTrue(worker.is_stop_now(control))
        self.assertIn("STOPPING", self.text("/"))

    def test_control_json_tracks_the_row(self):
        self.post("/control", {"action": "pause", "back": "/"})
        status, content_type, body = self.get("/api/control.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        document = json.loads(body)
        self.assertEqual(document["state"], "pausing")
        self.assertEqual(document["pill"], "PAUSING")
        self.assertFalse(document["stop_now"])


class TestJobActions(WebTestCase):
    def test_cancel_moves_a_queued_job_to_failed(self):
        conn = self.db()
        try:
            job_id = db.enqueue(conn, "cancel me", "student-one")
        finally:
            conn.close()
        status, location = self.post(f"/job/{job_id}/cancel", {})
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith(f"/job/{job_id}"), location)
        conn = self.db()
        try:
            job = db.get_job(conn, job_id)
        finally:
            conn.close()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.last_error, "cancelled by operator")

    def test_cancel_on_a_terminal_job_changes_nothing(self):
        status, _ = self.post(f"/job/{self.failed_id}/cancel", {})
        self.assertEqual(status, 303)
        conn = self.db()
        try:
            job = db.get_job(conn, self.failed_id)
        finally:
            conn.close()
        self.assertEqual(job.last_error, "gate exit 1: checks failed")

    def test_send_to_laptop_sets_needs_review(self):
        conn = self.db()
        try:
            job_id = db.enqueue(conn, "needs the paid model", "student-one")
        finally:
            conn.close()
        self.post(f"/job/{job_id}/laptop", {})
        conn = self.db()
        try:
            job = db.get_job(conn, job_id)
        finally:
            conn.close()
        self.assertEqual(job.state, "needs-laptop")
        self.assertEqual(job.needs, "review")


class TestHeld(WebTestCase):
    def test_publish_without_the_publisher_changes_nothing(self):
        try:
            from sketchgen import publish  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("packet 3.2 is installed in this checkout")
        conn = self.db()
        try:
            entry_id = db.create_entry(
                conn, self.queued_id, "held", prompt="not really published"
            )
        finally:
            conn.close()
        status, location = self.post(f"/held/{entry_id}/publish", {})
        self.assertEqual(status, 303)
        self.assertIn("publisher%20not%20installed", location)
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT state FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["state"], "held")

    def test_reject_moves_the_entry_and_its_job(self):
        status, location = self.post(
            f"/held/{self.entry_id}/reject", {"reason": "off brief"}
        )
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/held"), location)
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT state FROM entries WHERE id = ?", (self.entry_id,)
            ).fetchone()
            job = db.get_job(conn, self.held_id)
        finally:
            conn.close()
        self.assertEqual(row["state"], "rejected")
        self.assertEqual(job.state, "rejected")
        self.assertEqual(job.last_error, "off brief")


class TestArchiveAndReject(WebTestCase):
    """Packet 2 of the lineage ledger: §5.2's reject-and-publish, §5.3's archive."""

    def make_entry(self, state, prompt="one more", **fields):
        """A job and an entry in ``state``, removed again after the test."""
        conn = self.db()
        try:
            job_id = db.enqueue(conn, prompt, "student-three")
            job_state = {"held": "held", "published": "published"}.get(state, "failed")
            if job_state == "failed":
                conn.execute(
                    "UPDATE jobs SET state = 'failed' WHERE id = ?", (job_id,)
                )
            else:
                db.transition(conn, job_id, "executing")
                db.transition(conn, job_id, "gating")
                db.transition(conn, job_id, "held")
                if job_state == "published":
                    db.transition(conn, job_id, "published")
            entry_id = db.create_entry(conn, job_id, state, prompt=prompt, **fields)
            conn.commit()
        finally:
            conn.close()

        def clean():
            conn = self.db()
            conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            conn.close()

        self.addCleanup(clean)
        return job_id, entry_id

    def row(self, entry_id):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT * FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        finally:
            conn.close()

    # -- reject ------------------------------------------------------------

    def test_reject_stores_the_reason_and_takes_the_publish_path(self):
        _, entry_id = self.make_entry("held", prompt="one to refuse")
        app = web.App(db_path=str(self.db_path), jobs_dir=str(self.jobs_dir))
        seen = []
        original = web.publish_entry
        web.publish_entry = lambda a, c, i: seen.append(i) or "handed to the publisher"
        try:
            conn = self.db()
            try:
                message = web.reject_entry(app, conn, entry_id, " off brief ")
                conn.commit()
            finally:
                conn.close()
        finally:
            web.publish_entry = original
        self.assertEqual([entry_id], seen)
        self.assertIn("off brief", message)
        self.assertIn("publisher", message)
        row = self.row(entry_id)
        self.assertEqual("rejected", row["state"])
        self.assertEqual("off brief", row["reject_reason"])

    def test_a_rejection_whose_push_fails_stays_pending(self):
        # There is no gallery checkout here, so the publisher refuses. The
        # entry is rejected with a null published_utc, which is exactly how a
        # kept failure waits (§5.2), and the flash says what happened.
        _, entry_id = self.make_entry("held", prompt="one whose push fails")
        status, location = self.post(
            f"/held/{entry_id}/reject", {"reason": "drifted from the prompt"}
        )
        self.assertEqual(303, status)
        self.assertIn("drifted%20from%20the%20prompt", location)
        row = self.row(entry_id)
        self.assertEqual("rejected", row["state"])
        self.assertIsNone(row["published_utc"])

    # -- archive -----------------------------------------------------------

    def test_archive_takes_a_held_entry_off_the_held_page(self):
        _, entry_id = self.make_entry("held", prompt="one to shelve")
        self.assertIn(f'id="entry-{entry_id}"', self.text("/held"))
        status, location = self.post(f"/held/{entry_id}/archive", {"back": "/held"})
        self.assertEqual(303, status)
        self.assertTrue(location.startswith("/held?flash="), location)
        self.assertEqual("archived", self.row(entry_id)["state"])
        self.assertNotIn(f'id="entry-{entry_id}"', self.text("/held"))

    def test_archive_takes_an_unpublished_kept_failure_off_the_kept_list(self):
        _, entry_id = self.make_entry("failed-kept", prompt="one the gate refused")
        page = self.text("/held")
        self.assertIn(f'action="/held/{entry_id}/archive"', page)
        self.post(f"/held/{entry_id}/archive", {"back": "/held"})
        self.assertEqual("archived", self.row(entry_id)["state"])
        self.assertNotIn(f'id="entry-{entry_id}"', self.text("/held"))

    def test_archive_on_a_published_entry_is_refused_with_a_flash(self):
        _, entry_id = self.make_entry("published", prompt="one already on the site")
        status, location = self.post(f"/held/{entry_id}/archive", {"back": "/held"})
        # A refusal, not a 500: the operator reads it on the page.
        self.assertEqual(303, status)
        self.assertIn("refused", location)
        self.assertEqual("published", self.row(entry_id)["state"])

    def test_archive_of_an_entry_that_is_not_there_is_a_flash_too(self):
        status, location = self.post("/held/99999/archive", {"back": "/held"})
        self.assertEqual(303, status)
        self.assertIn("no%20entry%2099999", location)

    def test_an_archived_entry_is_still_readable_by_id(self):
        _, entry_id = self.make_entry("held", prompt="one to shelve and find again")
        self.post(f"/held/{entry_id}/archive", {"back": "/held"})
        page = self.text(f"/entry/{entry_id}")
        self.assertIn(f"Entry {entry_id}", page)
        self.assertIn("one to shelve and find again", page)
        self.assertIn("Nothing was deleted", page)
        # Read-only: no button on this page acts on the entry.
        self.assertNotIn(f'action="/held/{entry_id}/', page)
        self.assertNotIn(f'action="/entry/{entry_id}/spawn"', page)

    def test_the_console_summary_counts_archived_entries(self):
        _, entry_id = self.make_entry("held", prompt="one for the count")
        self.post(f"/held/{entry_id}/archive", {"back": "/held"})
        status, _, body = self.get("/api/console.json")
        self.assertEqual(200, status)
        funnel = json.loads(body)["funnel"]
        self.assertIn("archived", funnel)
        self.assertGreaterEqual(funnel["archived"]["total"], 1)


class TestDecisionCard(WebTestCase):
    """Packet 6: one number, one image, one input, four verbs.

    Entry 271's card said "271" six times and the word "entry" eight times,
    drew the gate's four-frame strip twice — the poster the run button sits on
    *is* that file — and offered two text boxes with a help string under each.
    One decision, said once. Each test here is one of those repetitions gone.
    """

    def card_entry(self, state="held", prompt="a cityscape at dusk", entry_id=9271):
        """An entry with a runnable attempt, renumbered to an id of its own.

        The seeded fixture's entry is 1 and its job is 2, and "1" is also the
        attempt number and half the numbers on the page, so counting how often
        a card says its own id needs an id that means nothing else there.
        """
        conn = self.db()
        try:
            job_id = db.enqueue(conn, prompt, "student-two", rules_file="treatment")
            if state == "held":
                db.transition(conn, job_id, "executing", executor="qwen3-coder:30b")
                db.transition(conn, job_id, "gating")
                db.transition(conn, job_id, "held")
            else:
                conn.execute(
                    "UPDATE jobs SET state = 'failed' WHERE id = ?", (job_id,)
                )
            attempt = self.jobs_dir / str(job_id) / "attempt-1"
            gate = attempt / ".gate"
            gate.mkdir(parents=True)
            (gate / "report.json").write_text(
                json.dumps(report_json(attempt)), encoding="utf-8"
            )
            (gate / "strip.png").write_bytes(PNG_BYTES)
            (attempt / "index.html").write_text(SKETCH_INDEX, encoding="utf-8")
            (attempt / "sketch.js").write_text(SKETCH_JS, encoding="utf-8")
            db.add_attempt(
                conn, job_id, 1,
                model="qwen3-coder:30b", rules_file="treatment",
                source_dir=str(attempt), gate_exit=0,
                gate_report_path=str(gate / "report.json"),
            )
            made = db.create_entry(
                conn, job_id, state, prompt=prompt,
                executor="qwen3-coder:30b", rules_file="treatment", attempts=1,
                source_dir=str(attempt), strip_path=str(gate / "strip.png"),
                submitted_by="student-two",
            )
            conn.execute("UPDATE entries SET id = ? WHERE id = ?", (entry_id, made))
            conn.commit()
        finally:
            conn.close()

        def clean():
            # Children first: a spawned child job points at this entry, and the
            # foreign keys are on.
            conn = self.db()
            try:
                conn.execute(
                    "DELETE FROM jobs WHERE parent_entry_id = ?", (entry_id,)
                )
                conn.execute(
                    "DELETE FROM lineage WHERE parent_entry_id = ? "
                    "OR child_entry_id = ?",
                    (entry_id, entry_id),
                )
                conn.execute("DELETE FROM attempts WHERE job_id = ?", (job_id,))
                conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
                conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
                conn.commit()
            finally:
                conn.close()
            shutil.rmtree(self.jobs_dir / str(job_id), ignore_errors=True)

        self.addCleanup(clean)
        return job_id, entry_id

    def card(self, page, entry_id):
        """Inside this entry's <section>, and nothing else on the page.

        From the end of the opening tag, so that stripping the tags out of
        what comes back leaves the words a person actually reads.
        """
        needle = f'id="entry-{entry_id}"'
        self.assertIn(needle, page)
        inside = page.split(needle, 1)[1].split(">", 1)[1]
        return inside.split("</section>", 1)[0]

    def row(self, entry_id):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT * FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        finally:
            conn.close()

    # -- what the card shows -----------------------------------------------

    def test_the_strip_is_drawn_once_as_the_run_buttons_poster(self):
        # The one that started the packet: _poster_url prefers strip.png, so
        # the poster and the picture underneath it were the same four frames.
        _, entry_id = self.card_entry()
        card = self.card(self.text("/held"), entry_id)
        self.assertEqual(1, card.count("data-preview"))
        self.assertEqual(1, card.count("<img"))
        self.assertIn("/.gate/strip.png", card)
        self.assertNotIn("<iframe", card)

    def test_an_entry_with_nothing_to_run_still_shows_its_strip(self):
        # _entry_image is the fallback now, not a second picture.
        job_id, entry_id = self.card_entry()
        (self.jobs_dir / str(job_id) / "attempt-1" / "index.html").unlink()
        card = self.card(self.text("/held"), entry_id)
        self.assertNotIn("data-preview", card)
        self.assertEqual(1, card.count("<img"))
        self.assertIn("strip.png", card)

    def test_the_entry_id_is_said_once_and_otherwise_only_to_a_screen_reader(self):
        _, entry_id = self.card_entry()
        card = self.card(self.text("/held"), entry_id)
        visible = re.sub(r"<[^>]+>", " ", card)
        self.assertEqual(1, len(re.findall(rf"\b{entry_id}\b", visible)), visible)
        # and the word itself is gone from the card's face: it is a card about
        # an entry, on a page of them, which nothing has to say out loud
        self.assertNotIn("entry", visible.lower())
        # the buttons still name it where it matters
        for label in ("Publish", "Reject", "Archive"):
            self.assertIn(f'aria-label="{label} entry {entry_id}"', card)
        self.assertIn(f'aria-label="Spawn a child of entry {entry_id}"', card)

    def test_the_prompt_is_the_root_sentence_and_the_newest_revision(self):
        from sketchgen import lineage

        prompt = lineage.compose_prompt(
            lineage.compose_prompt("a cityscape at dusk", "add a moon"),
            "try a colder palette after dusk",
        )
        _, entry_id = self.card_entry(prompt=prompt)
        card = self.card(self.text("/held"), entry_id)
        self.assertIn('<p class="prompt">a cityscape at dusk</p>', card)
        self.assertIn(
            '<span class="k">revise:</span> try a colder palette after dusk', card
        )
        # the earlier revisions are the ledger's job, not this card's
        self.assertNotIn("add a moon", card)

    def test_the_meta_line_says_the_rest_of_it_once(self):
        _, entry_id = self.card_entry()
        card = self.card(self.text("/held"), entry_id)
        meta = card.split('<p class="meta">', 1)[1].split("</p>", 1)[0]
        self.assertIn("gate passed on attempt 1", meta)
        self.assertIn("treatment", meta)
        self.assertIn("qwen3-coder:30b", meta)
        self.assertIn("a root prompt, no lineage", meta)
        self.assertIn("open in a tab", meta)
        # and the poster does not repeat the link it carries
        self.assertEqual(1, card.count("open in a tab"))
        # what the run costs stays beside the button that starts it
        self.assertIn("chip cost", card)

    def test_the_generation_is_said_in_the_header_and_not_again_below(self):
        parent = self.card_entry(entry_id=9270)[1]
        _, entry_id = self.card_entry(entry_id=9271)
        conn = self.db()
        try:
            db.add_lineage(
                conn, entry_id, parent, generation=4,
                critique_by="gemma4:e4b", critique="a colder palette",
            )
            conn.commit()
        finally:
            conn.close()
        self.addCleanup(self._drop_lineage, entry_id)
        card = self.card(self.text("/held"), entry_id)
        self.assertEqual(1, card.count("generation 4"))
        self.assertIn(f"from entry {parent}, critiqued by gemma4:e4b", card)

    def _drop_lineage(self, entry_id):
        conn = self.db()
        conn.execute("DELETE FROM lineage WHERE child_entry_id = ?", (entry_id,))
        conn.commit()
        conn.close()

    # -- the four verbs ----------------------------------------------------

    def test_one_form_one_box_four_verbs(self):
        _, entry_id = self.card_entry()
        card = self.card(self.text("/held"), entry_id)
        self.assertEqual(1, card.count("<form"))
        self.assertEqual(1, card.count('type="text"'))
        self.assertEqual(1, card.count('name="text"'))
        self.assertIn(f'action="/held/{entry_id}/publish"', card)
        for css, action in (
            ("rej", f"/held/{entry_id}/reject"),
            ("cri", f"/entry/{entry_id}/spawn"),
            ("arc", f"/held/{entry_id}/archive"),
        ):
            self.assertIn(f'class="{css}" formaction="{action}"', card)
        # critique_by left the page: a sentence typed here is the operator's
        self.assertNotIn("critique_by", card)
        # one help string, under all four
        self.assertEqual(1, card.count('class="hint"'))

    def test_a_kept_failure_card_has_no_reject(self):
        _, entry_id = self.card_entry(
            state="failed-kept", prompt="one the gate refused"
        )
        card = self.card(self.text("/held"), entry_id)
        self.assertNotIn("/reject", card)
        self.assertNotIn('class="rej"', card)
        self.assertIn(f'action="/held/{entry_id}/publish"', card)
        self.assertIn(f'formaction="/entry/{entry_id}/spawn"', card)
        self.assertIn(f'formaction="/held/{entry_id}/archive"', card)
        # and the hint says where publishing puts it
        self.assertIn("rejections page", card)

    def test_reject_reads_the_box_and_still_reads_the_old_field(self):
        _, new = self.card_entry(entry_id=9281)
        self.post(f"/held/{new}/reject", {"text": "off brief", "back": "/held"})
        self.assertEqual("off brief", self.row(new)["reject_reason"])
        # Anything posting the old name — the tests above, a bookmark, a
        # script — still works, which is why the box could be renamed at all.
        _, old = self.card_entry(entry_id=9282)
        self.post(f"/held/{old}/reject", {"reason": "the old field", "back": "/held"})
        self.assertEqual("the old field", self.row(old)["reject_reason"])

    def test_a_critique_typed_on_the_card_is_the_operators(self):
        _, entry_id = self.card_entry()
        expected = web.operator_username(self.row(entry_id))
        status, location = self.post(
            f"/entry/{entry_id}/spawn",
            {"text": "try a colder palette after dusk", "back": "/held"},
        )
        self.assertEqual(303, status)
        self.assertIn("Queued", location)
        conn = self.db()
        try:
            child = conn.execute(
                "SELECT * FROM jobs WHERE parent_entry_id = ?", (entry_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(child)
        self.assertEqual("try a colder palette after dusk", child["critique"])
        # no critique_by was posted, and the card has no field for one
        self.assertEqual(expected, child["critique_by"])

    # -- the same card, read-only ------------------------------------------

    def test_the_entry_page_is_the_same_card_with_no_form(self):
        _, entry_id = self.card_entry()
        card = self.card(self.text(f"/entry/{entry_id}"), entry_id)
        self.assertEqual(1, card.count("data-preview"))
        self.assertEqual(1, card.count("<img"))
        self.assertIn('<p class="prompt">a cityscape at dusk</p>', card)
        self.assertIn('<p class="meta">', card)
        self.assertNotIn("<form", card)
        self.assertIn("Waiting for a decision", card)

    def test_an_archived_entry_still_renders_at_entry_id(self):
        _, entry_id = self.card_entry()
        self.post(f"/held/{entry_id}/archive", {"back": "/held"})
        card = self.card(self.text(f"/entry/{entry_id}"), entry_id)
        self.assertIn('<span class="pill archived">archived</span>', card)
        self.assertIn("Nothing was deleted", card)
        self.assertNotIn("<form", card)


class TestSpawn(WebTestCase):
    """POST /entry/<id>/spawn — packet 5.3's one write to the operator UI."""

    def published_entry(self, prompt, parent=None, generation=None):
        """A published entry, optionally already N generations into a line."""
        conn = self.db()
        try:
            job_id = db.enqueue(conn, prompt, "student-two", rules_file="control")
            db.transition(conn, job_id, "executing")
            db.transition(conn, job_id, "gating")
            db.transition(conn, job_id, "held")
            db.transition(conn, job_id, "published")
            entry_id = db.create_entry(
                conn, job_id, "published", prompt=prompt,
                parent_entry_id=parent, submitted_by="student-two",
                rules_file="control",
            )
            if generation is not None:
                db.add_lineage(
                    conn, entry_id, parent, generation=generation,
                    critique_by="gemma4:e4b", critique="an earlier critique",
                )
            return entry_id
        finally:
            conn.close()

    def test_spawn_from_a_published_entry_queues_a_child_with_the_parent(self):
        entry_id = self.published_entry("a field of circles that drift")
        status, location = self.post(
            f"/entry/{entry_id}/spawn",
            {"critique": "let one circle fall out of phase",
             "critique_by": "gemma4:e4b", "back": "/held"},
        )
        self.assertEqual(303, status)
        self.assertTrue(location.startswith("/held?flash="), location)
        self.assertIn("Queued%20as", location)

        conn = self.db()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE parent_entry_id = ?", (entry_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual("queued", row["state"])
        self.assertIsNone(row["needs"])
        self.assertEqual("gemma4:e4b", row["critique_by"])
        self.assertEqual("let one circle fall out of phase", row["critique"])
        self.assertIn("Revise: let one circle fall out of phase", row["prompt"])
        self.assertTrue(row["prompt"].startswith("a field of circles that drift"))

    def test_at_the_depth_limit_the_child_is_held_and_needs_review(self):
        root = self.published_entry("the root of a long line")
        deep = self.published_entry(
            "two critiques in already", parent=root, generation=2
        )
        status, location = self.post(
            f"/entry/{deep}/spawn",
            {"critique": "one more turn of the same idea",
             "critique_by": "gemma4:e4b", "back": "/held"},
        )
        self.assertEqual(303, status)
        self.assertIn("depth", location)

        conn = self.db()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE parent_entry_id = ?", (deep,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("queued", row["state"])
        self.assertEqual("review", row["needs"])
        self.assertEqual("hold", row["publication"])

    def test_spawning_from_a_held_entry_queues_a_child(self):
        # A revision is how a person decides whether a held parent is worth
        # publishing (instructor, 2026-09-14), so the held card's form works.
        status, location = self.post(
            f"/entry/{self.entry_id}/spawn",
            {"critique": "slower and warmer", "critique_by": "profcarroll"},
        )
        self.assertEqual(303, status)
        self.assertIn("Queued", location)
        conn = self.db()
        try:
            child = conn.execute(
                "SELECT id, state, publication FROM jobs WHERE parent_entry_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (self.entry_id,),
            ).fetchone()
            self.assertIsNotNone(child)
            self.assertEqual("queued", child["state"])
            self.assertEqual("hold", child["publication"])
            conn.execute("DELETE FROM jobs WHERE id = ?", (child["id"],))
            conn.commit()
        finally:
            conn.close()

    def test_a_refusal_flashes_as_a_warning(self):
        conn = self.db()
        try:
            job = db.enqueue(conn, "a closed one", "student-two")
            conn.execute("UPDATE jobs SET state = 'rejected' WHERE id = ?", (job,))
            closed = db.create_entry(conn, job, "rejected", prompt="a closed one")
            conn.commit()
        finally:
            conn.close()
        try:
            status, location = self.post(
                f"/entry/{closed}/spawn",
                {"critique": "slower", "critique_by": "profcarroll"},
            )
            self.assertEqual(303, status)
            self.assertIn("refused", location)
            page = self.text(location[location.index("/"):])
            self.assertIn('class="flash warn"', page)
            self.assertIn("nothing spawned", page)
        finally:
            conn = self.db()
            conn.execute("DELETE FROM entries WHERE id = ?", (closed,))
            conn.execute("DELETE FROM jobs WHERE id = ?", (job,))
            conn.commit()
            conn.close()

    def test_the_form_is_on_the_held_cards_and_on_the_job_page(self):
        held = self.text("/held")
        self.assertIn(f'action="/entry/{self.entry_id}/spawn"', held)
        self.assertIn("Spawn a child", held)
        job = self.text(f"/job/{self.held_id}")
        self.assertIn(f'action="/entry/{self.entry_id}/spawn"', job)


class TestStaticFiles(WebTestCase):
    def test_a_gate_png_is_served_as_a_png(self):
        status, content_type, body = self.get(
            f"/jobs/{self.held_id}/attempt-1/.gate/strip.png"
        )
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(body, PNG_BYTES)

    def test_the_report_json_is_served(self):
        status, content_type, body = self.get(
            f"/jobs/{self.held_id}/attempt-1/.gate/report.json"
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertEqual(json.loads(body)["exit"], 0)

    def test_traversal_out_of_the_jobs_directory_is_404(self):
        for path in (
            "/jobs/../etc/passwd",
            "/jobs/%2e%2e/etc/passwd",
            "/jobs/../../etc/passwd",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, raw_path=True)[0], 404)

    def test_an_unservable_extension_is_404(self):
        (self.jobs_dir / str(self.held_id) / "attempt-1" / "sketch.js").write_text(
            "// not served", encoding="utf-8"
        )
        self.assertEqual(
            self.get(f"/jobs/{self.held_id}/attempt-1/sketch.js")[0], 404
        )


class TestPreview(WebTestCase):
    """The attempt directory served as the running sketch it is.

    The instructor's finding: a person cannot decide to publish from a frame
    strip. These tests are about the route that fixes that — that it serves the
    sketch, that it serves only the sketch's own directory, and that the two
    screens where the decision is made offer it.

    *Offer*, since 2026-09-15. They used to frame it, and the Held page framed
    every held entry at once, which is how entry 165 and entry 269 each ran the
    operator's laptop out of memory. Nothing renders an iframe now; a click
    builds one.
    """

    def raw(self, path):
        """(status, headers, body) with the path sent exactly as written."""
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def test_the_directory_url_serves_index_html(self):
        status, headers, body = self.raw(f"/preview/{self.held_id}/1/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b'<script src="sketch.js">', body)
        self.assertIn(b"cdnjs.cloudflare.com", body)

    def test_the_preview_may_be_framed_by_this_ui_and_no_other(self):
        _, headers, _ = self.raw(f"/preview/{self.held_id}/1/")
        self.assertEqual(
            headers.get("Content-Security-Policy"), "frame-ancestors 'self'"
        )

    def test_sketch_js_is_served_as_javascript(self):
        status, headers, body = self.raw(f"/preview/{self.held_id}/1/sketch.js")
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", headers.get("Content-Type", ""))
        self.assertIn(b"createCanvas", body)
        self.assertEqual(
            headers.get("Content-Security-Policy"), "frame-ancestors 'self'"
        )

    def test_the_directory_url_without_its_slash_redirects(self):
        status, headers, _ = self.raw(f"/preview/{self.held_id}/1")
        self.assertEqual(status, 301)
        self.assertEqual(headers.get("Location"), f"/preview/{self.held_id}/1/")

    def test_nothing_outside_the_attempt_directory_is_reachable(self):
        for path in (
            f"/preview/{self.held_id}/1/../../etc",
            f"/preview/{self.held_id}/1/../../../etc/passwd",
            f"/preview/{self.held_id}/1/%2e%2e/%2e%2e/etc/passwd",
            # the sibling attempt's files are somebody else's attempt
            f"/preview/{self.held_id}/1/../attempt-2/sketch.js",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.raw(path)[0], 404)

    def test_a_missing_attempt_is_404(self):
        self.assertEqual(self.raw(f"/preview/{self.held_id}/9/")[0], 404)
        self.assertEqual(self.raw("/preview/9999/1/")[0], 404)

    def test_only_the_sketchs_own_file_types_are_served(self):
        # prompt.txt sits in the same directory and is not part of the sketch
        self.assertEqual(self.raw(f"/preview/{self.held_id}/1/prompt.txt")[0], 404)

    def test_the_job_page_offers_the_sketch_without_running_it(self):
        page = self.text(f"/job/{self.held_id}")
        self.assertIn(f'data-src="/preview/{self.held_id}/1/"', page)
        self.assertNotIn("<iframe", page)
        # the poster is the strip the gate saw, inside the button
        self.assertIn("data-play", page)
        self.assertIn(f"/jobs/{self.held_id}/attempt-1/.gate/strip.png", page)
        # and is openable on its own
        self.assertIn("open in a tab", page)
        # every attempt is reachable, folded away
        self.assertIn("<details", page)

    def test_the_held_card_offers_the_sketch_without_running_it(self):
        page = self.text("/held")
        self.assertIn(f'data-src="/preview/{self.held_id}/1/"', page)
        # the strip the gate saw is the poster the play button sits on, and
        # since packet 6 that is the only place it is drawn
        self.assertIn("strip.png", page)

    def test_the_held_page_renders_no_iframe_at_all(self):
        # The one that matters. Opening /held used to start every held sketch
        # in the operator's browser at once; two of them allocated a GPU buffer
        # per line() per frame, and the machine went down. There is no amount of
        # iframe on this page that is safe, so there is none.
        page = self.text("/held")
        body = page.split("<main>", 1)[1].split("</main>", 1)[0]
        self.assertNotIn("<iframe", body)
        self.assertNotIn("<iframe", page.split("<script>")[0])
        # what it renders instead
        self.assertIn("data-preview", body)
        self.assertIn("data-play", body)

    def test_the_frame_a_click_builds_is_sandboxed(self):
        page = self.text("/held")
        self.assertIn('frame.setAttribute("sandbox", "allow-scripts")', page)
        self.assertIn("frame.remove()", page)

    def test_the_cards_carry_what_the_gate_run_cost(self):
        page = self.text("/held")
        # the seeded report is 4.2 s and records no ms_per_frame
        self.assertIn("gate 4s", page)
        self.assertIn("ms/frame not recorded", page)
        self.assertNotIn("chip cost warn", page)

    def test_a_slow_gate_run_gets_a_warning_chip(self):
        report = report_json(self.jobs_dir / str(self.held_id) / "attempt-1")
        report["timings"] = {"launch_s": 0.7, "load_s": 0.3, "total_s": 208.8,
                             "ms_per_frame": 1093.0}
        self.assertIn("chip cost warn", web.cost_chip(report))
        self.assertIn("1093 ms/frame", web.cost_chip(report))
        self.assertIn("3m 28s", web.cost_chip(report))

    def test_a_failed_frame_budget_gets_a_warning_chip_however_quick_the_run(self):
        # The wall ceiling stops a run early, so a budget failure can come with
        # a short total_s. The chip is about the sketch, not the clock.
        report = report_json(self.jobs_dir / str(self.held_id) / "attempt-1")
        report["timings"] = {"launch_s": 0.7, "load_s": 0.3, "total_s": 12.0,
                             "ms_per_frame": 340.0}
        report["checks"]["frame_budget"] = False
        chip = web.cost_chip(report)
        self.assertIn("chip cost warn", chip)
        self.assertIn("the frame budget failed", chip)

    def test_a_report_that_clears_the_budget_gets_a_plain_chip(self):
        report = report_json(self.jobs_dir / str(self.held_id) / "attempt-1")
        report["timings"] = {"launch_s": 0.7, "load_s": 0.3, "total_s": 2.4,
                             "ms_per_frame": 9.2}
        report["checks"]["frame_budget"] = True
        chip = web.cost_chip(report)
        self.assertNotIn("warn", chip)
        self.assertIn("9.2 ms/frame", chip)

    def test_no_report_says_so_rather_than_saying_nothing(self):
        self.assertIn("no gate report", web.cost_chip(None))

    def test_a_job_with_no_index_html_offers_nothing_to_run(self):
        page = self.text(f"/job/{self.queued_id}")
        self.assertNotIn("<iframe", page)
        # The script that knows how to build one is on every page; what this
        # page has none of is something for it to build.
        self.assertNotIn('data-src="/preview/', page)


class TestLiveTranscript(WebTestCase):
    """Packet 4.3: ``/events/job/<id>`` and the JSON the state event is built from.

    Every test here drives the stream over a raw ``http.client`` connection
    with a socket timeout, because the point of the endpoint is what arrives
    *while* the connection is open — urllib would sit on it until EOF.
    """

    def setUp(self):
        self._streams = []

    def tearDown(self):
        for pair in list(self._streams):
            self.close_stream(*pair)
        self._streams = []
        # A server thread only notices a closed reader on its next write, so
        # give the slots time to come back before the next test needs them.
        self.wait_for_streams(0, deadline_s=20)

    # -- helpers -----------------------------------------------------------

    def stream(self, path, timeout=3):
        """Open one SSE connection. Returns (connection, response)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", path)
        response = conn.getresponse()
        self._streams.append((conn, response))
        return conn, response

    @staticmethod
    def close_stream(conn, response):
        """Hang up. The response holds the socket, so it is what must close.

        ``Connection: close`` makes http.client drop its own reference the
        moment the headers are read, so ``conn.close()`` alone leaves the
        socket open on the response's file object and the server never sees
        the hangup.
        """
        for closeable in (response, conn):
            try:
                closeable.close()
            except OSError:
                pass

    def read_lines(self, response, until, deadline_s=2.0):
        """Read SSE lines until ``until(collected)`` says stop, or time runs out.

        Returns the lines collected. A read that times out is not a failure by
        itself — the caller asserts on what did arrive, and when.
        """
        collected = []
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            try:
                raw = response.readline()
            except (TimeoutError, OSError):
                break
            if not raw:
                collected.append(None)  # EOF: the server closed its end
                break
            collected.append(raw.decode("utf-8").rstrip("\n"))
            if until(collected):
                break
        return collected

    def wait_for_streams(self, n, deadline_s=10.0):
        end = time.monotonic() + deadline_s
        while web.live_streams() != n and time.monotonic() < end:
            time.sleep(0.05)
        return web.live_streams()

    @staticmethod
    def data_lines(lines):
        return [line[6:] for line in lines if line and line.startswith("data: ")]

    # -- the stream --------------------------------------------------------

    def test_the_tail_arrives_at_once_then_new_lines_as_they_are_written(self):
        log = self.jobs_dir / str(self.held_id) / "job.log"
        conn, response = self.stream(f"/events/job/{self.held_id}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get("Content-Type"), "text/event-stream")
        self.assertEqual(response.headers.get("Cache-Control"), "no-cache")

        # The 40 lines already on disk, before anything else happens.
        seen = self.read_lines(
            response, lambda got: "data: 2026-09-14T12:00:39Z job %d: line 39"
            % self.held_id in got,
            deadline_s=2.0,
        )
        self.assertIn(
            "data: 2026-09-14T12:00:00Z job %d: line 0" % self.held_id, seen
        )
        self.assertIn(
            "data: 2026-09-14T12:00:39Z job %d: line 39" % self.held_id, seen
        )
        self.assertIn("event: line", seen)

        # …and a line appended after the connection is open.
        marker = "2026-09-14T12:01:00Z job %d: appended after connect" % self.held_id
        started = time.monotonic()
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(marker + "\n")
        seen = self.read_lines(
            response, lambda got: ("data: " + marker) in got, deadline_s=2.0
        )
        self.assertIn("data: " + marker, seen)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_the_state_event_carries_the_state_and_the_attempt_count(self):
        conn, response = self.stream(f"/events/job/{self.held_id}")
        seen = self.read_lines(
            response,
            lambda got: "event: state" in got and got[-1].startswith("data: "),
            deadline_s=3.0,
        )
        self.assertIn("event: state", seen)
        payload = json.loads(self.data_lines(seen[seen.index("event: state") :])[0])
        self.assertEqual(payload["state"], "held")
        self.assertEqual(payload["id"], self.held_id)
        self.assertEqual(payload["attempt_n"], 1)
        self.assertEqual(payload["max_attempts"], 3)
        self.assertFalse(payload["terminal"])

    def test_a_terminal_state_sends_done_and_closes(self):
        conn = self.db()
        try:
            job_id = db.enqueue(conn, "a job about to be cancelled", "student-one")
        finally:
            conn.close()
        (self.jobs_dir / str(job_id)).mkdir(parents=True, exist_ok=True)
        (self.jobs_dir / str(job_id) / "job.log").write_text(
            "2026-09-14T12:00:00Z job %d: queued\n" % job_id, encoding="utf-8"
        )
        stream_conn, response = self.stream(f"/events/job/{job_id}", timeout=6)
        self.read_lines(response, lambda got: "event: state" in got, deadline_s=3.0)

        conn = self.db()
        try:
            db.transition(conn, job_id, "failed", last_error="cancelled by operator")
        finally:
            conn.close()

        seen = self.read_lines(response, lambda got: None in got, deadline_s=6.0)
        self.assertIn("event: done", seen)
        self.assertIn(None, seen)  # the server closed the connection itself
        self.assertLess(seen.index("event: done"), seen.index(None))

    def test_an_unknown_job_is_404(self):
        conn, response = self.stream("/events/job/9999")
        self.assertEqual(response.status, 404)
        response.read()

    def test_the_ninth_concurrent_stream_is_503(self):
        held = []
        for _ in range(web.MAX_STREAMS):
            conn, response = self.stream(f"/events/job/{self.held_id}", timeout=6)
            self.assertEqual(response.status, 200)
            # Read one frame so the handler is certainly inside the stream and
            # has taken its slot before the next connection is made.
            self.read_lines(response, lambda got: len(got) >= 1, deadline_s=3.0)
            held.append((conn, response))
        self.assertEqual(self.wait_for_streams(web.MAX_STREAMS), web.MAX_STREAMS)

        conn, response = self.stream(f"/events/job/{self.held_id}")
        self.assertEqual(response.status, 503)
        response.read()

        for pair in held:
            self.close_stream(*pair)
        self.assertEqual(self.wait_for_streams(0, deadline_s=20), 0)

        # …and the slots really did come back.
        conn, response = self.stream(f"/events/job/{self.held_id}")
        self.assertEqual(response.status, 200)

    def test_truncation_restarts_from_the_beginning(self):
        conn = self.db()
        try:
            job_id = db.enqueue(conn, "a job whose log is rewritten", "student-one")
        finally:
            conn.close()
        directory = self.jobs_dir / str(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / "job.log"
        log.write_text(
            "".join(f"2026-09-14T12:00:0{n}Z before {n}\n" for n in range(3)),
            encoding="utf-8",
        )
        stream_conn, response = self.stream(f"/events/job/{job_id}", timeout=5)
        self.read_lines(
            response, lambda got: "data: 2026-09-14T12:00:02Z before 2" in got,
            deadline_s=3.0,
        )
        # Rewritten shorter: the tailer must start again from zero rather than
        # read from the middle of a line at the old offset.
        log.write_text("2026-09-14T12:05:00Z after the truncation\n", encoding="utf-8")
        seen = self.read_lines(
            response,
            lambda got: "data: 2026-09-14T12:05:00Z after the truncation" in got,
            deadline_s=4.0,
        )
        self.assertIn("data: 2026-09-14T12:05:00Z after the truncation", seen)

    def test_a_partial_line_waits_for_its_newline(self):
        tailer = web.LogTailer(self.jobs_dir / "nowhere" / "job.log")
        self.assertEqual(tailer.initial(), [])  # no file yet is not an error
        path = self.jobs_dir / "partial.log"
        path.write_text("one\ntw", encoding="utf-8")
        tailer = web.LogTailer(path)
        self.assertEqual(tailer.initial(), ["one"])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("o\n")
        self.assertEqual(tailer.poll(), ["two"])
        self.assertEqual(tailer.poll(), [])

    # -- the JSON ----------------------------------------------------------

    def test_api_job_json_has_the_row_the_attempts_and_the_tail(self):
        status, content_type, body = self.get(f"/api/job/{self.held_id}.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        document = json.loads(body)
        self.assertEqual(document["id"], self.held_id)
        self.assertEqual(document["state"], "held")
        self.assertEqual(document["attempt_n"], 1)
        self.assertEqual(document["max_attempts"], 3)
        self.assertFalse(document["terminal"])
        self.assertEqual(document["job"]["prompt"], "three circles breathing")
        self.assertEqual(document["job"]["submitted_by"], "student-two")
        self.assertEqual(document["job"]["assertions"], [])
        self.assertEqual(len(document["attempt_rows"]), 1)
        self.assertEqual(document["attempt_rows"][0]["gate_exit"], 0)
        self.assertEqual(document["log_lines"], 200)
        self.assertIn("line 39", document["log"])
        self.assertNotIn("@", document["job"]["submitted_by"])  # username only

    def test_api_job_json_for_an_unknown_job_is_404(self):
        self.assertEqual(self.get("/api/job/9999.json")[0], 404)


class TestBindRefusal(unittest.TestCase):
    """--bind anything but the loopback is a refusal: exit 3, one line, no socket."""

    def test_cli_refuses_a_public_bind(self):
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "bin" / "sketchgen"),
                "web",
                "--bind",
                "0.0.0.0",
                "--once-for-test",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "SKETCHGEN_DB": "/nonexistent/never-created.db"},
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_make_server_refuses_too(self):
        with self.assertRaises(web.Refused):
            web.make_server(bind="0.0.0.0", port=0)

    def test_help_works(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "sketchgen"), "web", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--once-for-test", result.stdout)


if __name__ == "__main__":
    unittest.main()
