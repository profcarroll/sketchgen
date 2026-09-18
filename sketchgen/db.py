"""SQLite access for sketchgen: connection, migrations, typed helpers, and the
job and entry state machines.

Plain stdlib, Python 3.12, no ORM. Every timestamp written here is UTC, ISO 8601
with a trailing Z, and every column holding one is named ``*_utc``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "ACTIVITY_KEEP",
    "NODE_IDENTITY_KEYS",
    "DEFAULT_DB_PATH",
    "ENTRY_TRANSITIONS",
    "TRANSITIONS",
    "IllegalTransition",
    "UnknownEntry",
    "UnknownJob",
    "Attempt",
    "Control",
    "Job",
    "add_attempt",
    "add_lineage",
    "add_submission",
    "archive_entry",
    "begin_step",
    "billing_daily",
    "billing_services",
    "billing_tenancies",
    "claim_next",
    "connect",
    "create_entry",
    "current_activity",
    "decline_submission",
    "end_step",
    "enqueue",
    "entries_to_critique",
    "entry_transition",
    "get_control",
    "get_critique",
    "get_entry",
    "get_job",
    "get_meta",
    "init",
    "list_jobs",
    "pending_submissions",
    "recent_activity",
    "record_billing",
    "record_critique",
    "record_judgment",
    "release_submission",
    "requeue",
    "schema_version",
    "set_control",
    "set_meta",
    "submission",
    "submission_counts",
    "transition",
    "update_step",
    "utc_now",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"
MIGRATION_RE = re.compile(r"^(\d{3})_[A-Za-z0-9_-]+\.sql$")

#: Where the database lives unless told otherwise.
DEFAULT_DB_PATH = os.environ.get(
    "SKETCHGEN_DB", str(Path.home() / "sketchgen" / "sketchgen.db")
)

# ---------------------------------------------------------------------------
# The job state machine
# ---------------------------------------------------------------------------

#: Legal successors, by state. Anything not listed here is refused by
#: ``transition()``. The three ``'queued'`` entries below are the worker's
#: "stop now" path: the operator aborts the attempt in flight and the job goes
#: back on the queue rather than being lost (spec §4, plan §5 packet 2.3). It is
#: modelled as a transition like any other so that the abort is recorded and
#: stamped, instead of being a quiet UPDATE somewhere in the worker.
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"planning", "executing", "needs-laptop", "failed"}),
    # planning -> queued was added after the first unattended night: job 5 was
    # claimed into planning, the planner's reply had no Brief heading, and the
    # job sat in planning for good because nothing could move it back. The
    # worker's sweep now re-queues a job left in any running state by a worker
    # that died (worker.py, sweep_stuck), and planning is a running state like
    # the other three.
    "planning": frozenset({"executing", "needs-laptop", "failed", "queued"}),
    # executing -> repairing is packet 2.3's malformed-response path: a response
    # with no js block never reaches the gate, so the job repairs without
    # passing through gating and its attempt row carries a null gate_exit with
    # "executor: …" as its evidence.
    "executing": frozenset({"gating", "repairing", "failed", "queued"}),
    "gating": frozenset({"held", "repairing", "failed", "queued"}),
    "repairing": frozenset({"executing", "needs-laptop", "failed", "queued"}),
    "needs-laptop": frozenset({"planning", "executing", "repairing", "failed"}),
    "held": frozenset({"published", "rejected"}),
    # terminal
    "published": frozenset(),
    "rejected": frozenset(),
    "failed": frozenset(),
}

#: States a job may be requeued from: the stop-now path, and the worker's sweep
#: of jobs a dead worker left mid-flight. These are exactly the states in which
#: a job is supposed to have a worker attending it.
REQUEUABLE = frozenset({"planning", "executing", "gating", "repairing"})

CONTROL_STATES = frozenset({"running", "pausing", "paused"})

# ---------------------------------------------------------------------------
# The entry state machine
# ---------------------------------------------------------------------------

#: Legal successors for an ENTRY, which is a different machine from the job's
#: above even though the two share three state names. An entry is the
#: gallery-visible half of a job and it moves only when a person moves it.
#:
#: ``archived`` (lineage ledger §5.1) is the terminal state for something the
#: operator wants off their primary lists without deleting it: a held entry
#: they will not publish and will not reject, or a kept failure nobody ever put
#: on the site. Nothing on disk is touched by it and the row stays. It is not a
#: public state, so an archived entry has no page under ``e/``.
#:
#: ``rejected`` is terminal and, since §5.2, public: rejecting publishes the
#: entry to the rejections catalog with the operator's reason, because a
#: rejection that vanishes is not a record of a decision.
ENTRY_TRANSITIONS: dict[str, frozenset[str]] = {
    "held": frozenset({"published", "rejected", "archived"}),
    # A kept failure can be archived, or reopened for a person to judge, and
    # either one only while nobody has published it: once it is on the
    # rejections page it is part of the public record and taking it down again
    # would be the deletion this project does not do. Both conditions are
    # enforced in entry_transition, which is the only place that can see
    # published_utc.
    #
    # `held` is here because the gate stopped being the only judge of a sketch:
    # a job that never failed a QA check and only missed an assertion a model
    # wrote is now created held, for a person. The kept failures recorded under
    # the old rule have no such path, and 12 of them were never published — this
    # is the door back for exactly those, and it is shut for the 18 that are
    # already public.
    "failed-kept": frozenset({"archived", "held"}),
    # terminal
    "published": frozenset(),
    "rejected": frozenset(),
    "archived": frozenset(),
}


class IllegalTransition(Exception):
    """A job or entry was asked to move to a state that is not a legal successor."""


class UnknownJob(Exception):
    """No job with that id."""


class UnknownEntry(Exception):
    """No entry with that id."""


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: int
    state: str
    prompt: str
    brief: str | None = None
    assertions_json: str | None = None
    planner: str | None = None
    executor: str | None = None
    rules_file: str | None = None
    submitted_by: str = ""
    parent_entry_id: int | None = None
    publication: str = "hold"
    max_attempts: int = 3
    created_utc: str = ""
    updated_utc: str = ""
    needs: str | None = None
    last_error: str | None = None
    # Migration 005: the pending half of a lineage link. A spawned job carries
    # the critique that asked for it until the worker has an entry to hang it
    # on; see sketchgen/lineage.py.
    critique: str | None = None
    critique_by: str | None = None

    @property
    def assertions(self) -> list[str]:
        """The planner's assertion list, or [] if it has none yet."""
        return json.loads(self.assertions_json) if self.assertions_json else []


