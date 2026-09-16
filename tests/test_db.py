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
    "activity",
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
    "submissions",
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
            strip_path="/tmp/strip.png",  # critic-v3 only offers entries it can see
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

    def parent_without_a_strip(self, prompt="a blind root"):
        """A published entry the gate left no strip for."""
        job = self.enqueue(prompt)
        self.conn.execute("UPDATE jobs SET state = 'published' WHERE id = ?", (job,))
        entry = db.create_entry(
            self.conn, job, "published", prompt=prompt,
            published_utc="2026-09-13T12:00:00Z",  # older than parent()'s
        )
        self.conn.commit()
        return entry

    def test_an_entry_with_no_strip_is_never_offered(self):
        """critic-v3 refuses what it cannot see, so it is not offered at all."""
        blind = self.parent_without_a_strip()
        self.assertEqual([], db.entries_to_critique(self.conn, "critic-v3", 5))
        # and an empty string is as blind as a NULL
        self.conn.execute(
            "UPDATE entries SET strip_path = '   ' WHERE id = ?", (blind,)
        )
        self.conn.commit()
        self.assertEqual([], db.entries_to_critique(self.conn, "critic-v3", 5))

    def test_a_blind_entry_does_not_block_the_one_behind_it(self):
        """The head-of-line case: refusing writes no row, so it must not queue.

        The blind entry is older, so oldest-first would hand it back on every
        round and the sighted one behind it would never be reached.
        """
        self.parent_without_a_strip()
        sighted = self.parent(prompt="a root with a strip")
        self.assertEqual([sighted], db.entries_to_critique(self.conn, "critic-v3", 1))

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


class TestActivity(DbTestCase):
    """Migration 008: the row the worker writes and the operator UI reads."""

    def test_a_new_step_closes_the_one_this_pid_had_open(self):
        first = db.begin_step(self.conn, step="writing",
                              headline="Writing the sketch")
        second = db.begin_step(self.conn, step="evaluating",
                               headline="Evaluating the sketch in a browser")
        rows = {
            int(row["id"]): row
            for row in self.conn.execute("SELECT * FROM activity")
        }
        self.assertIsNotNone(rows[first]["ended_utc"])
        self.assertIsNone(rows[second]["ended_utc"])

    def test_another_pids_open_step_is_left_alone(self):
        # Two workers is not a thing this system has, but a worker that was
        # killed is: its row stays open, because it is the evidence of what
        # that process was doing when it stopped.
        stale = db.begin_step(self.conn, step="writing", headline="Writing",
                              pid=999999)
        db.begin_step(self.conn, step="planning", headline="Planning")
        row = self.conn.execute(
            "SELECT ended_utc FROM activity WHERE id = ?", (stale,)
        ).fetchone()
        self.assertIsNone(row["ended_utc"])

    def test_current_activity_is_the_open_one_and_recent_is_the_closed_ones(self):
        db.begin_step(self.conn, step="claiming", headline="Picking up the next job")
        db.begin_step(self.conn, step="planning",
                      headline="Turning the prompt into a brief")
        db.begin_step(self.conn, step="writing", headline="Writing the sketch",
                      model="qwen3-coder:30b")
        current = db.current_activity(self.conn)
        self.assertEqual("writing", current["step"])
        self.assertEqual("qwen3-coder:30b", current["model"])
        trail = db.recent_activity(self.conn, 3)
        self.assertEqual(
            ["Turning the prompt into a brief", "Picking up the next job"],
            [row["headline"] for row in trail],
        )

    def test_update_step_fills_in_what_the_step_learned_late(self):
        activity_id = db.begin_step(self.conn, step="judging",
                                    headline="Comparing two sketches")
        db.update_step(self.conn, activity_id,
                       detail="gemma4:e4b · entry 231 against entry 88")
        row = db.current_activity(self.conn)
        self.assertEqual("gemma4:e4b · entry 231 against entry 88", row["detail"])
        # a call with nothing in it writes nothing rather than blanking the row
        db.update_step(self.conn, activity_id)
        self.assertEqual(row["detail"], db.current_activity(self.conn)["detail"])

    def test_end_step_closes_without_opening_another(self):
        activity_id = db.begin_step(self.conn, step="idle",
                                    headline="Nothing to do")
        db.end_step(self.conn, activity_id)
        self.assertIsNone(db.current_activity(self.conn))
        self.assertEqual(["Nothing to do"],
                         [row["headline"] for row in db.recent_activity(self.conn)])

    def test_the_table_is_pruned_to_activity_keep(self):
        for n in range(db.ACTIVITY_KEEP + 25):
            db.begin_step(self.conn, step="idle", headline=f"step {n}")
        count = self.conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        self.assertEqual(db.ACTIVITY_KEEP, count)
        # the newest survive, not the oldest
        self.assertEqual(f"step {db.ACTIVITY_KEEP + 24}",
                         db.current_activity(self.conn)["headline"])

    def test_a_database_without_the_table_answers_rather_than_raising(self):
        # An older file — one that predates migration 008 — should render a
        # card that says nothing, not a traceback in the browser.
        self.conn.execute("DROP TABLE activity")
        self.assertIsNone(db.current_activity(self.conn))
        self.assertEqual([], db.recent_activity(self.conn))


