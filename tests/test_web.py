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
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
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

    def test_spawning_from_a_held_entry_changes_nothing(self):
        conn = self.db()
        try:
            before = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE parent_entry_id = ?",
                (self.entry_id,),
            ).fetchone()["c"]
        finally:
            conn.close()
        status, location = self.post(
            f"/entry/{self.entry_id}/spawn",
            {"critique": "slower and warmer", "critique_by": "profcarroll"},
        )
        self.assertEqual(303, status)
        self.assertIn("nothing", location)
        conn = self.db()
        try:
            after = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE parent_entry_id = ?",
                (self.entry_id,),
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(before, after)

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