@dataclass
class Attempt:
    id: int
    job_id: int
    n: int
    started_utc: str | None = None
    finished_utc: str | None = None
    model: str | None = None
    rules_file: str | None = None
    prompt_version: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prefill_s: float | None = None
    decode_s: float | None = None
    wall_s: float | None = None
    source_dir: str | None = None
    gate_exit: int | None = None
    gate_report_path: str | None = None
    evidence: str | None = None
    statement: str | None = None


@dataclass
class Control:
    id: int
    state: str
    reason: str | None
    updated_utc: str


def _row_to(cls: type, row: sqlite3.Row | None):
    if row is None:
        return None
    names = {f.name for f in dataclass_fields(cls)}
    return cls(**{k: row[k] for k in row.keys() if k in names})


# ---------------------------------------------------------------------------
# Connection and migrations
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """Now, UTC, ISO 8601 with a Z. The only clock this module reads."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (never create the directory for) a database and set our pragmas.

    Autocommit mode: statements commit as they run, and the few places that need
    more than one statement to be atomic say ``BEGIN IMMEDIATE`` themselves.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for entry in sorted(directory.iterdir()) if directory.is_dir() else []:
        match = MIGRATION_RE.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry))
    found.sort(key=lambda pair: (pair[0], pair[1].name))
    return found


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_utc TEXT NOT NULL
        )
        """
    )


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every unapplied migrations/NNN_*.sql in order. Safe to rerun.

    Returns the versions applied by this call (empty when there was nothing to do).

    Migrations run with ``PRAGMA foreign_keys`` **off**, and ``PRAGMA
    foreign_key_check`` runs over the whole database afterwards. That is
    SQLite's own procedure, and 007 is why it is needed here: changing a CHECK
    constraint means rebuilding the table, and dropping a parent table counts
    one deferred violation per child row without counting them back down when
    the replacement takes its name, so a rebuild with foreign keys on is
    refused at the COMMIT although nothing dangles. The pragma is a no-op
    inside a transaction and each migration is one, so this is the only place
    it can be set. The check afterwards is the price of turning it off: a
    migration that really did leave a dangling reference fails here, in the
    call that made it, rather than at some unrelated write weeks later.
    """
    _ensure_schema_version_table(conn)
    done = {row["version"] for row in conn.execute("SELECT version FROM schema_version")}
    applied: list[int] = []
    was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        applied = _apply(conn, directory, done)
    finally:
        if was_on:
            conn.execute("PRAGMA foreign_keys = ON")
    if applied:
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            rows = ", ".join(f"{row[0]} row {row[1]} -> {row[2]}" for row in broken[:5])
            raise sqlite3.IntegrityError(
                f"migrations {applied} left dangling references: {rows}"
            )
    return applied


def _apply(
    conn: sqlite3.Connection, directory: Path, done: set[int]
) -> list[int]:
    """The loop :func:`migrate` runs; see its docstring for the pragma around it."""
    applied: list[int] = []
    for version, path in _migration_files(directory):
        if version in done:
            continue
        # The whole migration, and the row recording it, run as one script inside
        # one transaction. The BEGIN and COMMIT are in the script rather than
        # around it because executescript() commits any transaction already open.
        # The two literals are safe: the name matched MIGRATION_RE, the timestamp
        # is ours.
        script = "\n".join(
            [
                "BEGIN IMMEDIATE;",
                path.read_text(encoding="utf-8"),
                "INSERT INTO schema_version (version, name, applied_utc) VALUES "
                f"({version}, '{path.name}', '{utc_now()}');",
                "COMMIT;",
            ]
        )
        try:
            conn.executescript(script)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        applied.append(version)
    return applied


def init(path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> list[int]:
    """Create the database file if needed and bring it up to the latest schema."""
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
    try:
        return migrate(conn)
    finally:
        conn.close()


def schema_version(conn: sqlite3.Connection) -> int:
    """The highest applied migration number, or 0 for an unmigrated database."""
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["v"]) if row and row["v"] is not None else 0


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

_JOB_FIELDS = (
    "brief",
    "assertions_json",
    "planner",
    "executor",
    "rules_file",
    "parent_entry_id",
    "publication",
    "max_attempts",
    "needs",
    "last_error",
    "critique",
    "critique_by",
)


def _check_fields(given: Iterable[str], allowed: Iterable[str], what: str) -> None:
    unknown = sorted(set(given) - set(allowed))
    if unknown:
        raise ValueError(f"unknown {what} column(s): {', '.join(unknown)}")


def enqueue(
    conn: sqlite3.Connection, prompt: str, submitted_by: str, **opts: Any
) -> int:
    """Put a new job on the queue. ``submitted_by`` is a GitHub username."""
    if "assertions" in opts:
        opts["assertions_json"] = json.dumps(opts.pop("assertions"))
    _check_fields(opts.keys(), _JOB_FIELDS, "job")
    now = utc_now()
    columns = ["state", "prompt", "submitted_by", "created_utc", "updated_utc"]
    values: list[Any] = ["queued", prompt, submitted_by, now, now]
    for key, value in sorted(opts.items()):
        columns.append(key)
        values.append(value)
    placeholders = ",".join("?" for _ in columns)
    cur = conn.execute(
        f"INSERT INTO jobs ({','.join(columns)}) VALUES ({placeholders})", values
    )
    return int(cur.lastrowid)


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to(Job, row)


def list_jobs(conn: sqlite3.Connection, state: str | None = None) -> list[Job]:
    """Jobs, oldest first. ``state`` filters; None returns all of them."""
    if state is None:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_utc, id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state = ? ORDER BY created_utc, id", (state,)
        ).fetchall()
    return [_row_to(Job, row) for row in rows]


def transition(
    conn: sqlite3.Connection, job_id: int, new_state: str, **fields: Any
) -> Job:
    """Move a job to ``new_state``, stamping ``updated_utc``.

    Raises IllegalTransition if the move is not in TRANSITIONS, UnknownJob if
    there is no such job. Extra keyword arguments update job columns in the same
    statement, so evidence and errors land with the state that explains them.
    """
    _check_fields(fields.keys(), _JOB_FIELDS, "job")
    if new_state not in TRANSITIONS:
        raise IllegalTransition(f"unknown state {new_state!r}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob(f"no job {job_id}")
        current = row["state"]
        if new_state not in TRANSITIONS[current]:
            raise IllegalTransition(f"{current} -> {new_state} (job {job_id})")
        columns = ["state = ?", "updated_utc = ?"]
        values: list[Any] = [new_state, utc_now()]
        for key, value in sorted(fields.items()):
            columns.append(f"{key} = ?")
            values.append(value)
        values.append(job_id)
        conn.execute(f"UPDATE jobs SET {', '.join(columns)} WHERE id = ?", values)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return get_job(conn, job_id)


def requeue(conn: sqlite3.Connection, job_id: int, reason: str | None = None) -> Job:
    """The worker's stop-now path: abandon the attempt in flight and queue again.

    Legal only from executing, gating and repairing (see TRANSITIONS); from
    anything else this raises IllegalTransition, which is the point. Clears
    ``needs`` and records ``reason`` in ``last_error``.
    """
    return transition(conn, job_id, "queued", last_error=reason, needs=None)


def claim_next(conn: sqlite3.Connection) -> Job | None:
    """Claim the oldest queued job, or return None if there is none.

    Moves it to ``planning`` when it has no brief yet and ``executing`` when it
    does. The select and the update run in one IMMEDIATE transaction, so two
    workers cannot claim the same job.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id, brief FROM jobs WHERE state = 'queued' "
            "ORDER BY created_utc, id LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        next_state = "executing" if row["brief"] else "planning"
        conn.execute(
            "UPDATE jobs SET state = ?, updated_utc = ? WHERE id = ? AND state = 'queued'",
            (next_state, utc_now(), row["id"]),
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return get_job(conn, row["id"])


# ---------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------

_ATTEMPT_FIELDS = tuple(
    f.name for f in dataclass_fields(Attempt) if f.name not in ("id", "job_id", "n")
)


def add_attempt(
    conn: sqlite3.Connection, job_id: int, n: int | None = None, **fields: Any
) -> int:
    """Record one execute+gate attempt. ``n`` defaults to the next 1-based number."""
    _check_fields(fields.keys(), _ATTEMPT_FIELDS, "attempt")
    if n is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(n), 0) AS n FROM attempts WHERE job_id = ?", (job_id,)
        ).fetchone()
        n = int(row["n"]) + 1
    columns = ["job_id", "n"]
    values: list[Any] = [job_id, n]
    for key, value in sorted(fields.items()):
        columns.append(key)
        values.append(value)
    placeholders = ",".join("?" for _ in columns)
    cur = conn.execute(
        f"INSERT INTO attempts ({','.join(columns)}) VALUES ({placeholders})", values
    )
    return int(cur.lastrowid)


