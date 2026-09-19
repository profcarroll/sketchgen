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
import contextlib
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
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen import models  # noqa: E402
from sketchgen import web  # noqa: E402
from sketchgen import worker  # noqa: E402

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

        # Whoever ran the suite is not the operator under test: no login from
        # gh (the cache says "asked, none, never ask again") and none from
        # the environment. TestNewJob sets each on purpose and puts it back.
        cls._operator_env = os.environ.pop("SKETCHGEN_OPERATOR", None)
        cls._gh_cache_before = dict(web._gh_cache)
        web._gh_cache.clear()
        web._gh_cache.update(login=None, asked=float("inf"))

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
        web._gh_cache.clear()
        web._gh_cache.update(cls._gh_cache_before)
        if cls._operator_env is not None:
            os.environ["SKETCHGEN_OPERATOR"] = cls._operator_env

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
        # be marked for publication onto the rejections page, but there is
        # nothing to reject. One already published has left the page.
        #
        # The verbs are marks in one page-wide form since packet 12, so what
        # names the entry is the control's own name and not a form action.
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
            self.assertIn(f'name="do-{kept}" value="publish"', page)
            self.assertNotIn(f'name="do-{kept}" value="reject"', page)
            self.assertNotIn(f'id="entry-{done}"', page)
            self.assertNotIn(f'name="do-{done}"', page)
            self.assertIn(f'name="do-{self.entry_id}" value="reject"', page)
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
            'data-k="odometer.session.slot_ours_pct"',
            # the Worker tiles became the status card in packet 5; control, up
            # since and slot survive in its foot line
            'data-act="headline"',
            'data-act="foot"',
        ):
            self.assertIn(path, page)
        self.assertNotIn('data-k="worker.job_in_flight"', page)

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
        # the job is named as a job on the meta line, every control still says
        # which entry it acts on — in its aria-label since packet 6, because
        # the id in four button labels was the same number said four more
        # times — the queue has an entry column, and the job page names its
        # entry. Since packet 12 the four verbs mark rather than act, and the
        # labels say so.
        held = self.text("/held")
        self.assertIn(f'<span class="n">{self.entry_id}</span>', held)
        self.assertIn(f'job <a href="/job/{self.held_id}">{self.held_id}</a>', held)
        self.assertIn(f'aria-label="Mark to publish entry {self.entry_id}"', held)
        self.assertIn(f'aria-label="Mark to reject entry {self.entry_id}"', held)
        self.assertIn(
            f'aria-label="Mark for a critique child of entry {self.entry_id}"', held
        )
        self.assertIn(f'aria-label="Mark to archive entry {self.entry_id}"', held)
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


