"""Unit tests for sketchgen.db — schema, helpers, and the job state machine.

Run:  python3 -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402

EXPECTED_TABLES = {
    "attempts",
    "control",
    "critiques",
    "engagement",
    "entries",
    "jobs",
    "judgments",
    "likes",
    "lineage",
    "meta",
    "schema_version",
    "sync_state",
}

#: Every migration on disk, as (version, filename), in the order migrate()
#: applies them. Derived rather than written out so that adding migrations/
#: NNN_*.sql (003_meta.sql was packet 4.1's) does not mean editing a literal
#: here; what the tests below assert is that init() applied *all of them, once*.
EXPECTED_MIGRATIONS = [(version, path.name) for version, path in db._migration_files()]

TIMESTAMP = "1970-01-01T00:00:00Z"


class DbTestCase(unittest.TestCase):
    """A temp-file database, initialised, with a connection open."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "test.db")
        db.init(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)

    def enqueue(self, prompt="a slow field of dots", submitted_by="octocat", **opts):
        return db.enqueue(self.conn, prompt, submitted_by, **opts)


class TestInit(DbTestCase):
    def test_init_creates_tables_and_records_every_migration(self):
        names = {name for name, _ in db.table_counts(self.conn)}
        self.assertEqual(EXPECTED_TABLES, names)
        self.assertEqual(EXPECTED_MIGRATIONS[-1][0], db.schema_version(self.conn))
        rows = self.conn.execute(
            "SELECT version, name FROM schema_version ORDER BY version"
        ).fetchall()
        self.assertEqual(
            EXPECTED_MIGRATIONS, [(row["version"], row["name"]) for row in rows]
        )
        self.assertEqual((1, "001_init.sql"), EXPECTED_MIGRATIONS[0])

    def test_rerunning_init_is_a_no_op(self):
        job_id = self.enqueue()
        applied = db.init(self.path)
        self.assertEqual([], applied)
        self.assertEqual(EXPECTED_MIGRATIONS[-1][0], db.schema_version(self.conn))
        self.assertEqual(
            len(EXPECTED_MIGRATIONS),
            self.conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"],
        )
        # and it did not disturb what was already there
        self.assertIsNotNone(db.get_job(self.conn, job_id))
        self.assertEqual("running", db.get_control(self.conn).state)


class TestHappyPath(DbTestCase):
    def test_queued_to_published_with_an_attempt_row(self):
        job_id = self.enqueue(submitted_by="octocat")
        self.assertEqual("queued", db.get_job(self.conn, job_id).state)
        for state in ("planning", "executing"):
            db.transition(self.conn, job_id, state)
        db.add_attempt(
            self.conn,
            job_id,
            model="qwen3-coder:30b-a3b-q4_K_M",
            rules_file="treatment",
            started_utc=TIMESTAMP,
            prompt_tokens=2048,
            completion_tokens=3072,
            gate_exit=0,
        )
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "held")
        entry_id = db.create_entry(self.conn, job_id, state="held", attempts=1)
        db.transition(self.conn, job_id, "published")

        job = db.get_job(self.conn, job_id)
        self.assertEqual("published", job.state)
        self.assertNotEqual(job.created_utc, "")
        self.assertTrue(job.updated_utc.endswith("Z"))
        attempts = db.list_attempts(self.conn, job_id)
        self.assertEqual(1, len(attempts))
        self.assertEqual(1, attempts[0].n)
        self.assertEqual(0, attempts[0].gate_exit)
        self.assertEqual(
            "held",
            self.conn.execute(
                "SELECT state FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()["state"],
        )


class TestRepairLoop(DbTestCase):
    def test_two_repairs_then_failed(self):
        job_id = self.enqueue(max_attempts=3)
        db.transition(self.conn, job_id, "executing")
        db.add_attempt(self.conn, job_id, gate_exit=1, evidence="AudioContext suspended")
        db.transition(self.conn, job_id, "gating")
        for _ in range(2):
            db.transition(self.conn, job_id, "repairing")
            db.transition(self.conn, job_id, "executing")
            db.add_attempt(self.conn, job_id, gate_exit=1, evidence="still suspended")
            db.transition(self.conn, job_id, "gating")
        job = db.transition(
            self.conn, job_id, "failed", last_error="gate failed after 3 attempts"
        )
        self.assertEqual("failed", job.state)
        self.assertEqual("gate failed after 3 attempts", job.last_error)
        self.assertEqual([1, 2, 3], [a.n for a in db.list_attempts(self.conn, job_id)])
        self.assertEqual(frozenset(), db.TRANSITIONS["failed"])


class TestNeedsLaptop(DbTestCase):
    def test_round_trip_from_planning(self):
        job_id = self.enqueue()
        db.transition(self.conn, job_id, "planning")
        job = db.transition(self.conn, job_id, "needs-laptop", needs="plan")
        self.assertEqual("needs-laptop", job.state)
        self.assertEqual("plan", job.needs)
        job = db.transition(self.conn, job_id, "planning", needs=None)
        self.assertEqual("planning", job.state)
        self.assertIsNone(job.needs)

    def test_round_trip_from_repairing(self):
        job_id = self.enqueue()
        db.transition(self.conn, job_id, "executing")
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "repairing")
        job = db.transition(self.conn, job_id, "needs-laptop", needs="repair")
        self.assertEqual("repair", job.needs)
        job = db.transition(self.conn, job_id, "repairing", needs=None)
        self.assertEqual("repairing", job.state)