def list_attempts(conn: sqlite3.Connection, job_id: int) -> list[Attempt]:
    rows = conn.execute(
        "SELECT * FROM attempts WHERE job_id = ? ORDER BY n", (job_id,)
    ).fetchall()
    return [_row_to(Attempt, row) for row in rows]


# ---------------------------------------------------------------------------
# Entries, judgments, lineage
# ---------------------------------------------------------------------------

_ENTRY_FIELDS = (
    "prompt",
    "brief",
    "statement",
    "planner",
    "planner_prompt_version",
    "executor",
    "executor_prompt_version",
    "rules_file",
    "assertions_json",
    "offplan_json",
    "harness_version",
    "attempts",
    "prompt_tokens",
    "completion_tokens",
    "wall_s",
    "shape",
    "seed",
    "parent_entry_id",
    "submitted_by",
    "source_dir",
    "strip_path",
    "png_path",
    "published_utc",
    "publish_commit",
    # Migration 007: why a person rejected it, in a column of its own rather
    # than overloading the job's last_error, which belongs to the executor.
    "reject_reason",
)


def create_entry(
    conn: sqlite3.Connection, job_id: int, state: str = "held", **fields: Any
) -> int:
    """Create the gallery-visible row for a job. One entry per job."""
    _check_fields(fields.keys(), _ENTRY_FIELDS, "entry")
    columns = ["job_id", "state", "created_utc"]
    values: list[Any] = [job_id, state, utc_now()]
    for key, value in sorted(fields.items()):
        columns.append(key)
        values.append(value)
    placeholders = ",".join("?" for _ in columns)
    cur = conn.execute(
        f"INSERT INTO entries ({','.join(columns)}) VALUES ({placeholders})", values
    )
    return int(cur.lastrowid)