class TestSubmissions(DbTestCase):
    """Migration 010: what the public asked for, before a person released it."""

    def setUp(self):
        super().setUp()
        self.job_id = self.enqueue("a quiet grid")
        self.entry_id = db.create_entry(
            self.conn, self.job_id, "held", prompt="a quiet grid"
        )

    def add(self, remote_id=31, kind="prompt", username="octocat", entry_id=None,
            text="a tide of small triangles", created_utc="2026-09-16T15:04:22Z"):
        return db.add_submission(
            self.conn,
            remote_id=remote_id,
            kind=kind,
            username=username,
            entry_id=entry_id,
            text=text,
            created_utc=created_utc,
        )

    def rows(self):
        return list(self.conn.execute("SELECT * FROM submissions ORDER BY id"))

    def test_a_submission_lands_pending_with_both_stamps(self):
        submission_id = self.add()
        row = db.submission(self.conn, submission_id)
        self.assertEqual("pending", row["state"])
        self.assertEqual(31, row["remote_id"])
        self.assertEqual("octocat", row["username"])
        self.assertEqual("a tide of small triangles", row["text"])
        self.assertEqual("2026-09-16T15:04:22Z", row["created_utc"])
        self.assertTrue(row["pulled_utc"], "the node stamps when it first saw it")
        self.assertIsNone(row["job_id"])
        self.assertIsNone(row["decided_utc"])

    def test_the_same_remote_id_twice_writes_one_row_and_answers_none(self):
        first = self.add()
        second = self.add(text="the boundary second, delivered again")
        self.assertIsNotNone(first)
        self.assertIsNone(second, "at-least-once delivery must not queue it twice")
        rows = self.rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("a tide of small triangles", rows[0]["text"])

    def test_a_repeat_does_not_resurrect_a_declined_row(self):
        submission_id = self.add()
        db.decline_submission(self.conn, submission_id, "not this week")
        self.assertIsNone(self.add())
        row = db.submission(self.conn, submission_id)
        self.assertEqual("declined", row["state"])
        self.assertEqual("not this week", row["decline_reason"])

    def test_releasing_records_the_job_and_the_moment(self):
        submission_id = self.add()
        db.release_submission(self.conn, submission_id, self.job_id)
        row = db.submission(self.conn, submission_id)
        self.assertEqual("released", row["state"])
        self.assertEqual(self.job_id, row["job_id"])
        self.assertTrue(row["decided_utc"])

    def test_a_released_row_cannot_be_released_again(self):
        submission_id = self.add()
        db.release_submission(self.conn, submission_id, self.job_id)
        other = self.enqueue("a second job")
        db.release_submission(self.conn, submission_id, other)
        self.assertEqual(self.job_id, db.submission(self.conn, submission_id)["job_id"])

    def test_declining_keeps_the_text_and_refuses_a_later_release(self):
        submission_id = self.add()
        db.decline_submission(self.conn, submission_id, "off topic")
        row = db.submission(self.conn, submission_id)
        self.assertEqual("declined", row["state"])
        self.assertEqual("a tide of small triangles", row["text"])
        db.release_submission(self.conn, submission_id, self.job_id)
        self.assertEqual("declined", db.submission(self.conn, submission_id)["state"])

    def test_pending_submissions_is_oldest_first_and_skips_what_was_decided(self):
        old = self.add(remote_id=1, created_utc="2026-09-16T10:00:00Z", text="first")
        mid = self.add(remote_id=2, created_utc="2026-09-16T11:00:00Z", text="second")
        new = self.add(remote_id=3, created_utc="2026-09-16T12:00:00Z", text="third")
        self.assertEqual(
            [old, mid, new],
            [row["id"] for row in db.pending_submissions(self.conn)],
        )
        db.decline_submission(self.conn, mid, "no")
        db.release_submission(self.conn, new, self.job_id)
        self.assertEqual(
            [old], [row["id"] for row in db.pending_submissions(self.conn)]
        )
        self.assertEqual([], db.pending_submissions(self.conn, 0))

    def test_a_critique_names_its_parent_entry(self):
        submission_id = self.add(
            kind="critique", entry_id=self.entry_id, text="thin the lines at the edge"
        )
        row = db.submission(self.conn, submission_id)
        self.assertEqual("critique", row["kind"])
        self.assertEqual(self.entry_id, row["entry_id"])

    def test_the_schema_refuses_a_kind_and_a_state_it_does_not_know(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.add(kind="rant")
        submission_id = self.add(remote_id=99)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE submissions SET state = 'approved' WHERE id = ?",
                (submission_id,),
            )

    def test_counts_are_by_state_with_zeros_and_no_table_is_zeros_too(self):
        self.assertEqual(
            {"pending": 0, "released": 0, "declined": 0},
            db.submission_counts(self.conn),
        )
        self.add(remote_id=1)
        released = self.add(remote_id=2)
        declined = self.add(remote_id=3)
        db.release_submission(self.conn, released, self.job_id)
        db.decline_submission(self.conn, declined, "no")
        self.assertEqual(
            {"pending": 1, "released": 1, "declined": 1},
            db.submission_counts(self.conn),
        )
        # An older file — one that predates migration 010 — answers zeros
        # rather than stopping the console's page.
        self.conn.execute("DROP TABLE submissions")
        self.assertEqual(
            {"pending": 0, "released": 0, "declined": 0},
            db.submission_counts(self.conn),
        )

    def test_the_job_state_machine_is_untouched_by_this_table(self):
        # Decision §1.2, asserted rather than trusted: a submission is not a
        # job, so nothing here may have grown a job state to hold one.
        self.assertEqual(
            {
                "queued", "planning", "executing", "gating", "repairing",
                "needs-laptop", "held", "published", "rejected", "failed",
            },
            set(db.TRANSITIONS),
        )
        self.assertNotIn("submissions", str(db.TRANSITIONS))


if __name__ == "__main__":
    unittest.main()
