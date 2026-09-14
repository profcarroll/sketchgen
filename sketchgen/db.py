"""SQLite access for sketchgen: connection, migrations, typed helpers, and the
job state machine.

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
    "DEFAULT_DB_PATH",
    "TRANSITIONS",
    "IllegalTransition",
    "UnknownJob",
    "Attempt",
    "Control",
    "Job",
    "add_attempt",
    "add_lineage",
    "claim_next",
    "connect",
    "create_entry",
    "enqueue",
    "get_control",
    "get_job",
    "init",
    "list_jobs",
    "record_judgment",
    "requeue",
    "schema_version",
    "set_control",
    "transition",
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
    "planning": frozenset({"executing", "needs-laptop", "failed"}),
    "executing": frozenset({"gating", "failed", "queued"}),
    "gating": frozenset({"held", "repairing", "failed", "queued"}),
    "repairing": frozenset({"executing", "needs-laptop", "failed", "queued"}),
    "needs-laptop": frozenset({"planning", "executing", "repairing", "failed"}),
    "held": frozenset({"published", "rejected"}),
    # terminal
    "published": frozenset(),
    "rejected": frozenset(),
    "failed": frozenset(),
}

#: States a job may be requeued from (the stop-now path).
REQUEUABLE = frozenset({"executing", "gating", "repairing"})

CONTROL_STATES = frozenset({"running", "pausing", "paused"})


class IllegalTransition(Exception):
    """A job was asked to move to a state that is not a legal successor."""


class UnknownJob(Exception):
    """No job with that id."""


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
    """
    _ensure_schema_version_table(conn)
    done = {row["version"] for row in conn.execute("SELECT version FROM schema_version")}
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