class TestRejected(DbTestCase):
    def test_rejected_from_held(self):
        job_id = self.enqueue()
        db.transition(self.conn, job_id, "executing")
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "held")
        job = db.transition(self.conn, job_id, "rejected")
        self.assertEqual("rejected", job.state)


class TestIllegalTransition(DbTestCase):
    def test_published_to_executing_raises_and_leaves_the_row_alone(self):
        job_id = self.enqueue()
        db.transition(self.conn, job_id, "executing")
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "held")
        db.transition(self.conn, job_id, "published")
        before = db.get_job(self.conn, job_id)
        with self.assertRaises(db.IllegalTransition):
            db.transition(self.conn, job_id, "executing", last_error="should not land")
        after = db.get_job(self.conn, job_id)
        self.assertEqual(before, after)
        self.assertEqual("published", after.state)
        self.assertIsNone(after.last_error)

    def test_unknown_job_raises(self):
        with self.assertRaises(db.UnknownJob):
            db.transition(self.conn, 4242, "planning")


class TestClaimNext(DbTestCase):
    def test_oldest_first_and_never_twice(self):
        first = self.enqueue(prompt="first")
        second = self.enqueue(prompt="second", brief="a brief, so it skips planning")
        claimed_first = db.claim_next(self.conn)
        self.assertEqual(first, claimed_first.id)
        self.assertEqual("planning", claimed_first.state)
        claimed_second = db.claim_next(self.conn)
        self.assertEqual(second, claimed_second.id)
        self.assertEqual("executing", claimed_second.state)
        self.assertIsNone(db.claim_next(self.conn))
        self.assertEqual([], db.list_jobs(self.conn, state="queued"))
        self.assertEqual(2, len(db.list_jobs(self.conn)))


class TestControl(DbTestCase):
    def test_seeded_running_and_set_control_round_trips(self):
        control = db.get_control(self.conn)
        self.assertEqual("running", control.state)
        self.assertIsNone(control.reason)
        self.assertTrue(control.updated_utc.endswith("Z"))
        db.set_control(self.conn, "paused", "maintenance")
        control = db.get_control(self.conn)
        self.assertEqual("paused", control.state)
        self.assertEqual("maintenance", control.reason)
        self.assertEqual(
            1, self.conn.execute("SELECT COUNT(*) AS c FROM control").fetchone()["c"]
        )
        self.assertEqual("running", db.set_control(self.conn, "running").state)
        with self.assertRaises(ValueError):
            db.set_control(self.conn, "stopped")


class TestJudgments(DbTestCase):
    def _entry(self, prompt):
        job_id = self.enqueue(prompt=prompt)
        return db.create_entry(self.conn, job_id, state="published", prompt=prompt)

    def test_one_answer_per_judge_pair_question(self):
        a = self._entry("dots")
        b = self._entry("lines")
        db.record_judgment(self.conn, a, b, "human", "octocat", "brief", "A")
        # same judge, same pair, the other question: allowed
        db.record_judgment(self.conn, a, b, "human", "octocat", "look", "B")
        # another judge, same pair and question: allowed
        db.record_judgment(self.conn, a, b, "agent", "gemma4:e4b", "brief", "tie")
        with self.assertRaises(sqlite3.IntegrityError):
            db.record_judgment(self.conn, a, b, "human", "octocat", "brief", "B")
        self.assertEqual(
            3, self.conn.execute("SELECT COUNT(*) AS c FROM judgments").fetchone()["c"]
        )

    def test_lineage_round_trip(self):
        parent = self._entry("dots")
        child = self._entry("dots, but responding to the bass")
        db.add_lineage(self.conn, child, parent, generation=2, critique_by="gemma4:e4b")
        row = self.conn.execute(
            "SELECT * FROM lineage WHERE child_entry_id = ?", (child,)
        ).fetchone()
        self.assertEqual(parent, row["parent_entry_id"])
        self.assertEqual(2, row["generation"])


