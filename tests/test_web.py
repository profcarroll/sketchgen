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
import json
import os
import subprocess
import sys
import tempfile
import threading
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
        self.assertIn(f'<pre id="log" data-job="{self.held_id}">', page)
        self.assertIn("line 39", page)  # the tail, not the head
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