def get_entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM entries WHERE id = ?", (int(entry_id),)
    ).fetchone()


def entry_transition(
    conn: sqlite3.Connection, entry_id: int, new_state: str, **fields: Any
) -> sqlite3.Row:
    """Move an entry to ``new_state`` under :data:`ENTRY_TRANSITIONS`.

    The entry's own machine, checked in one IMMEDIATE transaction like
    :func:`transition` checks the job's, so a move and the columns that explain
    it land together or not at all. Extra keyword arguments update entry
    columns in the same statement — ``reject_reason`` with the move to
    ``rejected``, for one.

    Raises :class:`IllegalTransition` for a move the machine does not allow and
    :class:`UnknownEntry` when there is no such entry.
    """
    _check_fields(fields.keys(), _ENTRY_FIELDS, "entry")
    if new_state not in ENTRY_TRANSITIONS:
        raise IllegalTransition(f"unknown entry state {new_state!r}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT state, published_utc FROM entries WHERE id = ?", (int(entry_id),)
        ).fetchone()
        if row is None:
            raise UnknownEntry(f"no entry {entry_id}")
        current = str(row["state"])
        if new_state not in ENTRY_TRANSITIONS.get(current, frozenset()):
            raise IllegalTransition(f"{current} -> {new_state} (entry {entry_id})")
        # The one guard the table above cannot express: a kept failure may leave
        # that state only while nobody has published it (§5.1). Archiving a
        # public entry would hide it; reopening one to `held` would take it off
        # the site altogether, because `held` is not a public state. Same rule,
        # same reason, both spellings.
        if current == "failed-kept" and new_state in ("archived", "held") \
                and row["published_utc"]:
            verb = "archived" if new_state == "archived" else "reopened"
            raise IllegalTransition(
                f"entry {entry_id} is a kept failure that is already on the site "
                f"(published {row['published_utc']}); it cannot be {verb}"
            )
        columns = ["state = ?"]
        values: list[Any] = [new_state]
        for key, value in sorted(fields.items()):
            columns.append(f"{key} = ?")
            values.append(value)
        values.append(int(entry_id))
        conn.execute(f"UPDATE entries SET {', '.join(columns)} WHERE id = ?", values)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return get_entry(conn, entry_id)


#: What the job row says about an entry the operator archived. There is no
#: ``archived`` job state and there is not going to be one: the job is over
#: either way, and a sixth terminal job state would mean touching every count,
#: filter and funnel in the console to say nothing new.
ARCHIVED_BY_OPERATOR = "archived by operator"


def archive_entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row:
    """Archive one held entry or unpublished kept failure. Deletes nothing.

    The files are untouched: the attempt directories under ``jobs/``, the
    strip, the gate report and the entry row itself all stay exactly where they
    are. Archiving is about the operator's lists, not about the record.

    The job follows its entry into ``rejected`` when the machine allows it (a
    ``held`` job does); a job that is already ``failed`` is already terminal and
    keeps that state, which is the one the console reads as "rejected · gate".
    Either way its ``last_error`` says who ended it.
    """
    row = entry_transition(conn, entry_id, "archived")
    job_id = row["job_id"]
    if job_id is None:  # pragma: no cover - the schema says NOT NULL
        return row
    job = get_job(conn, int(job_id))
    if job is None:
        return row
    if "rejected" in TRANSITIONS.get(job.state, frozenset()):
        transition(conn, job.id, "rejected", last_error=ARCHIVED_BY_OPERATOR)
    else:
        conn.execute(
            "UPDATE jobs SET last_error = ?, updated_utc = ? WHERE id = ?",
            (ARCHIVED_BY_OPERATOR, utc_now(), job.id),
        )
    return get_entry(conn, entry_id)