class TestEntriesToCritique(DbTestCase):
    """Who the idle critic may pick: no live child, no critique at this version."""

    def parent(self, prompt="a root"):
        job = self.enqueue(prompt)
        self.conn.execute("UPDATE jobs SET state = 'published' WHERE id = ?", (job,))
        entry = db.create_entry(
            self.conn, job, "published", prompt=prompt,
            published_utc="2026-09-14T12:00:00Z",
        )
        self.conn.commit()
        return entry

    def child(self, parent, job_state, entry_state=None):
        job = self.enqueue("a child", parent_entry_id=parent)
        self.conn.execute("UPDATE jobs SET state = ? WHERE id = ?", (job_state, job))
        entry = None
        if entry_state is not None:
            entry = db.create_entry(
                self.conn, job, entry_state, prompt="a child", parent_entry_id=parent,
            )
            self.conn.execute(
                "INSERT INTO lineage (child_entry_id, parent_entry_id, generation, "
                "critique, critique_by, created_utc) VALUES (?, ?, 1, 'x', 'octocat', ?)",
                (entry, parent, db.utc_now()),
            )
        self.conn.commit()
        return job, entry

    def test_a_parent_with_no_child_is_offered_oldest_first(self):
        one = self.parent("first")
        two = self.parent("second")
        self.assertEqual([one, two], db.entries_to_critique(self.conn, "critic-v2", 5))

    def test_a_live_child_blocks_its_parent(self):
        for job_state, entry_state in (
            ("queued", None), ("executing", None), ("held", "held"), ("published", "published"),
        ):
            with self.subTest(job_state=job_state, entry_state=entry_state):
                parent = self.parent()
                self.child(parent, job_state, entry_state)
                self.assertNotIn(parent, db.entries_to_critique(self.conn, "critic-v2", 50))

    def test_a_dead_child_frees_its_parent(self):
        # A failed gate, a person's rejection, or a kept rejection ends that
        # line; the parent is offered again rather than blocked for good.
        for job_state, entry_state in (
            ("failed", None), ("failed", "failed-kept"), ("rejected", "rejected"),
        ):
            with self.subTest(job_state=job_state, entry_state=entry_state):
                parent = self.parent()
                self.child(parent, job_state, entry_state)
                self.assertIn(parent, db.entries_to_critique(self.conn, "critic-v2", 50))

    def test_a_critique_at_this_version_blocks_even_after_a_dead_child(self):
        parent = self.parent()
        self.child(parent, "failed", "failed-kept")
        db.record_critique(
            self.conn, parent, critique="x", critique_by="gemma4:e4b",
            prompt_version="critic-v2", spawned_job_id=None, rejected_reason=None,
        )
        self.assertNotIn(parent, db.entries_to_critique(self.conn, "critic-v2", 50))
        self.assertIn(parent, db.entries_to_critique(self.conn, "critic-v3", 50))