class TestStatusCard(WebTestCase):
    """Packet 5: the process status card, on both pages and in the poll.

    The worker is a different process and is not running in these tests, so
    every row here is written by hand — which is also the point: the card is
    whatever the database says, and nothing else.
    """

    def step(self, *, step="writing", headline="Writing the sketch",
             detail="qwen3-coder:30b · job 42, attempt 2 of 3", pid=None):
        conn = self.db()
        try:
            conn.execute("DELETE FROM activity")
            db.begin_step(conn, step=step, headline=headline, detail=detail,
                          pid=pid or os.getpid())
            conn.commit()
        finally:
            conn.close()
        self.addCleanup(self.clear)

    def clear(self):
        conn = self.db()
        try:
            conn.execute("DELETE FROM activity")
            conn.commit()
        finally:
            conn.close()

    def test_the_console_and_the_queue_both_carry_the_card(self):
        self.step()
        console_page = self.text("/")
        self.assertIn('class="panel status"', console_page)
        self.assertIn('data-act="pill"', console_page)
        self.assertIn("Writing the sketch", console_page)
        self.assertIn("qwen3-coder:30b · job 42, attempt 2 of 3", console_page)
        # the Console's Worker tiles are gone; their three values are in the foot
        self.assertNotIn("<h3>Worker</h3>", console_page)
        self.assertIn('data-act="foot"', console_page)

        queue_page = self.text("/queue")
        self.assertIn('class="panel status compact"', queue_page)
        self.assertIn("Writing the sketch", queue_page)
        self.assertIn("Console ↗", queue_page)
        # one card on that page, not a second panel
        self.assertEqual(1, queue_page.count('class="panel status compact"'))

    def test_control_json_carries_the_card_the_poll_paints(self):
        self.step()
        status, content_type, body = self.get("/api/control.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        card = json.loads(body)["activity"]
        self.assertEqual("writing", card["step"])
        self.assertEqual("Writing the sketch", card["headline"])
        self.assertEqual("running", card["state"])
        # the pill and the elapsed text are decided server-side, so the page
        # script never has to know what a state means
        self.assertEqual("running", card["pill_text"])
        self.assertEqual("ok", card["pill_class"])
        self.assertIn("elapsed_text", card)
        self.assertIn("recent", card)

    def test_no_rows_renders_the_unknown_card_rather_than_raising(self):
        self.clear()
        page = self.text("/")
        self.assertIn('class="panel status"', page)
        self.assertIn("Nothing recorded yet", page)
        self.assertIn('class="pill quiet" data-act="pill">unknown', page)
        card = json.loads(self.get("/api/control.json")[2])["activity"]
        self.assertEqual("unknown", card["state"])

    def test_a_worker_that_is_gone_says_so_with_the_command_to_check_it(self):
        self.step(step="evaluating",
                  headline="Evaluating the sketch in a browser", pid=4194305)
        page = self.text("/")
        self.assertIn("Worker not running", page)
        self.assertIn("systemctl --user status sketchgen-worker", page)
        self.assertIn('class="pill bad" data-act="pill">not running', page)

    def test_a_detail_a_model_wrote_is_escaped(self):
        self.step(detail='<script>alert("nope")</script> & co')
        page = self.text("/")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert", page)

    def test_a_step_with_no_median_draws_the_ribbon(self):
        # Until 2026-09-16 this asserted the track was `hidden`, which drew an
        # empty grey track anyway: `.bar { display: flex }` is an author rule
        # and beats the browser's own [hidden]. The track is always there now
        # and its class says which of the three states it is in.
        self.step()
        page = self.text("/")
        self.assertIn('class="bar working" data-act="track"', page)
        self.assertNotIn("hidden", page.split('data-act="track"')[1][:40])

    def test_a_step_past_its_median_keeps_its_track_and_turns_amber(self):
        card = web.activity_card({"activity": {
            "headline": "Writing the sketch", "detail": "…",
            "elapsed_s": 300.0, "median_s": 67.0, "state": "running",
            "recent": [],
        }})
        self.assertIn('style="width:100.0%"', card)
        self.assertIn('class="over"', card)
        self.assertIn("5m 00s of about 1m 07s", card)


class TestBarStates(unittest.TestCase):
    """The bar says one of three things, and the track is always drawn.

    A fill is a measured step. A ribbon is a step that is running and cannot be
    measured — fewer than five of it have finished, and some steps will never
    have five. An empty track is a worker that is not working. The three are
    decided in Python, once, because the poll repaints the same card.
    """

    def act(self, **over):
        card = {"step": "writing", "headline": "Writing the sketch",
                "detail": "…", "elapsed_s": 24.0, "median_s": None,
                "state": "running", "recent": []}
        card.update(over)
        return card

    def test_measured_fills_and_does_not_stripe(self):
        act = self.act(elapsed_s=41.0, median_s=67.0)
        self.assertAlmostEqual(61.19, web.activity_bar_pct(act), places=1)
        self.assertEqual("", web.activity_bar_class(act))

    def test_running_without_a_median_stripes(self):
        act = self.act()
        self.assertIsNone(web.activity_bar_pct(act))
        self.assertEqual("working", web.activity_bar_class(act))

    def test_a_stalled_step_stripes_in_amber(self):
        act = self.act(elapsed_s=3000.0, state="stalled")
        self.assertEqual("working late", web.activity_bar_class(act))

    def test_the_nap_is_not_work_however_long_it_has_run(self):
        # The worker opens an `idle` row when it sleeps, on purpose, so an open
        # row exists for as long as the process does. A long nap is not
        # progress towards anything, and a nap with a median is not either.
        act = self.act(step="idle", headline="Nothing to do", state="idle",
                       elapsed_s=200.0, median_s=30.0)
        self.assertIsNone(web.activity_bar_pct(act))
        self.assertEqual("", web.activity_bar_class(act))
        self.assertFalse(web.activity_working(act))

    def test_idle_work_is_work(self):
        # judging and critiquing land under the `idle` STATE beside the nap,
        # which is why the bar reads the step and not the state.
        act = self.act(step="critiquing", headline="Critiquing entry 214",
                       state="idle")
        self.assertTrue(web.activity_working(act))
        self.assertEqual("working", web.activity_bar_class(act))

    def test_paused_gone_and_empty_all_draw_an_empty_track(self):
        for state in ("paused", "gone", "unknown"):
            with self.subTest(state=state):
                act = self.act(state=state, elapsed_s=900.0, median_s=67.0)
                self.assertIsNone(web.activity_bar_pct(act))
                self.assertEqual("", web.activity_bar_class(act))

    def test_the_card_never_hides_the_track(self):
        for act in (self.act(), self.act(state="paused"),
                    self.act(elapsed_s=41.0, median_s=67.0)):
            with self.subTest(state=act["state"], median=act["median_s"]):
                card = web.activity_card({"activity": act})
                self.assertIn('data-act="track"', card)
                self.assertNotIn("hidden", card)

    def test_one_job_is_not_one_jobs(self):
        def foot(session):
            return web.activity_foot({
                "worker": {"control": "running"},
                "funnel": {"generated": {"session": session}},
            })
        self.assertIn("1 job this session", foot(1))
        self.assertIn("2 jobs this session", foot(2))
        self.assertIn("0 jobs this session", foot(0))


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


class TestBillingCard(unittest.TestCase):
    """The bill, and how old the figure is."""

    def test_nothing_recorded_says_how_to_record_it(self):
        card = web.billing_card({})
        self.assertIn("Nothing recorded yet", card)
        self.assertIn("bin/sketchgen billing", card)

    def test_a_recorded_figure_is_shown_with_its_window(self):
        card = web.billing_card({
            "amount": "0", "currency": "USD",
            "through": "2026-09-17", "checked": db.utc_now(),
        })
        self.assertIn("0.00", card)
        self.assertIn("USD", card)
        self.assertIn("through 2026-09-17", card)
        # never claims to be live
        self.assertIn("Not live", card)
        self.assertNotIn("days old", card)

    def test_a_stale_figure_says_so(self):
        card = web.billing_card({
            "amount": "12.5", "currency": "USD", "through": "2026-08-01",
            "checked": "2026-08-01T00:00:00Z",
        })
        self.assertIn("12.50", card)
        self.assertIn("days old", card)

    def test_an_unreadable_amount_does_not_crash_the_console(self):
        card = web.billing_card({"amount": "not-a-number", "currency": "USD"})
        self.assertIn("not-a-number", card)


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
        self.assertEqual(sorted(job.assertions), ["no_motion", "responds(click)",
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


class TestNewJob(WebTestCase):
    """The New job page after its 2026-09-18 redesign: signed by the node's
    login, a parent you can see, defaults you can keep, a batch per line."""

    def setUp(self):
        self._env = os.environ.pop("SKETCHGEN_OPERATOR", None)

    def tearDown(self):
        os.environ.pop("SKETCHGEN_OPERATOR", None)
        if self._env is not None:
            os.environ["SKETCHGEN_OPERATOR"] = self._env
        web._gh_cache.clear()
        web._gh_cache.update(login=None, asked=float("inf"))
        conn = self.db()
        try:
            db.set_meta(conn, web.DEFAULTS_KEY, None)
        finally:
            conn.close()

    def signed_in_as(self, login):
        web._gh_cache.clear()
        web._gh_cache.update(login=login, asked=float("inf"))

    def post_page(self, path, fields):
        """(status, body text) for a POST that answers with a page."""
        data = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        request = urllib.request.Request(self.url(path), data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read().decode("utf-8")

    def newest_job(self):
        conn = self.db()
        try:
            return db.get_job(conn, max(job.id for job in db.list_jobs(conn)))
        finally:
            conn.close()

    def queued_ids(self):
        conn = self.db()
        try:
            return {job.id for job in db.list_jobs(conn, "queued")}
        finally:
            conn.close()

    # -- who signs it --------------------------------------------------------

    def test_the_nodes_login_signs_the_job(self):
        self.signed_in_as("profcarroll")
        page = self.text("/new")
        self.assertIn('<span class="pill ok" title="via gh on this node">profcarroll</span>', page)
        self.assertIn('<div id="other-box" hidden>', page)
        self.assertNotIn("required", page.split("Submitted by")[1].split("Parent entry")[0])
        status, location = self.post("/new", {"prompt": "a signed sketch"})
        self.assertEqual(status, 303, location)
        self.assertEqual("profcarroll", self.newest_job().submitted_by)

    def test_the_environment_outranks_gh(self):
        self.signed_in_as("profcarroll")
        os.environ["SKETCHGEN_OPERATOR"] = "teaching-assistant"
        page = self.text("/new")
        self.assertIn("teaching-assistant", page)
        self.assertIn("via $SKETCHGEN_OPERATOR", page)
        # (the gallery link in the header names profcarroll; the pill must not)
        self.assertNotIn(">profcarroll</span>", page)
        self.assertEqual("teaching-assistant", web.operator_username())

    def test_a_typed_name_is_somebody_elses_job(self):
        self.signed_in_as("profcarroll")
        status, _ = self.post("/new", {"prompt": "for a student", "submitted_by": "student-nine"})
        self.assertEqual(status, 303)
        self.assertEqual("student-nine", self.newest_job().submitted_by)
        # ...and a refused one keeps the student's name in an open box
        status, body = self.post_page(
            "/new", {"prompt": "", "submitted_by": "student-nine"}
        )
        self.assertEqual(status, 400)
        self.assertIn('<div id="other-box">', body)
        self.assertIn('value="student-nine"', body)

    def test_without_a_login_the_box_is_required_and_blank_is_refused(self):
        page = self.text("/new")
        self.assertIn('name="submitted_by" required', page)
        self.assertIn("No GitHub login on this node", page)
        before = self.queued_ids()
        status, body = self.post_page("/new", {"prompt": "unsigned"})
        self.assertEqual(status, 400)
        self.assertIn("no GitHub login on this node", body)
        self.assertEqual(before, self.queued_ids())

    # -- the parent ------------------------------------------------------------

    def test_the_parent_is_drawn_and_presets_planner_and_rules(self):
        page = self.text(f"/new?parent={self.entry_id}")
        self.assertIn(f'value="{self.entry_id}"', page)
        self.assertIn(f'<a href="/entry/{self.entry_id}">entry {self.entry_id}</a>', page)
        self.assertIn("three circles breathing", page)
        self.assertIn("generation 0", page)
        # the entry ran under control; spawn() would keep it, and so does this
        self.assertIn('<option value="control" selected>', page)
        self.assertIn('class="pick on"', page)
        self.assertIn("recent entries a line can grow from", page)

    def test_a_parent_that_cannot_grow_a_line_is_refused(self):
        conn = self.db()
        try:
            job_id = db.enqueue(conn, "a closed line", "student-two")
            db.transition(conn, job_id, "executing", executor="x")
            db.transition(conn, job_id, "gating")
            db.transition(conn, job_id, "held")
            closed = db.create_entry(conn, job_id, "held", prompt="a closed line")
            db.entry_transition(conn, closed, "rejected", reject_reason="no")
        finally:
            conn.close()
        page = self.text(f"/new?parent={closed}")
        self.assertIn("this will be refused", page)
        before = self.queued_ids()
        status, body = self.post_page(
            "/new", {"prompt": "a child of a closed line", "submitted_by": "student-two",
                     "parent_entry_id": str(closed)},
        )
        self.assertEqual(status, 400)
        self.assertIn("rejected", body)
        self.assertEqual(before, self.queued_ids())
        # and an unknown one, as before
        status, body = self.post_page(
            "/new", {"prompt": "orphan", "submitted_by": "student-two",
                     "parent_entry_id": "999999"},
        )
        self.assertEqual(status, 400)
        self.assertIn("there is no entry 999999", body)

    def test_a_job_can_descend_from_the_entry_on_the_job_page(self):
        page = self.text(f"/job/{self.held_id}")
        self.assertIn(f'href="/new?parent={self.entry_id}"', page)

    # -- defaults --------------------------------------------------------------

    def test_save_as_defaults_keeps_the_options_and_the_prompt(self):
        status, body = self.post_page(
            "/new/defaults",
            {"action": "save", "prompt": "still being typed", "planner": "paid",
             "rules": "random", "publication": "auto", "max_attempts": "5",
             "assert": ["responds(click)", "size"], "size_w": "800", "size_h": "600"},
        )
        self.assertEqual(status, 200)
        self.assertIn("Saved as defaults", body)
        self.assertIn("still being typed", body)
        fresh = self.text("/new")
        self.assertIn("Defaults saved", fresh)
        self.assertIn('<option value="paid" selected>', fresh)
        self.assertIn('<option value="random" selected>', fresh)
        self.assertIn('<option value="auto" selected>', fresh)
        self.assertIn('value="5"', fresh)
        self.assertIn('value="responds(click)" checked', fresh)
        self.assertIn('value="size" checked', fresh)
        self.assertIn('value="800"', fresh)
        self.assertNotIn('value="motion(idle)" checked', fresh)
        # a job queued with the form untouched carries them
        status, _ = self.post(
            "/new", {"prompt": "under the defaults", "submitted_by": "student-two",
                     "rules": "random", "publication": "auto", "max_attempts": "5"},
        )
        self.assertEqual(status, 303)
        job = self.newest_job()
        self.assertEqual(("random", "auto", 5), (job.rules_file, job.publication, job.max_attempts))

    def test_forget_them_goes_back_to_the_built_in_ones(self):
        self.post_page("/new/defaults", {"action": "save", "rules": "random", "max_attempts": "7"})
        status, body = self.post_page(
            "/new/defaults", {"action": "clear", "prompt": "kept", "rules": "random"}
        )
        self.assertEqual(status, 200)
        self.assertIn("Forgot the saved defaults", body)
        self.assertIn("kept", body)
        self.assertIn('<option value="treatment" selected>', body)
        fresh = self.text("/new")
        self.assertIn("Built-in defaults", fresh)
        self.assertIn('<option value="treatment" selected>', fresh)
        self.assertIn('value="3"', fresh)

    def test_bad_defaults_are_refused_and_nothing_is_saved(self):
        status, body = self.post_page("/new/defaults", {"action": "save", "max_attempts": "40"})
        self.assertEqual(status, 400)
        self.assertIn("max attempts must be", body)
        conn = self.db()
        try:
            self.assertIsNone(db.get_meta(conn, web.DEFAULTS_KEY))
        finally:
            conn.close()

    # -- a batch, and prompts to run again --------------------------------------

    def test_one_job_per_line_queues_a_batch(self):
        before = self.queued_ids()
        status, location = self.post(
            "/new", {"prompt": "first line\n\n  second line \nthird line",
                     "submitted_by": "student-two", "many": "1", "rules": "random"},
        )
        self.assertEqual(status, 303)
        self.assertIn("Queued%203%20jobs", location)
        new_ids = sorted(self.queued_ids() - before)
        self.assertEqual(3, len(new_ids))
        conn = self.db()
        try:
            prompts = [db.get_job(conn, job_id).prompt for job_id in new_ids]
            rules = {db.get_job(conn, job_id).rules_file for job_id in new_ids}
        finally:
            conn.close()
        self.assertEqual(["first line", "second line", "third line"], prompts)
        self.assertEqual({"random"}, rules)
        # blank lines only is no job at all
        status, body = self.post_page(
            "/new", {"prompt": "\n  \n", "submitted_by": "student-two", "many": "1"}
        )
        self.assertEqual(status, 400)
        self.assertIn("a job needs a prompt", body)

    def test_recent_prompts_are_offered_and_a_link_fills_the_box(self):
        page = self.text("/new")
        self.assertIn("recent prompts, to run again", page)
        self.assertIn("a slow field of dots that drift", page)
        self.assertIn("/new?prompt=a%20slow%20field", page)
        filled = self.text("/new?prompt=" + urllib.parse.quote("hello, gate"))
        self.assertIn(">hello, gate</textarea>", filled)

    def test_the_header_says_where_the_job_would_land(self):
        page = self.text("/new")
        self.assertRegex(page, r"behind \d+ queued jobs?|the queue is empty")


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
        # the card offers the mark; the route it used to post to still answers
        self.assertIn(f'name="do-{entry_id}" value="archive"', page)
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
        # the toggles still name it where it matters
        for label in ("Mark to publish", "Mark to reject", "Mark to archive"):
            self.assertIn(f'aria-label="{label} entry {entry_id}"', card)
        self.assertIn(
            f'aria-label="Mark for a critique child of entry {entry_id}"', card
        )

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

    def test_one_box_four_marks_and_no_form_of_the_cards_own(self):
        # Packet 12: the card's form is gone. The four verbs are toggles bound
        # to the page's one form by attribute, so the cards stay siblings in
        # the grid and nothing on the card is a request.
        _, entry_id = self.card_entry()
        card = self.card(self.text("/held"), entry_id)
        self.assertEqual(0, card.count("<form"))
        self.assertEqual(0, card.count("formaction"))
        self.assertEqual(1, card.count('type="text"'))
        self.assertEqual(1, card.count(f'name="text-{entry_id}"'))
        self.assertEqual(3, card.count('type="radio"'))
        self.assertEqual(1, card.count('type="checkbox"'))
        self.assertEqual(5, card.count('form="held-batch"'))
        for css, name, value in (
            ("pub", f"do-{entry_id}", "publish"),
            ("rej", f"do-{entry_id}", "reject"),
            ("arc", f"do-{entry_id}", "archive"),
        ):
            self.assertIn(f'class="tog {css}"', card)
            self.assertIn(f'name="{name}" value="{value}"', card)
        self.assertIn(f'name="cri-{entry_id}" value="on"', card)
        # critique_by left the page: a sentence typed here is the operator's
        self.assertNotIn("critique_by", card)
        # one help string, under all four
        self.assertEqual(1, card.count("decide-hint"))
        self.assertIn("Nothing happens until Process.", card)

    def test_a_kept_failure_card_has_no_reject(self):
        _, entry_id = self.card_entry(
            state="failed-kept", prompt="one the gate refused"
        )
        page = self.text("/held")
        card = self.card(page, entry_id)
        self.assertNotIn('value="reject"', card)
        self.assertNotIn("Mark to reject", card)
        self.assertNotIn('class="tog rej"', card)
        self.assertEqual(2, card.count('type="radio"'))
        self.assertIn(f'name="do-{entry_id}" value="publish"', card)
        self.assertIn(f'name="do-{entry_id}" value="archive"', card)
        self.assertIn(f'name="cri-{entry_id}"', card)
        # and the kept section still says where publishing one puts it
        self.assertIn("rejections page", page)

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

    def test_the_form_is_on_the_job_page_and_the_held_card_marks_instead(self):
        # The job page still posts one critique to /entry/<id>/spawn. On the
        # Held page the card's Critique toggle marks it instead and the batch
        # queues the child, so the page has one form and not one per card.
        held = self.text("/held")
        self.assertNotIn(f'action="/entry/{self.entry_id}/spawn"', held)
        self.assertIn(f'name="cri-{self.entry_id}"', held)
        self.assertIn(
            f'aria-label="Mark for a critique child of entry {self.entry_id}"', held
        )
        job = self.text(f"/job/{self.held_id}")
        self.assertIn(f'action="/entry/{self.entry_id}/spawn"', job)
        self.assertIn("Spawn a child", job)


class TestSubmissions(WebTestCase):
    """GET /submissions and its two verbs — packet 8's whole operator surface.

    The page is the decision card with the sentence where the sketch is, and
    the point of every test here is the same one: nothing a stranger typed
    reaches the queue until a person presses Release, and what happens when
    they do is a job like any other — held, and signed with their login.
    """

    def add(self, remote_id, *, kind="prompt", username="octocat", entry_id=None,
            text="a tide of small triangles", created_utc="2026-09-16T15:04:22Z"):
        conn = self.db()
        try:
            return db.add_submission(
                conn, remote_id=remote_id, kind=kind, username=username,
                entry_id=entry_id, text=text, created_utc=created_utc,
            )
        finally:
            conn.close()

    def entry_in_state(self, state, prompt="a field of circles that drift"):
        """One entry of this node's own, in the state the test needs."""
        conn = self.db()
        try:
            job_id = db.enqueue(conn, prompt, "student-two", rules_file="control")
            db.transition(conn, job_id, "executing")
            db.transition(conn, job_id, "gating")
            db.transition(conn, job_id, "held")
            if state == "published":
                db.transition(conn, job_id, "published")
            entry_id = db.create_entry(
                conn, job_id, "held", prompt=prompt,
                submitted_by="student-two", rules_file="control",
            )
            if state != "held":
                db.entry_transition(conn, entry_id, state,
                                    **({"reject_reason": "not this one"}
                                       if state == "rejected" else {}))
            return entry_id
        finally:
            conn.close()

    def submission(self, submission_id):
        conn = self.db()
        try:
            return dict(db.submission(conn, submission_id))
        finally:
            conn.close()

    def job(self, job_id):
        conn = self.db()
        try:
            return db.get_job(conn, job_id)
        finally:
            conn.close()

    def critiques_of(self, entry_id):
        conn = self.db()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM critiques WHERE entry_id = ? ORDER BY id",
                    (entry_id,),
                )
            ]
        finally:
            conn.close()

    # -- the page ----------------------------------------------------------

    def test_the_page_lists_pending_rows_oldest_first(self):
        old = self.add(101, created_utc="2026-09-16T10:00:00Z", text="the early one")
        new = self.add(102, created_utc="2026-09-16T12:00:00Z", text="the late one")
        declined = self.add(103, text="the one nobody wants")
        conn = self.db()
        try:
            db.decline_submission(conn, declined, "off topic")
        finally:
            conn.close()

        page = self.text("/submissions")
        self.assertIn("the early one", page)
        self.assertIn("the late one", page)
        self.assertNotIn("the one nobody wants", page)
        self.assertLess(
            page.index(f'id="submission-{old}"'),
            page.index(f'id="submission-{new}"'),
            "oldest first: whoever typed first waits least",
        )
        self.assertIn("@octocat", page)
        # One form, two formactions, no script: the decision card's own shape.
        self.assertIn(f'action="/submissions/{old}/release"', page)
        self.assertIn(f'formaction="/submissions/{old}/decline"', page)
        self.assertNotIn("<script>", page.split("</main>")[0])

    def test_a_critique_shows_the_entry_it_is_about(self):
        entry_id = self.entry_in_state("held", "a slow lattice of lines")
        self.add(110, kind="critique", entry_id=entry_id,
                 text="let the lines thin as they near the edge")
        page = self.text("/submissions")
        self.assertIn("let the lines thin as they near the edge", page)
        self.assertIn("a slow lattice of lines", page)
        self.assertIn(f'href="/entry/{entry_id}"', page)

    def test_the_nav_and_the_console_tile_link_here(self):
        self.assertIn('href="/submissions"', self.text("/queue"))
        self.assertIn('data-k="submissions.pending"', self.text("/"))

    # -- releasing ---------------------------------------------------------

    def test_release_on_a_prompt_queues_a_random_rules_job_held_and_signed(self):
        submission_id = self.add(
            120, username="hubot", text="a tide of small triangles that drifts"
        )
        status, location = self.post(f"/submissions/{submission_id}/release", {})
        self.assertEqual(303, status)
        self.assertTrue(location.startswith("/submissions?flash="), location)

        row = self.submission(submission_id)
        self.assertEqual("released", row["state"])
        self.assertTrue(row["decided_utc"])
        job = self.job(row["job_id"])
        self.assertEqual("queued", job.state)
        self.assertEqual("a tide of small triangles that drifts", job.prompt)
        self.assertEqual("random", job.rules_file)
        self.assertEqual("hold", job.publication)
        self.assertEqual("hubot", job.submitted_by)
        self.assertIsNone(job.parent_entry_id)

    def test_release_on_a_critique_spawns_the_child_and_records_human_login(self):
        entry_id = self.entry_in_state("published", "a quiet grid of dots")
        submission_id = self.add(
            130, kind="critique", entry_id=entry_id, username="octocat",
            text="let the dots drift apart as they fall",
        )
        status, _ = self.post(f"/submissions/{submission_id}/release", {})
        self.assertEqual(303, status)

        row = self.submission(submission_id)
        self.assertEqual("released", row["state"])
        job = self.job(row["job_id"])
        self.assertEqual("queued", job.state)
        self.assertEqual(entry_id, job.parent_entry_id)
        self.assertEqual("octocat", job.submitted_by)
        self.assertEqual("octocat", job.critique_by)
        self.assertEqual("let the dots drift apart as they fall", job.critique)
        # The parent's rules file, not 'random': a line stays a fair
        # comparison with itself (decision §1.6).
        self.assertEqual("control", job.rules_file)
        self.assertIn("Revise:", job.prompt)

        critiques = self.critiques_of(entry_id)
        self.assertEqual(1, len(critiques))
        self.assertEqual("human:octocat", critiques[0]["prompt_version"])
        self.assertEqual("octocat", critiques[0]["critique_by"])
        self.assertEqual(row["job_id"], critiques[0]["spawned_job_id"])

    def test_two_people_may_each_critique_one_entry_but_neither_twice(self):
        entry_id = self.entry_in_state("published", "three circles breathing out")
        first = self.add(140, kind="critique", entry_id=entry_id,
                         username="octocat", text="slower, and further apart")
        second = self.add(141, kind="critique", entry_id=entry_id,
                          username="hubot", text="hold the third one still")
        third = self.add(142, kind="critique", entry_id=entry_id,
                         username="octocat", text="and now in a different colour")

        self.post(f"/submissions/{first}/release", {})
        self.post(f"/submissions/{second}/release", {})
        self.assertEqual("released", self.submission(first)["state"])
        self.assertEqual("released", self.submission(second)["state"])
        self.assertEqual(
            {"human:octocat", "human:hubot"},
            {row["prompt_version"] for row in self.critiques_of(entry_id)},
        )

        # The same person, the same entry, a second time: refused, and the row
        # is left for the operator to decline.
        _, location = self.post(f"/submissions/{third}/release", {})
        self.assertIn("refused", urllib.parse.unquote(location))
        self.assertEqual("pending", self.submission(third)["state"])
        self.assertEqual(2, len(self.critiques_of(entry_id)))

    def test_a_rejected_parent_declines_the_row_rather_than_raising(self):
        entry_id = self.entry_in_state("rejected", "a sketch nobody kept")
        submission_id = self.add(150, kind="critique", entry_id=entry_id,
                                 text="try it again with fewer lines")
        before = len(self.jobs())
        status, location = self.post(f"/submissions/{submission_id}/release", {})
        self.assertEqual(303, status)
        row = self.submission(submission_id)
        self.assertEqual("declined", row["state"])
        self.assertIn("rejected", row["decline_reason"])
        self.assertIsNone(row["job_id"])
        self.assertEqual(before, len(self.jobs()), "nothing was queued")
        self.assertIn("declined", urllib.parse.unquote(location))

    def test_a_released_row_cannot_be_released_twice(self):
        submission_id = self.add(160, text="only once, please")
        self.post(f"/submissions/{submission_id}/release", {})
        job_id = self.submission(submission_id)["job_id"]
        before = len(self.jobs())

        _, location = self.post(f"/submissions/{submission_id}/release", {})
        self.assertIn("already released", urllib.parse.unquote(location))
        self.assertEqual(before, len(self.jobs()), "no second job")
        self.assertEqual(job_id, self.submission(submission_id)["job_id"])

    # -- declining ---------------------------------------------------------

    def test_decline_writes_the_reason_keeps_the_text_and_queues_nothing(self):
        submission_id = self.add(170, text="something the operator will not run")
        before = len(self.jobs())
        status, location = self.post(
            f"/submissions/{submission_id}/decline", {"text": "not this week"}
        )
        self.assertEqual(303, status)
        row = self.submission(submission_id)
        self.assertEqual("declined", row["state"])
        self.assertEqual("not this week", row["decline_reason"])
        self.assertEqual("something the operator will not run", row["text"])
        self.assertIsNone(row["job_id"])
        self.assertEqual(before, len(self.jobs()))
        self.assertIn("not this week", urllib.parse.unquote(location))

    def test_a_decline_with_an_empty_box_still_says_who_did_it(self):
        submission_id = self.add(171, text="no reason given for this one")
        self.post(f"/submissions/{submission_id}/decline", {"text": "  "})
        self.assertEqual(
            "declined by operator", self.submission(submission_id)["decline_reason"]
        )

    def test_a_submission_that_is_not_there_changes_nothing(self):
        _, location = self.post("/submissions/99999/release", {})
        self.assertIn("there is no submission", urllib.parse.unquote(location))
        _, location = self.post("/submissions/99999/decline", {"text": "no"})
        self.assertIn("there is no submission", urllib.parse.unquote(location))

    def jobs(self):
        conn = self.db()
        try:
            return db.list_jobs(conn)
        finally:
            conn.close()


class TestSubmissionsCLI(unittest.TestCase):
    """`sketchgen submissions` — the same three verbs without the tunnel.

    A subprocess against a temp database, because what is under test is the
    exit codes as a person over SSH would see them: 0 released, 3 refused, and
    1 for the one outcome that is neither.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sketchgen-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "sketchgen.db"
        db.init(self.db_path)
        conn = db.connect(self.db_path)
        try:
            self.prompt_id = db.add_submission(
                conn, remote_id=1, kind="prompt", username="octocat",
                entry_id=None, text="a tide of small triangles",
                created_utc="2026-09-16T15:04:22Z",
            )
            self.other_id = db.add_submission(
                conn, remote_id=2, kind="prompt", username="hubot",
                entry_id=None, text="a grid that loses its corners",
                created_utc="2026-09-16T15:05:00Z",
            )
        finally:
            conn.close()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "sketchgen"), "submissions",
             *args, "--db", str(self.db_path)],
            capture_output=True, text=True, timeout=60,
        )

    def row(self, submission_id):
        conn = db.connect(self.db_path)
        try:
            return dict(db.submission(conn, submission_id))
        finally:
            conn.close()

    def test_bare_submissions_lists_what_is_waiting(self):
        result = self.run_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("a tide of small triangles", result.stdout)
        self.assertIn("@octocat", result.stdout)
        self.assertIn("2 waiting", result.stdout)

    def test_release_queues_the_job_and_decline_keeps_the_row(self):
        released = self.run_cli("release", "--id", str(self.prompt_id), "--json")
        self.assertEqual(0, released.returncode, released.stderr)
        document = json.loads(released.stdout)
        self.assertEqual("released", document["outcome"])
        conn = db.connect(self.db_path)
        try:
            job = db.get_job(conn, document["job_id"])
        finally:
            conn.close()
        self.assertEqual("random", job.rules_file)
        self.assertEqual("hold", job.publication)
        self.assertEqual("octocat", job.submitted_by)

        declined = self.run_cli("decline", "--id", str(self.other_id),
                                "--reason", "not this week")
        self.assertEqual(0, declined.returncode, declined.stderr)
        row = self.row(self.other_id)
        self.assertEqual("declined", row["state"])
        self.assertEqual("not this week", row["decline_reason"])
        self.assertEqual("a grid that loses its corners", row["text"])

        self.assertIn("nothing waiting", self.run_cli("list").stdout)

    def test_a_decided_row_and_a_missing_one_are_refusals(self):
        self.run_cli("release", "--id", str(self.prompt_id))
        again = self.run_cli("release", "--id", str(self.prompt_id))
        self.assertEqual(3, again.returncode)
        self.assertIn("already released", again.stderr)
        self.assertEqual(3, self.run_cli("release", "--id", "9999").returncode)
        self.assertEqual(3, self.run_cli("decline", "--id", "9999").returncode)

    def test_a_rejected_parent_exits_one_and_declines_the_row(self):
        conn = db.connect(self.db_path)
        try:
            job_id = db.enqueue(conn, "a sketch nobody kept", "student-two")
            db.transition(conn, job_id, "executing")
            db.transition(conn, job_id, "gating")
            db.transition(conn, job_id, "held")
            entry_id = db.create_entry(conn, job_id, "held",
                                       prompt="a sketch nobody kept")
            db.entry_transition(conn, entry_id, "rejected",
                                reject_reason="not this one")
            submission_id = db.add_submission(
                conn, remote_id=3, kind="critique", username="octocat",
                entry_id=entry_id, text="try it again with fewer lines",
                created_utc="2026-09-16T15:06:00Z",
            )
        finally:
            conn.close()
        result = self.run_cli("release", "--id", str(submission_id))
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("declined", result.stderr)
        self.assertEqual("declined", self.row(submission_id)["state"])

    def test_help_works(self):
        result = self.run_cli("--help")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("release", result.stdout)


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


class FakeMany:
    """A stand-in for publish.publish_many: no git, and it can be held open.

    It calls ``on_step`` exactly where packet 10 says it will — one ``commit``
    per entry, then ``push``, then ``index`` — and returns something shaped
    like a ``ManyResult``. The two Events are how a test looks at a batch while
    it is running without sleeping through it: ``commit_gate`` stops it with one
    entry mid-commit, ``push_gate`` stops it after the push step has begun.
    """

    def __init__(self, *, refused=None, failed=None, boom=None,
                 commit_gate=None, push_gate=None, index_note=None):
        self.refused = dict(refused or {})
        self.failed = dict(failed or {})
        self.boom = boom
        self.commit_gate = commit_gate
        self.push_gate = push_gate
        self.index_note = index_note
        self.calls = []        # the entry-id lists it was asked to publish
        self.steps = []        # (phase, entry_id), in the order they happened

    def __call__(self, conn, entry_ids, *, on_step=None, **kwargs):
        entry_ids = list(entry_ids)
        self.calls.append(entry_ids)
        for entry_id in entry_ids:
            self._step(on_step, "commit", entry_id)
            if self.commit_gate is not None:
                self.commit_gate.wait(10)
                self.commit_gate = None      # the first entry only
        if self.boom is not None:
            raise self.boom
        self._step(on_step, "push", None)
        if self.push_gate is not None:
            self.push_gate.wait(10)
        self._step(on_step, "index", None)
        published = [
            Committed(entry_id)
            for entry_id in entry_ids
            if entry_id not in self.refused and entry_id not in self.failed
        ]
        return ManyResultish(
            published=published,
            refused={k: v for k, v in self.refused.items() if k in entry_ids},
            failed={k: v for k, v in self.failed.items() if k in entry_ids},
            index_note=self.index_note,
        )

    def _step(self, on_step, phase, entry_id):
        self.steps.append((phase, entry_id))
        if on_step is not None:
            on_step(phase, entry_id, "whatever packet 10 would have said")


class Committed:
    """One row of ManyResult.published: the runner reads ``entry_id``."""

    def __init__(self, entry_id):
        self.entry_id = entry_id
        self.commit = "0" * 40


class ManyResultish:
    def __init__(self, published, refused, failed, index_note=None):
        self.published = published
        self.refused = refused
        self.failed = failed
        self.index_commit = None
        self.index_note = index_note


class BatchFixtures:
    """Entries, presses and a way to look at a batch while it is running.

    No git anywhere: ``publish.publish_many`` is patched with :class:`FakeMany`,
    which can be held open with a :class:`threading.Event` so that a test reads
    a running batch without sleeping through one. Shared by the runner's tests
    (packet 11) and the page's (packet 12), which need exactly the same
    scaffolding and must agree about what a batch looks like.
    """

    # -- fixtures ----------------------------------------------------------

    def setUp(self):
        self.server.app.batch = None

    def tearDown(self):
        # The tray is emptied between tests and the rows are left where they
        # are: a spawned child job points at its parent entry, so deleting the
        # fixtures would mean unpicking a lineage this packet went to some
        # trouble to write. Every test here names its own entries, and the
        # database is this class's own temporary file.
        self.server.app.batch = None

    def held(self, prompt="a sketch waiting for a person", state="held"):
        """One entry in ``state``, with a job behind it in a matching state."""
        conn = self.db()
        try:
            job = db.enqueue(conn, prompt, "student-batch", publication="hold")
            if state == "held":
                db.transition(conn, job, "executing", executor="qwen3-coder:30b")
                db.transition(conn, job, "gating")
                db.transition(conn, job, "held")
            else:
                conn.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (job,))
            entry = db.create_entry(
                conn, job, state, prompt=prompt, submitted_by="student-batch"
            )
            conn.commit()
        finally:
            conn.close()
        return entry

    def state_of(self, entry_id):
        conn = self.db()
        try:
            row = db.get_entry(conn, entry_id)
            return None if row is None else row["state"]
        finally:
            conn.close()

    def children_of(self, entry_id):
        conn = self.db()
        try:
            return [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM jobs WHERE parent_entry_id = ?", (entry_id,)
                )
            ]
        finally:
            conn.close()

    # -- requests ----------------------------------------------------------

    def post_batch(self, fields, path="/held/batch"):
        """A form press: (status, the flash out of the Location header)."""
        status, location = self.post(path, fields)
        flash = ""
        if "flash=" in location:
            flash = urllib.parse.unquote_plus(location.split("flash=", 1)[1])
        return status, location, flash

    def post_batch_json(self, fields, path="/held/batch"):
        """The same press with ``Accept: application/json``: (status, document)."""
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(
                "POST",
                path,
                body=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            conn.close()

    def batch_json(self):
        status, content_type, body = self.get("/api/batch.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        return json.loads(body)["batch"]

    # -- waiting, bounded, on real state and never on a duration -----------

    def until(self, predicate, what, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.005)
        self.fail(f"timed out waiting for {what}")

    def finished(self):
        return self.until(
            lambda: self.server.app.batch
            if self.server.app.batch and self.server.app.batch.state == "done"
            else None,
            "the batch to finish",
        )

    def items_by_entry(self, batch):
        return {(item.entry_id, item.verb): item for item in batch.items}

    # -- patching the publisher -------------------------------------------

    @contextlib.contextmanager
    def publisher(self, many=None, single=None):
        """Install (or remove) ``publish_many`` and ``publish`` for one test."""
        from sketchgen import publish

        before = {
            name: getattr(publish, name, None) for name in ("publish_many", "publish")
        }
        for name, value in (("publish_many", many), ("publish", single)):
            if value is None:
                if hasattr(publish, name):
                    delattr(publish, name)
            else:
                setattr(publish, name, value)
        try:
            yield
        finally:
            for name, value in before.items():
                if value is None:
                    if hasattr(publish, name):
                        delattr(publish, name)
                else:
                    setattr(publish, name, value)


class TestHeldBatch(BatchFixtures, WebTestCase):
    """POST /held/batch: the marks, the run order, the guards and the document.

    The fallback path (§3.3) is exercised by taking ``publish_many`` away and
    patching the single-entry ``publish.publish`` instead. What is under test is
    the runner's bookkeeping and its refusals, which is all packet 11 owns.
    """

    # -- §3.1: every refusal, and nothing started --------------------------

    def assertNothingStarted(self):
        self.assertIsNone(self.server.app.batch)
        self.assertIsNone(self.batch_json())

    def test_a_press_with_nothing_marked_changes_nothing(self):
        status, location, flash = self.post_batch({})
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/held?"), location)
        self.assertEqual(flash, "nothing is marked — nothing changed")
        self.assertTrue(web.is_refusal(flash))
        self.assertNothingStarted()

    def test_a_press_that_only_fills_a_box_marks_nothing(self):
        entry = self.held()
        status, _location, flash = self.post_batch({f"text-{entry}": "typed and left"})
        self.assertEqual(status, 303)
        self.assertEqual(flash, "nothing is marked — nothing changed")
        self.assertNothingStarted()
        self.assertEqual(self.state_of(entry), "held")

    def test_a_critique_with_an_empty_box_is_refused(self):
        entry = self.held()
        status, _location, flash = self.post_batch(
            {f"cri-{entry}": "on", f"text-{entry}": "   "}
        )
        self.assertEqual(status, 303)
        self.assertEqual(
            flash, f"entry {entry}: a critique needs a sentence — nothing changed"
        )
        self.assertNothingStarted()
        self.assertEqual(self.children_of(entry), [])

    def test_a_critique_cannot_join_a_rejection(self):
        entry = self.held()
        status, _location, flash = self.post_batch(
            {
                f"do-{entry}": "reject",
                f"cri-{entry}": "on",
                f"text-{entry}": "make the circles slower",
            }
        )
        self.assertEqual(status, 303)
        self.assertIn(f"entry {entry}:", flash)
        self.assertIn("nothing changed", flash)
        self.assertNothingStarted()
        self.assertEqual(self.state_of(entry), "held")

    def test_a_kept_rejection_cannot_be_rejected_again(self):
        entry = self.held(state="failed-kept")
        status, _location, flash = self.post_batch({f"do-{entry}": "reject"})
        self.assertEqual(status, 303)
        self.assertIn(f"entry {entry} is failed-kept, not held", flash)
        self.assertNothingStarted()
        self.assertEqual(self.state_of(entry), "failed-kept")

    def test_an_unknown_entry_is_refused(self):
        status, _location, flash = self.post_batch({"do-99999": "publish"})
        self.assertEqual(status, 303)
        self.assertEqual(flash, "there is no entry 99999 — nothing changed")
        self.assertNothingStarted()

    def test_a_verb_outside_the_three_is_refused(self):
        entry = self.held()
        status, _location, flash = self.post_batch({f"do-{entry}": "delete"})
        self.assertEqual(status, 303)
        self.assertIn("is not publish, reject or archive", flash)
        self.assertNothingStarted()
        self.assertEqual(self.state_of(entry), "held")

    def test_a_field_that_does_not_name_an_entry_is_refused(self):
        status, _location, flash = self.post_batch({"do-everything": "publish"})
        self.assertEqual(status, 303)
        self.assertIn("that is not an entry id", flash)
        self.assertNothingStarted()

    def test_a_refusal_for_a_script_is_json_and_400(self):
        entry = self.held()
        status, document = self.post_batch_json({f"cri-{entry}": "on"})
        self.assertEqual(status, 400)
        self.assertEqual(
            document["error"],
            f"entry {entry}: a critique needs a sentence — nothing changed",
        )
        self.assertNothingStarted()

    # -- §1.6: the order is the plan's, not the form's ---------------------

    def test_the_run_order_is_the_plans_and_not_the_forms(self):
        publish_id = self.held()
        archive_id = self.held()
        critique_id = self.held()
        reject_id = self.held()
        # Marked in the worst order the form could send them in.
        form = {
            f"do-{publish_id}": ["publish"],
            f"cri-{critique_id}": ["on"],
            f"text-{critique_id}": ["make the circles slower"],
            f"do-{reject_id}": ["reject"],
            f"do-{archive_id}": ["archive"],
        }
        conn = self.db()
        try:
            items = web.batch_plan(conn, form)
        finally:
            conn.close()
        self.assertEqual(
            [(item.verb, item.entry_id) for item in items],
            [
                ("critique", critique_id),
                ("archive", archive_id),
                ("reject", reject_id),
                ("publish", publish_id),
            ],
        )

    def test_a_batch_runs_the_phases_in_order_and_ends_done(self):
        critique_id = self.held()
        archive_id = self.held()
        reject_id = self.held()
        publish_id = self.held()
        many = FakeMany()
        with self.publisher(many=many):
            status, _location, flash = self.post_batch(
                {
                    f"do-{publish_id}": "publish",
                    f"do-{reject_id}": "reject",
                    f"do-{archive_id}": "archive",
                    f"cri-{critique_id}": "on",
                    f"text-{critique_id}": "make the circles slower",
                }
            )
            self.assertEqual((status, flash), (303, ""))
            batch = self.finished()
        # rejections first, then publications, in one call, with one push
        self.assertEqual(many.calls, [[reject_id, publish_id]])
        self.assertEqual(
            many.steps,
            [("commit", reject_id), ("commit", publish_id),
             ("push", None), ("index", None)],
        )
        items = self.items_by_entry(batch)
        self.assertEqual([item.verb for item in batch.items],
                         ["critique", "archive", "reject", "publish"])
        for key, item in items.items():
            self.assertEqual(item.state, "done", key)
        # the state the runner owns: the flip, the archive, the child
        self.assertEqual(self.state_of(reject_id), "rejected")
        self.assertEqual(self.state_of(archive_id), "archived")
        self.assertEqual(len(self.children_of(critique_id)), 1)
        # 1 critique + 1 archive + 2 commits + 1 push + 1 index
        self.assertEqual(batch.steps, 6)
        self.assertEqual(batch.step, 6)
        self.assertEqual(batch.state, "done")
        self.assertTrue(batch.ended_utc)
        self.assertRegex(batch.now, r"^4 done in \d")

    def test_a_publish_and_critique_card_spawns_while_the_parent_is_held(self):
        entry = self.held()
        gate = threading.Event()
        many = FakeMany(commit_gate=gate)
        with self.publisher(many=many):
            status, _document = self.post_batch_json(
                {f"do-{entry}": "publish", f"cri-{entry}": "on",
                 f"text-{entry}": "make the circles slower"}
            )
            self.assertEqual(status, 202)
            # Held inside the commit: the child is already queued and the
            # parent has not been published out from under it.
            self.until(lambda: self.children_of(entry), "the child to be queued")
            self.assertEqual(self.state_of(entry), "held")
            gate.set()
            batch = self.finished()
        items = self.items_by_entry(batch)
        self.assertEqual(items[(entry, "critique")].state, "done")
        self.assertIn("Queued as #", items[(entry, "critique")].message)
        self.assertEqual(items[(entry, "publish")].state, "done")

    # -- §1.5: one batch at a time, and the guards -------------------------

    def test_a_second_press_during_a_run_is_refused_and_the_first_finishes(self):
        first = self.held()
        second = self.held()
        gate = threading.Event()
        many = FakeMany(commit_gate=gate)
        with self.publisher(many=many):
            status, _document = self.post_batch_json({f"do-{first}": "publish"})
            self.assertEqual(status, 202)
            status, location, flash = self.post_batch({f"do-{second}": "publish"})
            self.assertEqual(status, 303)
            self.assertEqual(flash, "a batch is already running — nothing changed")
            status, document = self.post_batch_json({f"do-{second}": "publish"})
            self.assertEqual(status, 409)
            self.assertEqual(
                document["error"], "a batch is already running — nothing changed"
            )
            gate.set()
            batch = self.finished()
        self.assertEqual([item.entry_id for item in batch.items], [first])
        self.assertEqual(many.calls, [[first]])

    def test_the_three_gallery_routes_refuse_while_a_batch_runs(self):
        marked = self.held()
        other = self.held()
        gate = threading.Event()
        many = FakeMany(commit_gate=gate)
        guarded = [
            f"/held/{other}/publish",
            f"/held/{other}/reject",
            f"/held/{other}/archive",
        ]
        with self.publisher(many=many):
            status, _document = self.post_batch_json({f"do-{marked}": "publish"})
            self.assertEqual(status, 202)
            for path in guarded:
                with self.subTest(path=path):
                    status, location, flash = self.post_batch({"text": "no"}, path=path)
                    self.assertEqual(status, 303)
                    self.assertEqual(
                        flash,
                        "a batch is running — nothing changed; it will finish first",
                    )
            # nothing the guarded routes were asked to do has happened
            self.assertEqual(self.state_of(other), "held")
            gate.set()
            self.finished()
        # …and the same press works once the batch is over
        status, location, flash = self.post_batch({}, path=f"/held/{other}/archive")
        self.assertEqual(status, 303)
        self.assertIn("archived", flash)
        self.assertEqual(self.state_of(other), "archived")

    def test_spawning_is_not_guarded(self):
        marked = self.held()
        other = self.held()
        gate = threading.Event()
        many = FakeMany(commit_gate=gate)
        with self.publisher(many=many):
            status, _document = self.post_batch_json({f"do-{marked}": "publish"})
            self.assertEqual(status, 202)
            status, _location, flash = self.post_batch(
                {"text": "make the circles slower"}, path=f"/entry/{other}/spawn"
            )
            self.assertEqual(status, 303)
            self.assertIn("Queued as #", flash)
            gate.set()
            self.finished()
        self.assertEqual(len(self.children_of(other)), 1)

    # -- §3.4: the document ------------------------------------------------

    def test_the_json_is_the_document_the_tray_reads(self):
        critique_id = self.held()
        archive_id = self.held()
        publish_id = self.held()
        gate = threading.Event()
        many = FakeMany(commit_gate=gate)
        with self.publisher(many=many):
            status, posted = self.post_batch_json(
                {
                    f"cri-{critique_id}": "on",
                    f"text-{critique_id}": "make the circles slower",
                    f"do-{archive_id}": "archive",
                    f"do-{publish_id}": "publish",
                }
            )
            self.assertEqual(status, 202)
            self.assertIn("batch", posted)
            document = self.until(
                lambda: (self.batch_json() or {}).get("phase") == "commit"
                and self.batch_json(),
                "the commit phase",
            )
            self.assertEqual(
                sorted(document),
                sorted(
                    [
                        "id", "state", "phase", "now", "step", "steps", "bar_pct",
                        "elapsed_s", "phases", "items", "summary",
                    ]
                ),
            )
            self.assertEqual(document["id"], self.server.app.batch.id)
            self.assertEqual(document["state"], "running")
            self.assertEqual(
                document["now"],
                f"Entry {publish_id} — rendering, scanning, "
                f"committing e/{publish_id}/",
            )
            # 1 critique + 1 archive + 1 commit + 1 push + 1 index, two done
            self.assertEqual(document["steps"], 5)
            self.assertEqual(document["step"], 2)
            self.assertEqual(document["bar_pct"], 40.0)
            self.assertIsInstance(document["elapsed_s"], int)
            self.assertIsNone(document["summary"])
            self.assertEqual(
                document["phases"],
                [
                    {"key": "critique", "label": "Critiques", "done": 1, "of": 1},
                    {"key": "archive", "label": "Archives", "done": 1, "of": 1},
                    {"key": "commit", "label": "Render & commit", "done": 0, "of": 1},
                    {"key": "push", "label": "Push", "done": 0, "of": 1},
                    {"key": "index", "label": "Index", "done": 0, "of": 1},
                ],
            )
            self.assertEqual(
                document["items"][0],
                {
                    "entry_id": critique_id,
                    "verb": "critique",
                    "state": "done",
                    "message": self.items_by_entry(self.server.app.batch)[
                        (critique_id, "critique")
                    ].message,
                },
            )
            self.assertEqual(
                [item["state"] for item in document["items"]],
                ["done", "done", "working"],
            )
            gate.set()
            self.finished()
        done = self.batch_json()
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["summary"], done["now"])
        self.assertRegex(done["summary"], r"^3 done in \d")
        self.assertEqual(done["bar_pct"], 100.0)

    def test_a_phase_with_nothing_in_it_is_not_in_the_document(self):
        archive_id = self.held()
        many = FakeMany()
        with self.publisher(many=many):
            status, _document = self.post_batch_json({f"do-{archive_id}": "archive"})
            self.assertEqual(status, 202)
            self.finished()
        document = self.batch_json()
        self.assertEqual([phase["key"] for phase in document["phases"]], ["archive"])
        self.assertEqual(document["steps"], 1)
        self.assertEqual(many.calls, [])  # no commit, so no trip to the gallery

    def test_an_item_between_its_commit_and_the_push_is_back_in_the_queue(self):
        publish_id = self.held()
        gate = threading.Event()
        many = FakeMany(push_gate=gate)
        with self.publisher(many=many):
            status, _document = self.post_batch_json({f"do-{publish_id}": "publish"})
            self.assertEqual(status, 202)
            document = self.until(
                lambda: (self.batch_json() or {}).get("phase") == "push"
                and self.batch_json(),
                "the push phase",
            )
            self.assertEqual(document["now"], "Pushing 1 commits to the gallery")
            self.assertEqual(document["items"][0]["state"], "queued")
            self.assertEqual(
                document["items"][0]["message"], "committed, waiting for the push"
            )
            gate.set()
            self.finished()
        self.assertEqual(self.batch_json()["items"][0]["state"], "done")

    def test_dismiss_refuses_a_running_batch_and_clears_a_finished_one(self):
        publish_id = self.held()
        gate = threading.Event()
        many = FakeMany(commit_gate=gate)
        with self.publisher(many=many):
            status, _document = self.post_batch_json({f"do-{publish_id}": "publish"})
            self.assertEqual(status, 202)
            status, location, flash = self.post_batch({}, path="/held/batch/dismiss")
            self.assertEqual(status, 303)
            self.assertEqual(
                flash, "a batch is running — nothing changed; it will finish first"
            )
            self.assertIsNotNone(self.batch_json())
            gate.set()
            self.finished()
        status, location, flash = self.post_batch({}, path="/held/batch/dismiss")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/held")
        self.assertIsNone(self.batch_json())
        self.assertIsNone(self.server.app.batch)

    # -- the outcomes a batch reports on its items -------------------------

    def test_one_refused_entry_is_reported_on_its_item_and_the_batch_goes_on(self):
        refused_id = self.held()
        publish_id = self.held()
        many = FakeMany(refused={refused_id: "personal data in sketch.js"})
        with self.publisher(many=many):
            self.assertEqual(
                self.post_batch_json(
                    {f"do-{refused_id}": "publish", f"do-{publish_id}": "publish"}
                )[0],
                202,
            )
            batch = self.finished()
        items = self.items_by_entry(batch)
        self.assertEqual(items[(refused_id, "publish")].state, "refused")
        self.assertEqual(
            items[(refused_id, "publish")].message, "personal data in sketch.js"
        )
        self.assertEqual(items[(publish_id, "publish")].state, "done")
        self.assertRegex(batch.now, r"^1 done · 1 refused in \d")

    def test_a_push_that_fails_leaves_every_publication_failed(self):
        first = self.held()
        second = self.held()
        broke = "! [remote rejected]"
        many = FakeMany(failed={first: broke, second: broke})
        with self.publisher(many=many):
            self.assertEqual(
                self.post_batch_json(
                    {f"do-{first}": "publish", f"do-{second}": "publish"}
                )[0],
                202,
            )
            batch = self.finished()
        for item in batch.items:
            self.assertEqual(item.state, "failed")
            self.assertEqual(item.message, broke)
        self.assertRegex(batch.now, r"^0 done · 2 failed in \d")

    def test_a_checkout_refusal_is_every_entrys_refusal(self):
        from sketchgen import publish

        first = self.held()
        second = self.held()
        many = FakeMany(boom=publish.PublishRefused("gallery checkout is dirty"))
        with self.publisher(many=many):
            self.assertEqual(
                self.post_batch_json(
                    {f"do-{first}": "publish", f"do-{second}": "publish"}
                )[0],
                202,
            )
            batch = self.finished()
        for item in batch.items:
            self.assertEqual(item.state, "refused")
            self.assertEqual(item.message, "refused: gallery checkout is dirty")
        self.assertEqual(batch.state, "done")

    def test_an_entry_whose_state_moved_is_its_own_items_refusal(self):
        # Marked Archive on the page, archived by something else before the
        # press lands: not a validation failure, just that item's own refusal.
        entry = self.held()
        conn = self.db()
        try:
            db.archive_entry(conn, entry)
        finally:
            conn.close()
        many = FakeMany()
        with self.publisher(many=many):
            self.assertEqual(
                self.post_batch_json({f"do-{entry}": "archive"})[0], 202
            )
            batch = self.finished()
        self.assertEqual(batch.items[0].state, "refused")
        self.assertIn("refused:", batch.items[0].message)
        self.assertEqual(batch.state, "done")

    def test_an_exception_inside_the_thread_still_ends_the_batch(self):
        critique_id = self.held()
        publish_id = self.held()

        def boom(conn, entry_id, form):
            raise RuntimeError("the runner broke")

        before = web.spawn_child
        web.spawn_child = boom
        many = FakeMany()
        try:
            with self.publisher(many=many):
                self.assertEqual(
                    self.post_batch_json(
                        {
                            f"cri-{critique_id}": "on",
                            f"text-{critique_id}": "make the circles slower",
                            f"do-{publish_id}": "publish",
                        }
                    )[0],
                    202,
                )
                batch = self.finished()
        finally:
            web.spawn_child = before
        self.assertEqual(batch.state, "done")
        self.assertTrue(batch.ended_utc)
        for item in batch.items:
            self.assertEqual(item.state, "failed")
            self.assertEqual(item.message, "the runner broke")
        self.assertEqual(many.calls, [])  # it never reached the gallery
        self.assertRegex(batch.now, r"^0 done · 2 failed in \d")

    # -- §3.3: the same batch without packet 10 ----------------------------

    def test_without_publish_many_the_runner_publishes_one_at_a_time(self):
        reject_id = self.held()
        publish_id = self.held()
        calls = []

        def single(conn, entry_id, **kwargs):
            calls.append(entry_id)
            return Committed(entry_id)

        with self.publisher(many=None, single=single):
            self.assertEqual(
                self.post_batch_json(
                    {f"do-{reject_id}": "reject", f"text-{reject_id}": "too static",
                     f"do-{publish_id}": "publish"}
                )[0],
                202,
            )
            batch = self.finished()
        # one commit step each, no push step and no index step to show
        self.assertEqual(calls, [reject_id, publish_id])
        self.assertEqual(batch.steps, 2)
        self.assertEqual(batch.step, 2)
        self.assertEqual(
            [phase["key"] for phase in self.batch_json()["phases"]], ["commit"]
        )
        items = self.items_by_entry(batch)
        self.assertEqual(items[(reject_id, "reject")].state, "done")
        self.assertIn("too static", items[(reject_id, "reject")].message)
        self.assertEqual(items[(publish_id, "publish")].state, "done")
        self.assertEqual(self.state_of(reject_id), "rejected")

    def test_without_a_publisher_at_all_every_commit_is_refused(self):
        publish_id = self.held()
        with self.publisher(many=None, single=None):
            self.assertEqual(
                self.post_batch_json({f"do-{publish_id}": "publish"})[0], 202
            )
            batch = self.finished()
        self.assertEqual(batch.items[0].state, "refused")
        self.assertIn("publisher not installed", batch.items[0].message)
        self.assertEqual(self.state_of(publish_id), "held")


class TestHeldBatchPage(BatchFixtures, WebTestCase):
    """The Held page as a batch: one form, four marks, and the tray (packet 12).

    The card's four ``formaction``s are gone and so is the card's form. What is
    under test here is what the server renders — the controls bound to the one
    page-wide form, and the tray in each of its three states — because that is
    what a browser with no JavaScript gets, and the script only makes it live.
    """

    def card(self, page, entry_id):
        """Inside this entry's <section>, and nothing else on the page."""
        needle = f'id="entry-{entry_id}"'
        self.assertIn(needle, page)
        inside = page.split(needle, 1)[1].split(">", 1)[1]
        return inside.split("</section>", 1)[0]

    def tray(self, page):
        self.assertIn('<section class="tray"', page)
        return page.split('<section class="tray"', 1)[1].split("</section>", 1)[0]

    # -- §4.1: the card ----------------------------------------------------

    def test_the_page_has_one_form_and_the_cards_have_none(self):
        entry = self.held()
        page = self.text("/held")
        self.assertEqual(1, page.count('<form id="held-batch"'))
        self.assertEqual(1, page.count('action="/held/batch"'))
        self.assertNotIn("<form method=", self.card(page, entry))
        # Nothing on this page acts on one entry any more: the marks are marks,
        # and Process is the only request the page can make.
        self.assertNotIn("formaction", page)
        self.assertNotIn(f'action="/held/{entry}/publish"', page)
        self.assertNotIn(f'action="/entry/{entry}/spawn"', page)

    def test_a_held_card_carries_three_radios_and_a_checkbox(self):
        entry = self.held()
        card = self.card(self.text("/held"), entry)
        self.assertEqual(3, card.count('type="radio"'))
        self.assertEqual(1, card.count('type="checkbox"'))
        self.assertEqual(5, card.count('form="held-batch"'))
        for value in ("publish", "reject", "archive"):
            self.assertIn(f'name="do-{entry}" value="{value}"', card)
        self.assertIn(f'name="cri-{entry}" value="on"', card)
        self.assertIn(f'name="text-{entry}"', card)

    def test_a_kept_card_carries_two_radios_and_a_checkbox(self):
        entry = self.held(prompt="one the gate refused", state="failed-kept")
        card = self.card(self.text("/held"), entry)
        self.assertEqual(2, card.count('type="radio"'))
        self.assertEqual(1, card.count('type="checkbox"'))
        self.assertNotIn('value="reject"', card)

    def test_the_single_entry_routes_are_still_in_the_table(self):
        # Nothing on the page points at them; scripts, the CLI-minded and
        # /entry/<id> habits still do, so they stay.
        paths = [route[1].pattern for route in web.ROUTES]
        for pattern in (
            r"^/held/(?P<entry_id>\d+)/publish$",
            r"^/held/(?P<entry_id>\d+)/reject$",
            r"^/held/(?P<entry_id>\d+)/archive$",
            r"^/entry/(?P<entry_id>\d+)/spawn$",
        ):
            self.assertIn(pattern, paths)

    # -- §4.2: the tray, in three states -----------------------------------

    def test_with_no_batch_the_tray_is_the_title_and_the_button(self):
        page = self.text("/held")
        tray = self.tray(page)
        self.assertIn("<h1>Held ", tray)
        self.assertIn("waiting · ", tray)
        self.assertIn('<div class="tally" id="tally" aria-live="polite"></div>', tray)
        self.assertIn('id="process"', tray)
        self.assertNotIn("Processing…", tray)
        self.assertNotIn("disabled", tray.split('id="tally"', 1)[1])
        self.assertIn('id="prog-row" hidden', tray)
        # Hidden, but present: the script fills these nodes on every poll and
        # never builds them, and the press that starts a batch happens here.
        for node in ('id="now"', 'id="bar-fill"', 'id="prog-t"'):
            self.assertIn(node, tray)
        self.assertIn('id="results" hidden', tray)
        self.assertNotIn("http-equiv", tray)
        # the page's own first heading went with it: the tray is the title now
        self.assertEqual(1, page.count("<h1>Held "))
        self.assertNotIn("Held for publication</h1>", page)

    def test_the_layout_hides_what_it_marks_hidden_and_the_stop_pill_is_clickable(self):
        page = self.text("/held")
        # .tray-row sets display itself, which beats the browser's [hidden] rule
        self.assertIn(".tray-row[hidden], .phases[hidden], .results[hidden] { display: none; }", page)
        # the label floats over the iframe; if it ignores the pointer, the
        # click falls through to the sandboxed frame and stop never fires
        label = page.split(".preview-run .play-label {", 1)[1].split("}", 1)[0]
        self.assertNotIn("pointer-events: none", label)

    def test_while_a_batch_runs_the_page_is_locked_and_says_so(self):
        marked = self.held()
        gate = threading.Event()
        many = FakeMany(commit_gate=gate)
        with self.publisher(many=many):
            self.assertEqual(
                self.post_batch_json({f"do-{marked}": "publish"})[0], 202
            )
            self.until(
                lambda: (self.batch_json() or {}).get("phase") == "commit",
                "the commit phase",
            )
            page = self.text("/held")
            tray = self.tray(page)
            self.assertIn("Processing…", tray)
            self.assertIn('id="process" disabled', tray)
            self.assertIn('<noscript><meta http-equiv="refresh" content="2">', tray)
            self.assertIn("0 of 3 steps · ", tray)
            self.assertIn("Render &amp; commit 0/1", tray)
            self.assertIn(f"Entry {marked} — rendering", tray)
            # every control on every card, not only the marked one
            card = self.card(page, marked)
            self.assertEqual(5, card.count(" disabled>"))
            self.assertIn('<span class="pill st warn">working</span>', card)
            self.assertIn("is-working", page.split(f'id="entry-{marked}"', 1)[0][-90:])
            other = self.card(page, self.entry_id)
            self.assertEqual(5, other.count(" disabled>"))
            self.assertNotIn("pill st", other)
            gate.set()
            self.finished()

    def test_a_finished_batch_leaves_its_result_and_pre_marks_the_refusals(self):
        refused = self.held()
        done = self.held()
        many = FakeMany(
            refused={refused: "refused: sketch.js holds an email address"},
            index_note="the index push was refused; the entries are public",
        )
        with self.publisher(many=many):
            self.assertEqual(
                self.post_batch_json(
                    {
                        f"do-{refused}": "publish",
                        f"text-{refused}": "one that will not go",
                        f"do-{done}": "publish",
                    }
                )[0],
                202,
            )
            self.finished()
        page = self.text("/held")
        tray = self.tray(page)
        self.assertRegex(tray, r"1 done · 1 refused in \d")
        self.assertIn('class="bar done"', tray)
        # the refusal is the first row, and the note the index left is there
        self.assertLess(
            tray.index("sketch.js holds an email address"),
            tray.index(f'<span class="n">{done}</span>'),
        )
        self.assertIn("the index push was refused", tray)
        self.assertIn('form="held-dismiss"', tray)
        self.assertIn('action="/held/batch/dismiss"', page)
        # the refused card is still there, still marked, with its sentence
        card = self.card(page, refused)
        self.assertIn('value="publish"', card)
        self.assertEqual(1, card.count(" checked>"))
        self.assertIn('value="one that will not go"', card)
        self.assertIn('class="hint decide-hint bad" data-reason="1"', card)
        self.assertIn("still held, still marked", card)
        self.assertIn('<span class="pill st bad">refused</span>', card)
        # nothing is disabled: the batch is over and the retry is one press
        self.assertNotIn(" disabled", card)
        self.assertNotIn("Processing…", tray)

    def test_the_tray_is_on_held_and_nowhere_else(self):
        for path in ("/", "/queue", "/new", "/submissions", f"/job/{self.held_id}",
                     f"/entry/{self.entry_id}"):
            with self.subTest(path=path):
                page = self.text(path)
                self.assertNotIn('class="tray"', page)
                self.assertNotIn('id="held-batch"', page)

    def test_both_header_rows_are_sticky_on_every_page(self):
        # The sticky box is the wrapper, not header.top, so the tray sticks
        # with the nav rather than under it.
        for path in ("/", "/queue", "/new", "/held", "/submissions",
                     f"/job/{self.held_id}"):
            with self.subTest(path=path):
                page = self.text(path)
                self.assertIn('<div class="sticky">', page)
                self.assertIn(".sticky { position: sticky;", page)
                header = page.split("header.top {", 1)[1].split("}", 1)[0]
                self.assertNotIn("position: sticky", header)

    # -- §4.3: the script, as text -----------------------------------------

    def test_the_script_is_on_the_held_page_and_writes_no_html(self):
        page = self.text("/held")
        self.assertIn("held-marks", page)
        self.assertIn("/api/batch.json", page)
        self.assertNotIn(".innerHTML", web.HELD_SCRIPT)
        self.assertIn('"Accept": "application/json"', web.HELD_SCRIPT)
        # ES5-plain, the same house rule as the layout's own script: no arrow
        # functions, no template literals, and every variable a var.
        self.assertNotIn("=>", web.HELD_SCRIPT)
        self.assertNotIn("`", web.HELD_SCRIPT)
        for line in web.HELD_SCRIPT.splitlines():
            code = line.split("//", 1)[0]
            self.assertNotRegex(code, r"\b(const|let|class)\s")


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


# ---------------------------------------------------------------------------
# The planner menu — the models this node has, rather than "local" or "paid"
# ---------------------------------------------------------------------------

#: What the node answered on 2026-09-19, trimmed to the shapes that matter:
#: a vision model, the coder (no vision), a proxied cloud model, and an
#: embedding model that can do neither.
CATALOGUE = [
    models.Model(name="gemma4:e4b",
                 capabilities=frozenset({"completion", "vision", "audio",
                                         "tools", "thinking"}),
                 size_bytes=9608350718, parameter_size="8.0B", family="gemma4"),
    models.Model(name="gemma4:26b",
                 capabilities=frozenset({"completion", "vision", "tools"}),
                 size_bytes=18604148513, parameter_size="25.2B", family="gemma4"),
    models.Model(name="qwen3.5:9b",
                 capabilities=frozenset({"completion", "vision", "tools"}),
                 size_bytes=6594474711, parameter_size="9.7B", family="qwen35"),
    models.Model(name="qwen3-coder:30b-a3b-q4_K_M",
                 capabilities=frozenset({"completion", "tools"}),
                 size_bytes=18556700761, parameter_size="30.5B"),
    models.Model(name="nomic-embed-text:latest",
                 capabilities=frozenset({"embedding"}), size_bytes=274302450),
    models.Model(name="gemma4:31b-cloud",
                 capabilities=frozenset({"completion", "vision", "tools"}),
                 size_bytes=312, parameter_size="32.7B", remote=True),
]


class PlannerMenuTestCase(WebTestCase):
    """A fixed catalogue, so the menu is the same on a node and on a laptop."""

    catalogue = CATALOGUE

    def setUp(self):
        patcher = mock.patch.object(
            web.models, "catalogue", lambda *a, **k: list(self.catalogue)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.signed_in_as("profcarroll")
        self.addCleanup(self.sign_out)
        conn = self.db()
        try:
            db.set_meta(conn, web.DEFAULTS_KEY, None)
        finally:
            conn.close()

    def sign_out(self):
        web._gh_cache.clear()
        web._gh_cache.update(login=None, asked=float("inf"))

    def signed_in_as(self, login):
        web._gh_cache.clear()
        web._gh_cache.update(login=login, asked=float("inf"))

    def queued_ids(self):
        conn = self.db()
        try:
            return {job.id for job in db.list_jobs(conn, "queued")}
        finally:
            conn.close()

    def newest_planner(self, before):
        conn = self.db()
        try:
            new = {job.id for job in db.list_jobs(conn, "queued")} - before
            self.assertEqual(1, len(new))
            return db.get_job(conn, new.pop()).planner
        finally:
            conn.close()

    def post_page(self, path, fields):
        data = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        request = urllib.request.Request(self.url(path), data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read().decode("utf-8")


class TestPlannerMenu(PlannerMenuTestCase):
    def test_the_menu_offers_every_vision_capable_model_on_the_node(self):
        page = self.text("/new")
        for tag in ("gemma4:e4b", "gemma4:26b", "qwen3.5:9b"):
            self.assertIn(f'<option value="{tag}"', page)

    def test_a_model_that_cannot_see_is_not_offered_as_a_planner(self):
        page = self.text("/new")
        self.assertNotIn('value="qwen3-coder:30b-a3b-q4_K_M"', page)
        self.assertNotIn('value="nomic-embed-text:latest"', page)

    def test_a_model_that_leaves_the_node_is_grouped_apart_from_the_rest(self):
        page = self.text("/new")
        self.assertIn('<optgroup label="on this node">', page)
        self.assertIn('<optgroup label="off this node">', page)
        here = page.index('<optgroup label="on this node">')
        away = page.index('<optgroup label="off this node">')
        self.assertLess(here, page.index('value="gemma4:e4b"'))
        self.assertLess(page.index('value="gemma4:e4b"'), away)
        # the cloud tag and the paid route are both in the second group, and
        # the cloud one says where it goes
        self.assertLess(away, page.index('value="gemma4:31b-cloud"'))
        self.assertLess(away, page.index('value="paid"'))
        self.assertIn("leaves this node", page)

    def test_the_menu_says_which_one_a_job_gets_by_default(self):
        page = self.text("/new")
        self.assertIn("(default)", page)
        self.assertIn(f'<option value="{worker.DEFAULT_PLANNER_MODEL}" selected>', page)

    def test_a_label_carries_the_size_so_the_choice_has_a_cost(self):
        page = self.text("/new")
        self.assertIn("8.0B", page)
        self.assertIn("8.9 GB", page)

    def test_the_chosen_model_is_what_the_job_is_queued_with(self):
        before = self.queued_ids()
        status, _ = self.post("/new", {
            "prompt": "a lattice that leans toward the pointer",
            "submitted_by": "student-three", "planner": "qwen3.5:9b",
            "rules": "treatment", "publication": "hold", "max_attempts": "2",
        })
        self.assertEqual(303, status)
        self.assertEqual("qwen3.5:9b", self.newest_planner(before))

    def test_paid_still_reaches_the_column_as_paid(self):
        before = self.queued_ids()
        self.post("/new", {"prompt": "a paid plan", "submitted_by": "student-three",
                           "planner": "paid"})
        self.assertEqual("paid", self.newest_planner(before))

    def test_local_still_means_the_workers_default(self):
        """An old bookmark, an old saved default, a form with no planner field."""
        before = self.queued_ids()
        self.post("/new", {"prompt": "an unspecified plan",
                           "submitted_by": "student-three", "planner": "local"})
        self.assertEqual(worker.DEFAULT_PLANNER_MODEL, self.newest_planner(before))

    def test_a_model_this_node_does_not_have_is_refused_without_writing(self):
        before = self.queued_ids()
        status, body = self.post_page("/new", {
            "prompt": "planned by something imaginary",
            "submitted_by": "student-three", "planner": "gpt-9:enormous",
        })
        self.assertEqual(400, status)
        self.assertIn("is not a model this node offers", body)
        self.assertEqual(before, self.queued_ids())

    def test_a_model_that_cannot_see_is_refused_even_though_it_exists(self):
        before = self.queued_ids()
        status, body = self.post_page("/new", {
            "prompt": "planned by the coder",
            "submitted_by": "student-three",
            "planner": "qwen3-coder:30b-a3b-q4_K_M",
        })
        self.assertEqual(400, status)
        self.assertEqual(before, self.queued_ids())

    def test_a_model_can_be_saved_as_the_pages_default(self):
        status, body = self.post_page("/new/defaults", {
            "action": "save", "planner": "gemma4:26b", "rules": "treatment",
            "publication": "hold", "max_attempts": "3",
        })
        self.assertEqual(200, status)
        self.assertIn("Saved as defaults — gemma4:26b", body)
        self.assertIn('<option value="gemma4:26b" selected>', self.text("/new"))
        conn = self.db()
        try:
            db.set_meta(conn, web.DEFAULTS_KEY, None)
        finally:
            conn.close()

    def test_picking_a_parent_preselects_the_model_it_was_planned_with(self):
        conn = self.db()
        try:
            job_id = db.enqueue(conn, "a line with a model", "student-two",
                                planner="qwen3.5:9b")
            db.transition(conn, job_id, "executing", executor="x")
            db.transition(conn, job_id, "gating")
            db.transition(conn, job_id, "held")
            parent = db.create_entry(conn, job_id, "held", prompt="a line with a model",
                                     planner="qwen3.5:9b", submitted_by="student-two")
        finally:
            conn.close()
        page = self.text(f"/new?parent={parent}")
        self.assertIn('<option value="qwen3.5:9b" selected>', page)

    def test_a_parent_planned_with_a_model_since_removed_falls_back(self):
        """The select must never open on nothing, or on a silently other model."""
        conn = self.db()
        try:
            job_id = db.enqueue(conn, "a line with an old model", "student-two",
                                planner="gemma3:gone")
            db.transition(conn, job_id, "executing", executor="x")
            db.transition(conn, job_id, "gating")
            db.transition(conn, job_id, "held")
            parent = db.create_entry(conn, job_id, "held",
                                     prompt="a line with an old model",
                                     planner="gemma3:gone", submitted_by="student-two")
        finally:
            conn.close()
        page = self.text(f"/new?parent={parent}")
        self.assertNotIn('value="gemma3:gone"', page)
        self.assertIn(f'<option value="{worker.DEFAULT_PLANNER_MODEL}" selected>', page)


class TestPlannerMenuWithNoModelHost(PlannerMenuTestCase):
    """Ollama down, or not there at all: the page is the page it always was."""

    catalogue = []

    def test_the_page_still_renders_with_the_workers_default(self):
        page = self.text("/new")
        self.assertIn('<option value="local" selected>', page)
        self.assertIn(worker.DEFAULT_PLANNER_MODEL, page)
        self.assertIn("the model host did not answer", page)
        self.assertIn('<option value="paid"', page)

    def test_a_job_can_still_be_queued(self):
        before = self.queued_ids()
        status, _ = self.post("/new", {"prompt": "a plan with no menu",
                                       "submitted_by": "student-three",
                                       "planner": "local"})
        self.assertEqual(303, status)
        self.assertEqual(worker.DEFAULT_PLANNER_MODEL, self.newest_planner(before))

    def test_a_tag_from_a_page_rendered_while_the_host_was_up_is_taken(self):
        """The host blinked between the render and the press: the worker, not
        this form, is the place that finds out the model is gone."""
        before = self.queued_ids()
        status, _ = self.post("/new", {"prompt": "a plan chosen a minute ago",
                                       "submitted_by": "student-three",
                                       "planner": "qwen3.5:9b"})
        self.assertEqual(303, status)
        self.assertEqual("qwen3.5:9b", self.newest_planner(before))

    def test_junk_is_still_refused(self):
        before = self.queued_ids()
        status, _ = self.post_page("/new", {"prompt": "a plan by nonsense",
                                            "submitted_by": "student-three",
                                            "planner": "../../etc/passwd"})
        self.assertEqual(400, status)
        self.assertEqual(before, self.queued_ids())


if __name__ == "__main__":
    unittest.main()