def record_judgment(
    conn: sqlite3.Connection,
    entry_a: int,
    entry_b: int,
    judge_kind: str,
    judge_id: str,
    question: str,
    choice: str,
    prompt_version: str | None = None,
    artefact_hash: str | None = None,
) -> int:
    """Store one answer. The unique constraint lets a judge answer a
    (pair, question) once; a second answer raises sqlite3.IntegrityError."""
    cur = conn.execute(
        "INSERT INTO judgments (entry_a, entry_b, judge_kind, judge_id, question, "
        "choice, prompt_version, artefact_hash, created_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            entry_a,
            entry_b,
            judge_kind,
            judge_id,
            question,
            choice,
            prompt_version,
            artefact_hash,
            utc_now(),
        ),
    )
    return int(cur.lastrowid)


def add_lineage(
    conn: sqlite3.Connection,
    child_entry_id: int,
    parent_entry_id: int | None,
    generation: int = 1,
    critique_by: str | None = None,
    critique: str | None = None,
) -> None:
    """Record which entry begat which, and the critique that did it."""
    conn.execute(
        "INSERT OR REPLACE INTO lineage (child_entry_id, parent_entry_id, generation, "
        "critique_by, critique, created_utc) VALUES (?,?,?,?,?,?)",
        (child_entry_id, parent_entry_id, generation, critique_by, critique, utc_now()),
    )


# ---------------------------------------------------------------------------
# Critiques — migration 006, the worker's idle work
# ---------------------------------------------------------------------------


def record_critique(
    conn: sqlite3.Connection,
    entry_id: int,
    *,
    critique: str | None,
    critique_by: str | None,
    prompt_version: str,
    spawned_job_id: int | None = None,
    rejected_reason: str | None = None,
    strip_path: str | None = None,
    strip_sha256: str | None = None,
) -> int:
    """Record one critique of one entry, whether or not it spawned anything.

    One row per (entry, prompt_version): the UNIQUE constraint in migration 006
    is what stops the worker critiquing the same entry on every idle round, so a
    second call for the same pair raises ``sqlite3.IntegrityError`` by design.

    ``strip_path`` and ``strip_sha256`` (migration 009) are the frame strip the
    critic was shown and the sha256 of exactly those bytes. Both are NULL for
    every row written under critic-v2, which was blind.
    """
    cur = conn.execute(
        "INSERT INTO critiques (entry_id, critique, critique_by, prompt_version, "
        "spawned_job_id, rejected_reason, strip_path, strip_sha256, created_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            int(entry_id),
            critique,
            critique_by,
            prompt_version,
            spawned_job_id,
            rejected_reason,
            strip_path or None,
            strip_sha256 or None,
            utc_now(),
        ),
    )
    return int(cur.lastrowid)


def get_critique(
    conn: sqlite3.Connection, entry_id: int, prompt_version: str
) -> sqlite3.Row | None:
    """This entry's critique under this prompt version, if it has one."""
    return conn.execute(
        "SELECT * FROM critiques WHERE entry_id = ? AND prompt_version = ?",
        (int(entry_id), prompt_version),
    ).fetchone()


def entries_to_critique(
    conn: sqlite3.Connection, prompt_version: str, limit: int = 1
) -> list[int]:
    """Published entries with no live child and no critique yet, oldest first.

    "No live child" is asked of all three places a child can show up — a job
    spawned from this entry, an entry that came from it, and a recorded
    `lineage` link — because the three are written at different moments in a
    line's life and a line that is already running does not need a second
    critique of its parent (spec §8.1). A child that died — its job failed or
    was rejected, its entry rejected or kept as a rejection — does not count:
    that line is over, and without this the parent would never be offered
    again (entries 2, 10, 14 and 35 on 2026-09-14, each blocked for good by
    one dead child). "No critique yet" is per prompt version, which is
    migration 006's UNIQUE constraint read from the other side, so a parent
    gets one more chance per version, not an endless supply.

    An entry with no ``strip_path`` is not offered at all. Since critic-v3
    :func:`sketchgen.lineage.critique` refuses an entry it cannot see, and a
    refusal writes no row, so nothing marks the entry as tried: offered oldest
    first at LIMIT 1 it would be picked again every round, and one strip-less
    entry would stop the idle critic for good. That is the same head-of-line
    block a dead child used to cause above, and it is excluded here for the
    same reason -- the query offers only entries a critique can actually be
    written for.
    """
    if int(limit) <= 0:
        return []
    rows = conn.execute(
        "SELECT e.id AS id FROM entries e "
        "WHERE e.state = 'published' "
        "  AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.parent_entry_id = e.id "
        "                    AND j.state NOT IN ('failed', 'rejected')) "
        "  AND NOT EXISTS (SELECT 1 FROM entries c WHERE c.parent_entry_id = e.id "
        "                    AND c.state NOT IN ('rejected', 'failed-kept')) "
        "  AND NOT EXISTS (SELECT 1 FROM lineage l JOIN entries c ON c.id = l.child_entry_id "
        "                    WHERE l.parent_entry_id = e.id "
        "                    AND c.state NOT IN ('rejected', 'failed-kept')) "
        "  AND NOT EXISTS (SELECT 1 FROM critiques q WHERE q.entry_id = e.id "
        "                    AND q.prompt_version = ?) "
        "  AND e.strip_path IS NOT NULL AND TRIM(e.strip_path) <> '' "
        "ORDER BY e.created_utc, e.id LIMIT ?",
        (prompt_version, int(limit)),
    ).fetchall()
    return [int(row["id"]) for row in rows]