class TestEntryTransitions(DbTestCase):
    """The entry state machine of the lineage ledger's §5.1."""

    def held(self, prompt="a held one"):
        """A job carried to held with an entry on it, as the worker leaves it."""
        job_id = self.enqueue(prompt=prompt)
        db.transition(self.conn, job_id, "executing")
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "held")
        return job_id, db.create_entry(self.conn, job_id, "held", prompt=prompt)

    def kept(self, prompt="one the gate refused", **fields):
        job_id = self.enqueue(prompt=prompt)
        db.transition(self.conn, job_id, "failed")
        return job_id, db.create_entry(
            self.conn, job_id, "failed-kept", prompt=prompt, **fields
        )

    def state(self, entry_id):
        return db.get_entry(self.conn, entry_id)["state"]

    def test_held_goes_to_published_rejected_or_archived(self):
        for target in ("published", "rejected", "archived"):
            with self.subTest(target=target):
                _, entry_id = self.held()
                db.entry_transition(self.conn, entry_id, target)
                self.assertEqual(target, self.state(entry_id))

    def test_rejecting_stores_the_reason_on_the_entry(self):
        _, entry_id = self.held()
        db.entry_transition(self.conn, entry_id, "rejected", reject_reason="off brief")
        row = db.get_entry(self.conn, entry_id)
        self.assertEqual("rejected", row["state"])
        self.assertEqual("off brief", row["reject_reason"])

    def test_an_unpublished_kept_failure_can_be_archived(self):
        _, entry_id = self.kept()
        db.entry_transition(self.conn, entry_id, "archived")
        self.assertEqual("archived", self.state(entry_id))

    def test_a_published_kept_failure_cannot_be_archived(self):
        # It is on the site. Taking it down again would be the deletion this
        # project does not do (§5.1).
        _, entry_id = self.kept(published_utc=TIMESTAMP)
        with self.assertRaises(db.IllegalTransition):
            db.entry_transition(self.conn, entry_id, "archived")
        self.assertEqual("failed-kept", self.state(entry_id))

    def test_published_to_archived_is_refused(self):
        _, entry_id = self.held()
        db.entry_transition(self.conn, entry_id, "published")
        with self.assertRaises(db.IllegalTransition):
            db.entry_transition(self.conn, entry_id, "archived")
        self.assertEqual("published", self.state(entry_id))

    def test_archived_and_rejected_are_terminal(self):
        for terminal in ("archived", "rejected"):
            with self.subTest(terminal=terminal):
                _, entry_id = self.held()
                db.entry_transition(self.conn, entry_id, terminal)
                for target in ("published", "held", "archived", "rejected"):
                    with self.assertRaises(db.IllegalTransition):
                        db.entry_transition(self.conn, entry_id, target)

    def test_an_unknown_entry_and_an_unknown_state_are_told_apart(self):
        with self.assertRaises(db.UnknownEntry):
            db.entry_transition(self.conn, 9999, "archived")
        _, entry_id = self.held()
        with self.assertRaises(db.IllegalTransition):
            db.entry_transition(self.conn, entry_id, "vanished")

    def test_archive_entry_moves_the_job_and_leaves_a_failed_one_alone(self):
        job_id, entry_id = self.held()
        db.archive_entry(self.conn, entry_id)
        job = db.get_job(self.conn, job_id)
        self.assertEqual("rejected", job.state)
        self.assertEqual(db.ARCHIVED_BY_OPERATOR, job.last_error)

        # A kept failure's job is already terminal; it keeps the state that
        # says the gate ended it, and only says who archived the entry.
        kept_job, kept_entry = self.kept()
        db.archive_entry(self.conn, kept_entry)
        job = db.get_job(self.conn, kept_job)
        self.assertEqual("failed", job.state)
        self.assertEqual(db.ARCHIVED_BY_OPERATOR, job.last_error)

    def test_archiving_deletes_nothing(self):
        _, entry_id = self.held()
        before = dict(db.get_entry(self.conn, entry_id))
        db.archive_entry(self.conn, entry_id)
        after = dict(db.get_entry(self.conn, entry_id))
        self.assertEqual(
            {k: v for k, v in before.items() if k != "state"},
            {k: v for k, v in after.items() if k != "state"},
        )


class TestRequeue(DbTestCase):
    def test_requeue_from_gating_and_not_from_held(self):
        job_id = self.enqueue()
        db.transition(self.conn, job_id, "executing")
        db.transition(self.conn, job_id, "gating")
        job = db.requeue(self.conn, job_id, reason="stop now")
        self.assertEqual("queued", job.state)
        self.assertEqual("stop now", job.last_error)
        # the stopped job is claimable again
        self.assertEqual(job_id, db.claim_next(self.conn).id)

        other = self.enqueue(prompt="held one")
        db.transition(self.conn, other, "executing")
        db.transition(self.conn, other, "gating")
        db.transition(self.conn, other, "held")
        with self.assertRaises(db.IllegalTransition):
            db.requeue(self.conn, other)
        self.assertEqual("held", db.get_job(self.conn, other).state)


if __name__ == "__main__":
    unittest.main()
