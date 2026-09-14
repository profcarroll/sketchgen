"""Unit tests for `sketchgen sync` and sketchgen/sync.py.

A stdlib http.server on 127.0.0.1, port 0, serves one canned /pull payload for
the duration of each test and is shut down in tearDown. It is bound to the
loopback interface and is gone before the test method returns; nothing in this
repository listens on anything else.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sketchgen import db  # noqa: E402
from sketchgen import sync  # noqa: E402

CLI = REPO_ROOT / "bin" / "sketchgen"
TOKEN = "test-pull-token"

# The payload the Worker would send. It deliberately carries three things the
# node must not keep — an email address, a display name and an avatar URL — so
# that "username only" is tested against a payload that offers more.
PAYLOAD = {
    "votes": [
        {
            "username": "octocat", "entry_a": 1, "entry_b": 2,
            "question": "brief", "choice": "A",
            "created_utc": "2026-09-14T00:00:01Z", "updated_utc": "2026-09-14T00:00:01Z",
            "email": "octocat@users.noreply.github.invalid",
            "name": "The Octocat",
        },
        {
            "username": "hubot", "entry_a": 1, "entry_b": 2,
            "question": "look", "choice": "tie",
            "created_utc": "2026-09-14T00:00:09Z", "updated_utc": "2026-09-14T00:00:09Z",
            "avatar_url": "https://avatars.example.invalid/u/1",
        },
        {   # an entry this node has never published: skipped, never written
            "username": "octocat", "entry_a": 1, "entry_b": 99,
            "question": "brief", "choice": "B",
            "created_utc": "2026-09-14T00:00:02Z", "updated_utc": "2026-09-14T00:00:02Z",
        },
    ],
    "likes": [
        {
            "entry_id": 1, "username": "octocat", "active": 1,
            "created_utc": "2026-09-14T00:00:05Z", "updated_utc": "2026-09-14T00:00:05Z",
        },
        {   # taken back before the node ever saw it: nothing to delete, no error
            "entry_id": 2, "username": "hubot", "active": 0,
            "created_utc": "2026-09-14T00:00:04Z", "updated_utc": "2026-09-14T00:00:06Z",
        },
    ],
    "views": [
        {"entry_id": 1, "count": 41, "updated_utc": "2026-09-14T00:00:07Z"},
        {"entry_id": 2, "count": 3, "updated_utc": "2026-09-14T00:00:03Z"},
    ],
}

LAST_STAMP = "2026-09-14T00:00:09Z"


class _Handler(BaseHTTPRequestHandler):
    """Serves /pull the way worker.js does: bearer required, window inclusive."""

    server_version = "canned-writepath/1"

    def log_message(self, *args):  # keep the test output clean
        pass

    def _json(self, status, body):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlparse(self.path)
        header = self.headers.get("Authorization", "")
        self.server.requests.append(
            {"path": parsed.path, "query": parse_qs(parsed.query), "auth": header}
        )
        if parsed.path != "/pull":
            self._json(404, {"error": "not found"})
            return
        if header != f"Bearer {self.server.token}":
            self._json(401, {"error": "pull requires the bearer token"})
            return
        since = (parse_qs(parsed.query).get("since") or ["1970-01-01T00:00:00Z"])[0]
        window = {
            key: [row for row in PAYLOAD[key] if row["updated_utc"] >= since]
            for key in ("votes", "likes", "views")
        }
        watermark = since
        for rows in window.values():
            for row in rows:
                watermark = max(watermark, row["updated_utc"])
        self._json(200, {"since": since, "next_since": watermark, **window})


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        self.db_path = root / "sketchgen.db"
        db.init(self.db_path)
        conn = db.connect(self.db_path)
        try:
            for prompt in ("a field of circles", "a quiet grid"):
                job_id = db.enqueue(conn, prompt, "profcarroll")
                db.create_entry(conn, job_id, state="published", submitted_by="profcarroll")
            ids = [row["id"] for row in conn.execute("SELECT id FROM entries ORDER BY id")]
        finally:
            conn.close()
        self.assertEqual(ids, [1, 2])

        self.token_file = root / "writepath.token"
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        os.chmod(self.token_file, 0o600)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.token = TOKEN
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # -- helpers ----------------------------------------------------------

    def run_sync(self, *extra, token_file=None):
        return subprocess.run(
            [
                sys.executable, str(CLI), "sync",
                "--db", str(self.db_path),
                "--url", self.url,
                "--token-file", str(token_file or self.token_file),
                "--once", "--json", *extra,
            ],
            capture_output=True, text=True, check=False,
        )

    def rows(self, table):
        conn = db.connect(self.db_path)
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        finally:
            conn.close()

    def watermark(self):
        conn = db.connect(self.db_path)
        try:
            return sync.get_since(conn)
        finally:
            conn.close()

    # -- tests ------------------------------------------------------------

    def test_two_runs_are_idempotent_and_move_the_watermark(self):
        self.assertEqual(self.watermark(), sync.EPOCH)

        first = self.run_sync()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_result = json.loads(first.stdout)
        self.assertEqual(first_result["since"], sync.EPOCH)
        self.assertEqual(first_result["next_since"], LAST_STAMP)
        self.assertEqual(first_result["votes"], 2)      # the third names entry 99
        self.assertEqual(first_result["likes"], 1)
        self.assertEqual(first_result["unlikes"], 1)
        self.assertEqual(first_result["views"], 2)
        self.assertEqual(first_result["skipped"], 1)
        self.assertEqual(self.watermark(), LAST_STAMP)

        after_first = {
            "judgments": self.rows("judgments"),
            "likes": self.rows("likes"),
            "engagement": self.rows("engagement"),
        }
        self.assertEqual(len(after_first["judgments"]), 2)
        self.assertEqual(len(after_first["likes"]), 1)
        self.assertEqual(len(after_first["engagement"]), 2)

        second = self.run_sync()
        self.assertEqual(second.returncode, 0, second.stderr)
        second_result = json.loads(second.stdout)
        # The watermark is inclusive, so the boundary row arrives again and is
        # re-applied. Re-applying it must change nothing.
        self.assertEqual(second_result["since"], LAST_STAMP)
        self.assertEqual(second_result["next_since"], LAST_STAMP)
        self.assertEqual(
            {
                "judgments": self.rows("judgments"),
                "likes": self.rows("likes"),
                "engagement": self.rows("engagement"),
            },
            after_first,
        )

        # The second pull asked for the watermark the first one returned.
        pulls = [r for r in self.server.requests if r["path"] == "/pull"]
        self.assertEqual(len(pulls), 2)
        self.assertEqual(pulls[0]["query"]["since"], [sync.EPOCH])
        self.assertEqual(pulls[1]["query"]["since"], [LAST_STAMP])
        self.assertEqual(pulls[0]["auth"], f"Bearer {TOKEN}")

    def test_votes_land_as_human_judgments_under_the_pair_constraint(self):
        self.assertEqual(self.run_sync().returncode, 0)
        judgments = sorted(self.rows("judgments"), key=lambda r: r["judge_id"])
        self.assertEqual([r["judge_id"] for r in judgments], ["hubot", "octocat"])
        for row in judgments:
            self.assertEqual(row["judge_kind"], "human")
            self.assertEqual(row["prompt_version"], "gallery-v1")
            self.assertIsNone(row["artefact_hash"])
        self.assertEqual(
            {(r["entry_a"], r["entry_b"], r["question"], r["choice"]) for r in judgments},
            {(1, 2, "brief", "A"), (1, 2, "look", "tie")},
        )

        # A judge answers each (pair, question) once: a changed choice updates
        # the row the constraint already holds rather than adding another.
        conn = db.connect(self.db_path)
        try:
            sync.apply_changes(
                conn,
                {
                    "next_since": LAST_STAMP,
                    "votes": [
                        {
                            "username": "octocat", "entry_a": 1, "entry_b": 2,
                            "question": "brief", "choice": "tie",
                            "created_utc": "2026-09-14T00:01:00Z",
                            "updated_utc": "2026-09-14T00:01:00Z",
                        }
                    ],
                },
            )
        finally:
            conn.close()
        rows = [r for r in self.rows("judgments") if r["judge_id"] == "octocat"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["choice"], "tie")

    def test_views_never_go_backwards(self):
        self.assertEqual(self.run_sync().returncode, 0)
        self.assertEqual(
            {r["entry_id"]: r["views"] for r in self.rows("engagement")}, {1: 41, 2: 3}
        )
        conn = db.connect(self.db_path)
        try:
            sync.apply_changes(
                conn,
                {
                    "next_since": LAST_STAMP,
                    "views": [{"entry_id": 1, "count": 7, "updated_utc": LAST_STAMP}],
                },
            )
        finally:
            conn.close()
        self.assertEqual(
            {r["entry_id"]: r["views"] for r in self.rows("engagement")}, {1: 41, 2: 3}
        )

    def test_a_username_is_the_only_identity_anything_stores(self):
        self.assertEqual(self.run_sync().returncode, 0)
        login = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")

        identities = [r["judge_id"] for r in self.rows("judgments")]
        identities += [r["username"] for r in self.rows("likes")]
        self.assertTrue(identities)
        for value in identities:
            self.assertRegex(value, login)

        # Nothing the payload offered beyond the login reached the database, in
        # any table: not the email address, not the display name, not the
        # avatar URL, and no '@' anywhere.
        conn = db.connect(self.db_path)
        try:
            tables = [name for name, _ in db.table_counts(conn)]
            dumped = json.dumps({t: self.rows(t) for t in tables}, default=str)
        finally:
            conn.close()
        self.assertNotIn("@", dumped)
        for leak in ("The Octocat", "avatars.example.invalid", "noreply.github"):
            self.assertNotIn(leak, dumped)

        # The schema itself offers nowhere to put one: the only person-shaped
        # columns in the tables sync writes are named for a username.
        conn = db.connect(self.db_path)
        try:
            for table in ("judgments", "likes", "engagement"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertEqual(
                    columns & {"email", "name", "avatar_url", "ip", "real_name"}, set()
                )
        finally:
            conn.close()

    def test_a_loose_or_missing_token_file_is_a_refusal(self):
        missing = Path(self.tmp.name) / "not-there.token"
        result = self.run_sync(token_file=missing)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("token file missing", result.stderr)

        loose = Path(self.tmp.name) / "loose.token"
        loose.write_text(TOKEN, encoding="utf-8")
        os.chmod(loose, 0o644)
        result = self.run_sync(token_file=loose)
        self.assertEqual(result.returncode, 3)
        self.assertIn("0644", result.stderr)

        # Neither refusal reached the server, and neither wrote a row.
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.rows("judgments"), [])

    def test_a_wrong_token_is_a_failure_not_a_refusal(self):
        wrong = Path(self.tmp.name) / "wrong.token"
        wrong.write_text("not-the-token", encoding="utf-8")
        os.chmod(wrong, 0o600)
        result = self.run_sync(token_file=wrong)
        self.assertEqual(result.returncode, 1)
        self.assertIn("HTTP 401", result.stderr)
        self.assertEqual(self.rows("judgments"), [])
        self.assertEqual(self.watermark(), sync.EPOCH)

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "sync", "--help"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--token-file", result.stdout)


if __name__ == "__main__":
    unittest.main()


class UserAgentTests(unittest.TestCase):
    def test_sync_sends_a_named_user_agent(self):
        # Cloudflare answers 403 (error 1010) to Python-urllib's default agent.
        from sketchgen import sync
        self.assertTrue(sync.USER_AGENT.startswith("sketchgen-sync/"))
        import inspect
        self.assertIn('"User-Agent": USER_AGENT', inspect.getsource(sync))