# ---------------------------------------------------------------------------
# Submissions — migration 010, what the public asked for
# ---------------------------------------------------------------------------

#: The states of this table's own machine, in the order the console counts
#: them. It is not the job machine and it shares nothing with it on purpose:
#: an unreleased submission is not a job, so ``claim_next`` cannot reach it
#: however this row is spelled, and the job state machine above is untouched by
#: this whole packet.
SUBMISSION_STATES = ("pending", "released", "declined")


def add_submission(
    conn: sqlite3.Connection,
    *,
    remote_id: int,
    kind: str,
    username: str,
    entry_id: int | None,
    text: str,
    created_utc: str,
) -> int | None:
    """Record one row pulled from the write path. Returns its id, or None.

    None means the row was already here and nothing was written. ``/pull`` is
    at-least-once — the inclusive watermark hands the boundary second back on
    the next pull — so this is called twice with the same ``remote_id`` as a
    matter of course, and the second call must not queue the prompt a second
    time or lift a row the operator has already declined. The UNIQUE index on
    ``remote_id`` is what recognises it and ``DO NOTHING`` is what makes the
    repeat free.

    Everything this writes has been checked by its caller (sync.py): the login
    is a GitHub login, the kind is one of two, the parent exists. The row lands
    ``pending``, which is the only state a person can move it out of.
    """
    cur = conn.execute(
        "INSERT INTO submissions (remote_id, kind, username, entry_id, text, "
        "state, created_utc, pulled_utc) VALUES (?,?,?,?,?,'pending',?,?) "
        "ON CONFLICT (remote_id) DO NOTHING",
        (
            int(remote_id),
            kind,
            username,
            None if entry_id is None else int(entry_id),
            text,
            created_utc,
            utc_now(),
        ),
    )
    # rowcount is 0 when the conflict clause swallowed the insert; lastrowid
    # after that is whatever this connection inserted last, so it is read only
    # when a row really was written.
    return int(cur.lastrowid) if cur.rowcount else None


def pending_submissions(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Submissions waiting for a person, oldest first.

    Oldest first because the queue is a queue: somebody typed the first of
    these before they typed the last, and a review page that shows the newest
    at the top quietly starves the person who was early.
    """
    if int(limit) <= 0:
        return []
    return list(
        conn.execute(
            "SELECT * FROM submissions WHERE state = 'pending' "
            "ORDER BY created_utc, id LIMIT ?",
            (int(limit),),
        ).fetchall()
    )


def submission(conn: sqlite3.Connection, submission_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM submissions WHERE id = ?", (int(submission_id),)
    ).fetchone()


def release_submission(
    conn: sqlite3.Connection, submission_id: int, job_id: int
) -> None:
    """Record that a person released this submission as ``job_id``.

    Only from ``pending``: the ``state`` in the WHERE clause is why a row that
    has already been released cannot be released again by a second click on a
    page left open, or by a CLI running beside the browser. The caller checks
    the state first so that no job is created for a row this would refuse; this
    is the line under that, in the same statement as the write.
    """
    conn.execute(
        "UPDATE submissions SET state = 'released', job_id = ?, decided_utc = ? "
        "WHERE id = ? AND state = 'pending'",
        (int(job_id), utc_now(), int(submission_id)),
    )


def decline_submission(
    conn: sqlite3.Connection, submission_id: int, reason: str
) -> None:
    """A person said no. The row stays, with the reason, as the record.

    Nothing is deleted and the text is not touched: what was asked for is the
    thing worth keeping, and this project does not moderate by forgetting
    (plan §7).
    """
    conn.execute(
        "UPDATE submissions SET state = 'declined', decline_reason = ?, "
        "decided_utc = ? WHERE id = ? AND state = 'pending'",
        (reason, utc_now(), int(submission_id)),
    )


def submission_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many submissions are in each state, zeros included.

    A database that predates migration 010 answers zeros rather than raising,
    the way :func:`get_meta` treats a missing ``meta`` table: the console's tile
    should say nothing on an older file, not stop the whole page.
    """
    empty = {state: 0 for state in SUBMISSION_STATES}
    try:
        rows = conn.execute(
            "SELECT state, COUNT(*) AS c FROM submissions GROUP BY state"
        ).fetchall()
    except sqlite3.OperationalError:
        return empty
    found = {str(row["state"]): int(row["c"]) for row in rows}
    return {state: found.get(state, 0) for state in SUBMISSION_STATES}


# ---------------------------------------------------------------------------
# Activity — migration 008, what the worker is doing right now
# ---------------------------------------------------------------------------

#: How many activity rows are kept. A status channel, not a transcript: the
#: card shows one open step and the three closed ones behind it, and the median
#: is fitted over the newest fifty of a step, so two hundred rows is several
#: hours of a busy worker and still nothing to read or vacuum. Older rows are
#: pruned by :func:`begin_step`, on the process that is already writing.
ACTIVITY_KEEP = 200


def begin_step(
    conn: sqlite3.Connection,
    *,
    step: str,
    headline: str,
    detail: str | None = None,
    job_id: int | None = None,
    entry_id: int | None = None,
    model: str | None = None,
    pid: int | None = None,
) -> int:
    """Open one step, closing whatever this pid had open. Returns its row id.

    One row per pid is open at a time, and the close is part of the same call
    rather than a separate one the caller could forget: the worker's steps abut
    — the sketch is written, then evaluated, then corrected — and a step that
    ended without the next one being asked for is not a thing this loop does.
    The nap is a step too (worker.Worker._nap), so the worker always has a row
    open while it is alive, and an open row whose pid is gone is how the card
    says the worker stopped mid-step.
    """
    pid = os.getpid() if pid is None else int(pid)
    now = utc_now()
    conn.execute(
        "UPDATE activity SET ended_utc = ? WHERE pid = ? AND ended_utc IS NULL",
        (now, pid),
    )
    cur = conn.execute(
        "INSERT INTO activity (step, headline, detail, job_id, entry_id, model, "
        "pid, started_utc, ended_utc) VALUES (?,?,?,?,?,?,?,?,NULL)",
        (
            str(step),
            str(headline),
            detail,
            None if job_id is None else int(job_id),
            None if entry_id is None else int(entry_id),
            model,
            pid,
            now,
        ),
    )
    activity_id = int(cur.lastrowid)
    conn.execute(
        "DELETE FROM activity WHERE id NOT IN "
        "(SELECT id FROM activity ORDER BY id DESC LIMIT ?)",
        (ACTIVITY_KEEP,),
    )
    return activity_id


def update_step(
    conn: sqlite3.Connection,
    activity_id: int,
    *,
    detail: str | None = None,
    entry_id: int | None = None,
    model: str | None = None,
) -> None:
    """Fill in what a step only learns once it is under way.

    Two steps cannot say everything at their own start: ``claiming`` does not
    know which job it will get, and the judge picks its pair internally and
    only names it afterwards, through its log callback. Rather than delay the
    row — which would leave the card blank for exactly the seconds a person is
    watching it — the row opens with what is known and is amended here. Fields
    left None are left alone.
    """
    sets: list[str] = []
    values: list[Any] = []
    if detail is not None:
        sets.append("detail = ?")
        values.append(detail)
    if entry_id is not None:
        sets.append("entry_id = ?")
        values.append(int(entry_id))
    if model is not None:
        sets.append("model = ?")
        values.append(model)
    if not sets:
        return
    values.append(int(activity_id))
    conn.execute(f"UPDATE activity SET {', '.join(sets)} WHERE id = ?", tuple(values))


def end_step(conn: sqlite3.Connection, activity_id: int) -> None:
    """Close one step without opening another.

    The worker never calls this: its steps abut, and the last row is left open
    on purpose so that a worker killed mid-evaluation still says what it was
    doing. It is here for a caller that really does stop — a one-shot run, a
    test — and does not want the card claiming it is still working.
    """
    conn.execute(
        "UPDATE activity SET ended_utc = ? WHERE id = ? AND ended_utc IS NULL",
        (utc_now(), int(activity_id)),
    )


def current_activity(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The newest step still running, or None.

    A database that predates migration 008 answers None rather than raising,
    the way :func:`get_meta` treats a missing ``meta`` table and
    ``worker.idle_summary`` a missing ``judgments``: an older file should
    render a card that says nothing, not a traceback in the browser.
    """
    try:
        return conn.execute(
            "SELECT * FROM activity WHERE ended_utc IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def recent_activity(conn: sqlite3.Connection, limit: int = 3) -> list[sqlite3.Row]:
    """The newest finished steps, newest first. The card's trail."""
    if int(limit) <= 0:
        return []
    try:
        return list(
            conn.execute(
                "SELECT * FROM activity WHERE ended_utc IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


def get_control(conn: sqlite3.Connection) -> Control | None:
    row = conn.execute("SELECT * FROM control WHERE id = 1").fetchone()
    return _row_to(Control, row)


def set_control(
    conn: sqlite3.Connection, state: str, reason: str | None = None
) -> Control:
    """Set the operator's switch: running, pausing (finish the attempt), paused."""
    if state not in CONTROL_STATES:
        raise ValueError(f"unknown control state {state!r}")
    conn.execute(
        "INSERT INTO control (id, state, reason, updated_utc) VALUES (1,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
        "reason = excluded.reason, updated_utc = excluded.updated_utc",
        (state, reason, utc_now()),
    )
    return get_control(conn)


# ---------------------------------------------------------------------------
# Meta — migration 003's key/value scratchpad
# ---------------------------------------------------------------------------


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None):
    """One meta value, or ``default``.

    A database still on migration 001 has no ``meta`` table; that is not an
    error here, it is the answer ``default``. The console reads
    ``worker_started_utc`` through this and treats its absence as "no session
    yet", so an older database renders rather than raising.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row["value"] if row is not None else default


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    """Write one meta value, replacing any previous one."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# Billing — migration 013's daily meter readings
# ---------------------------------------------------------------------------

#: Meta keys the node writes about itself, with no credentials, from the
#: instance metadata service. They are what a billing reading is checked
#: against: a figure read from one tenancy must not be shown as another's.
NODE_IDENTITY_KEYS = (
    "node_tenancy",
    "node_instance_id",
    "node_shape",
    "node_ocpus",
    "node_memory_gb",
    "node_identified_utc",
)


def record_billing(
    conn: sqlite3.Connection,
    tenancy: str,
    rows: Iterable[dict[str, Any]],
    fetched_utc: str | None = None,
) -> int:
    """Write daily meter readings, replacing any earlier reading of the same day.

    Oracle restates the most recent day or two while its meters settle, so a
    re-read of a day is an improvement on the last one and simply overwrites
    it. Days not mentioned by this reading are left alone: a query for
    September must never erase August.

    Returns the number of rows written.
    """
    stamp = fetched_utc or utc_now()
    written = 0
    for row in rows:
        conn.execute(
            "INSERT INTO billing_usage (tenancy, day, service, sku, sku_name, "
            "quantity, unit, amount, currency, fetched_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenancy, day, service, sku) DO UPDATE SET "
            "sku_name = excluded.sku_name, quantity = excluded.quantity, "
            "unit = excluded.unit, amount = excluded.amount, "
            "currency = excluded.currency, fetched_utc = excluded.fetched_utc",
            (
                tenancy,
                str(row["day"])[:10],
                str(row.get("service") or "?"),
                str(row.get("sku") or "?"),
                row.get("sku_name"),
                float(row.get("quantity") or 0.0),
                row.get("unit"),
                float(row.get("amount") or 0.0),
                row.get("currency"),
                stamp,
            ),
        )
        written += 1
    return written


def billing_daily(
    conn: sqlite3.Connection, tenancy: str, since: str | None = None
) -> list[dict[str, Any]]:
    """One row per UTC day: what it cost and how much of the node it metered.

    ``ocpu_hours`` and ``gb_hours`` are pulled out by unit rather than by SKU,
    because the part numbers are Oracle's and may be renumbered, while the
    units are the physical quantity and will not be.
    """
    sql = (
        "SELECT day, SUM(amount) AS amount, "
        "SUM(CASE WHEN unit = 'OCPU Per Hour' THEN quantity ELSE 0 END) AS ocpu_hours, "
        "SUM(CASE WHEN unit = 'Gigabyte Per Hour' THEN quantity ELSE 0 END) AS gb_hours, "
        "MAX(currency) AS currency, MAX(fetched_utc) AS fetched_utc "
        "FROM billing_usage WHERE tenancy = ?"
    )
    args: list[Any] = [tenancy]
    if since:
        sql += " AND day >= ?"
        args.append(since)
    sql += " GROUP BY day ORDER BY day"
    return [dict(row) for row in conn.execute(sql, tuple(args))]


def billing_services(
    conn: sqlite3.Connection, tenancy: str, since: str | None = None
) -> list[dict[str, Any]]:
    """What each service cost over the window, dearest first then by name."""
    sql = (
        "SELECT service, SUM(amount) AS amount, MAX(currency) AS currency "
        "FROM billing_usage WHERE tenancy = ?"
    )
    args: list[Any] = [tenancy]
    if since:
        sql += " AND day >= ?"
        args.append(since)
    sql += " GROUP BY service ORDER BY amount DESC, service"
    return [dict(row) for row in conn.execute(sql, tuple(args))]


def billing_tenancies(conn: sqlite3.Connection) -> list[str]:
    """Every tenancy this database holds readings for, in name order.

    More than one means somebody pointed the query at the wrong account. The
    card says so rather than adding them together.
    """
    return [
        str(row["tenancy"])
        for row in conn.execute(
            "SELECT DISTINCT tenancy FROM billing_usage ORDER BY tenancy"
        )
    ]


# ---------------------------------------------------------------------------
# Inspection, for `sketchgen db status`
# ---------------------------------------------------------------------------


def table_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Every table and its row count, name order. Deterministic."""
    names = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts: list[tuple[str, int]] = []
    for name in names:
        row = conn.execute(f'SELECT COUNT(*) AS c FROM "{name}"').fetchone()
        counts.append((name, int(row["c"])))
    return counts


def job_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Job counts by state, in the state machine's own order, zeros included."""
    rows = conn.execute("SELECT state, COUNT(*) AS c FROM jobs GROUP BY state")
    found = {row["state"]: int(row["c"]) for row in rows}
    return [(state, found.get(state, 0)) for state in TRANSITIONS]
