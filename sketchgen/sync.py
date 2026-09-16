"""Pull the gallery write path into the app database.

The gallery is a static site and cannot write; the node must not listen (spec
§4). So human votes, likes, view counts and submissions land in a small hosted
Worker (``writepath/worker.js``) and this module pulls them down over HTTPS on
the node's own schedule. ``GET /pull`` is the only route the node ever calls,
and this is the only module that calls it.

A submission — a prompt or a critique a signed-in visitor asked for — is the
one kind of row here that a person must still act on. It lands in
``submissions`` and nowhere near ``jobs``; releasing it is the operator's, on
/submissions (plan §4.4).

What crosses the wire about a person is a GitHub username. Nothing here reads,
accepts or stores anything else: rows carrying an unusable login are counted as
skipped rather than written.

Delivery is at-least-once. The Worker's watermark is inclusive, so rows on the
boundary second arrive again on the next pull; every write below is an upsert,
so applying the same payload twice changes nothing. Stdlib only, Python 3.12.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import db

__all__ = [
    "DEFAULT_TIMEOUT",
    "DEFAULT_TOKEN_FILE",
    "DEFAULT_URL",
    "EPOCH",
    "JUDGE_KIND",
    "PROMPT_VERSION",
    "SINCE_KEY",
    "SUBMISSION_KINDS",
    "SyncFailed",
    "SyncRefused",
    "apply_changes",
    "fetch_changes",
    "get_since",
    "read_token",
    "run_once",
    "set_since",
]

#: The watermark's row in ``sync_state`` (migrations/002_sync.sql).
SINCE_KEY = "writepath_since"

#: What a pull asks for when the node has never pulled before.
EPOCH = "1970-01-01T00:00:00Z"

#: Every row this module writes into ``judgments`` is a human judgment made on
#: the gallery's compare page, under the prompt that page showed.
JUDGE_KIND = "human"
PROMPT_VERSION = "gallery-v1"

DEFAULT_URL = os.environ.get("SKETCHGEN_WRITEPATH_URL", "")
USER_AGENT = "sketchgen-sync/1.0 (+https://github.com/profcarroll/sketchgen)"
DEFAULT_TOKEN_FILE = str(Path.home() / "sketchgen" / "writepath.token")
DEFAULT_TIMEOUT = 30.0

#: GitHub logins: alphanumerics and single hyphens, 39 characters at most.
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

QUESTIONS = frozenset({"brief", "look"})
CHOICES = frozenset({"A", "B", "tie"})

#: The two things a visitor can ask for. Anything else is a row from a Worker
#: this node does not understand, and it is skipped rather than stored: the
#: CHECK constraint in migration 010 would refuse it anyway, and a pull that
#: raises stops the rows behind it from landing.
SUBMISSION_KINDS = frozenset({"prompt", "critique"})


class SyncRefused(Exception):
    """The pull was not attempted: no token, a loose token file, no URL."""


class SyncFailed(Exception):
    """The pull was attempted and did not produce a usable payload."""


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def read_token(path: str | os.PathLike[str]) -> str:
    """Read the pull token from ``path``, refusing a file anyone else can read.

    The token is never an argument: an argv value is visible to every process on
    the box in ``/proc``, and this one is a bearer credential. Mode must be 0600
    (nothing set for group or other), which is the same bar the deploy key gets.
    """
    target = Path(os.path.expanduser(str(path)))
    try:
        info = target.stat()
    except FileNotFoundError:
        raise SyncRefused(f"token file missing: {target}") from None
    except OSError as exc:
        raise SyncRefused(f"token file unreadable: {target}: {exc}") from None
    if not stat.S_ISREG(info.st_mode):
        raise SyncRefused(f"token file is not a regular file: {target}")
    if info.st_mode & 0o077:
        raise SyncRefused(
            f"token file {target} is mode {info.st_mode & 0o777:04o}; "
            "it must be 0600 (chmod 600 it, then try again)"
        )
    try:
        token = target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SyncRefused(f"token file unreadable: {target}: {exc}") from None
    if not token:
        raise SyncRefused(f"token file is empty: {target}")
    return token


# ---------------------------------------------------------------------------
# The watermark
# ---------------------------------------------------------------------------


def get_since(conn: sqlite3.Connection) -> str:
    """The watermark the next pull should ask for, EPOCH if there is none."""
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?", (SINCE_KEY,)
    ).fetchone()
    value = row["value"] if row else None
    return value if value and UTC_RE.match(value) else EPOCH


def set_since(conn: sqlite3.Connection, value: str) -> None:
    """Move the watermark forward. Never backward: a late payload cannot rewind
    the cursor and make the node re-apply a week of rows."""
    if not UTC_RE.match(value):
        raise SyncFailed(f"next_since is not a UTC ISO 8601 timestamp: {value!r}")
    if value < get_since(conn):
        return
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SINCE_KEY, value),
    )


# ---------------------------------------------------------------------------
# The pull
# ---------------------------------------------------------------------------


def fetch_changes(
    url: str, token: str, since: str, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """``GET {url}/pull?since=...`` with the bearer token, decoded as JSON."""
    base = url.rstrip("/")
    target = f"{base}/pull?" + urllib.parse.urlencode({"since": since})
    request = urllib.request.Request(
        target,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            # Cloudflare's browser integrity check answers 403 (error 1010) to
            # Python-urllib's default agent; a named agent passes. Measured.
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise SyncFailed(f"pull returned HTTP {exc.code} from {base}/pull") from None
    except (urllib.error.URLError, OSError) as exc:
        raise SyncFailed(f"pull could not reach {base}/pull: {exc}") from None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncFailed(f"pull returned something that is not JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise SyncFailed("pull returned JSON that is not an object")
    return payload


# ---------------------------------------------------------------------------
# Applying a payload
# ---------------------------------------------------------------------------

_UPSERT_JUDGMENT = (
    "INSERT INTO judgments (entry_a, entry_b, judge_kind, judge_id, question, "
    "choice, prompt_version, artefact_hash, created_utc) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?) "
    "ON CONFLICT (judge_kind, judge_id, entry_a, entry_b, question) "
    "DO UPDATE SET choice = excluded.choice"
)

_INSERT_LIKE = (
    "INSERT INTO likes (entry_id, username, created_utc) VALUES (?, ?, ?) "
    "ON CONFLICT (entry_id, username) DO NOTHING"
)

_DELETE_LIKE = "DELETE FROM likes WHERE entry_id = ? AND username = ?"

_UPSERT_VIEWS = (
    "INSERT INTO engagement (entry_id, views) VALUES (?, ?) "
    "ON CONFLICT (entry_id) DO UPDATE SET views = MAX(engagement.views, excluded.views)"
)


def _known_entries(conn: sqlite3.Connection) -> set[int]:
    return {int(row["id"]) for row in conn.execute("SELECT id FROM entries")}


def _entry_id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _login(value: Any) -> str | None:
    return value if isinstance(value, str) and LOGIN_RE.match(value) else None


def _stamp(value: Any) -> str | None:
    return value if isinstance(value, str) and UTC_RE.match(value) else None


def apply_changes(
    conn: sqlite3.Connection, payload: dict[str, Any]
) -> dict[str, int]:
    """Write one /pull payload into the app database, inside one transaction.

    Rows naming an entry this node has never published, or carrying anything
    that is not a GitHub login, are skipped and counted rather than written.
    Returns counts: votes, likes, unlikes, views, submissions, skipped.
    """
    counts = {
        "votes": 0,
        "likes": 0,
        "unlikes": 0,
        "views": 0,
        "submissions": 0,
        "skipped": 0,
    }
    conn.execute("BEGIN IMMEDIATE")
    try:
        entries = _known_entries(conn)

        for row in payload.get("votes") or []:
            if not isinstance(row, dict):
                counts["skipped"] += 1
                continue
            a = _entry_id(row.get("entry_a"))
            b = _entry_id(row.get("entry_b"))
            username = _login(row.get("username"))
            created = _stamp(row.get("created_utc")) or _stamp(row.get("updated_utc"))
            question = row.get("question")
            choice = row.get("choice")
            if (
                a is None or b is None or username is None or created is None
                or question not in QUESTIONS or choice not in CHOICES
                or a not in entries or b not in entries
            ):
                counts["skipped"] += 1
                continue
            conn.execute(
                _UPSERT_JUDGMENT,
                (a, b, JUDGE_KIND, username, question, choice, PROMPT_VERSION, created),
            )
            counts["votes"] += 1

        for row in payload.get("likes") or []:
            if not isinstance(row, dict):
                counts["skipped"] += 1
                continue
            entry_id = _entry_id(row.get("entry_id"))
            username = _login(row.get("username"))
            created = _stamp(row.get("created_utc")) or _stamp(row.get("updated_utc"))
            if entry_id is None or username is None or created is None or entry_id not in entries:
                counts["skipped"] += 1
                continue
            # `active` is the Worker's on/off flag; 0 means the like was taken
            # back, and the node's row goes with it (writepath/schema.sql).
            if row.get("active", 1):
                conn.execute(_INSERT_LIKE, (entry_id, username, created))
                counts["likes"] += 1
            else:
                conn.execute(_DELETE_LIKE, (entry_id, username))
                counts["unlikes"] += 1

        for row in payload.get("views") or []:
            if not isinstance(row, dict):
                counts["skipped"] += 1
                continue
            entry_id = _entry_id(row.get("entry_id"))
            total = row.get("count")
            if (
                entry_id is None
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 0
                or entry_id not in entries
            ):
                counts["skipped"] += 1
                continue
            conn.execute(_UPSERT_VIEWS, (entry_id, total))
            counts["views"] += 1

        for row in payload.get("submissions") or []:
            if not isinstance(row, dict):
                counts["skipped"] += 1
                continue
            # The write path's own id. `_entry_id` asks only "a positive
            # integer", which is what that id is too.
            remote_id = _entry_id(row.get("id"))
            username = _login(row.get("username"))
            created = _stamp(row.get("created_utc")) or _stamp(row.get("updated_utc"))
            kind = row.get("kind")
            # Collapsed here and stored collapsed: the sentence becomes a prompt
            # or a revision line, and both of those are one line by the time
            # lineage composes them.
            text = " ".join(str(row.get("text") or "").split())
            parent = _entry_id(row.get("entry_id"))
            if (
                remote_id is None
                or username is None
                or created is None
                or kind not in SUBMISSION_KINDS
                or not text
            ):
                counts["skipped"] += 1
                continue
            # The Worker has no entries table and can only check that the
            # parent id is a positive integer (plan §3.2). This node has the
            # table, so a critique of an entry it has never published is
            # dropped here rather than held for an operator who could do
            # nothing with it.
            if kind == "critique" and (parent is None or parent not in entries):
                counts["skipped"] += 1
                continue
            db.add_submission(
                conn,
                remote_id=remote_id,
                kind=kind,
                username=username,
                # The kind decides, not the payload: a prompt has no parent,
                # whatever else arrived in the row.
                entry_id=parent if kind == "critique" else None,
                text=text,
                created_utc=created,
            )
            counts["submissions"] += 1

        watermark = payload.get("next_since")
        if isinstance(watermark, str) and UTC_RE.match(watermark):
            set_since(conn, watermark)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return counts


def run_once(
    conn: sqlite3.Connection,
    url: str,
    token: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """One pull and one apply. Returns the counts plus both watermarks."""
    since = get_since(conn)
    payload = fetch_changes(url, token, since, timeout=timeout)
    counts = apply_changes(conn, payload)
    return {
        "since": since,
        "next_since": get_since(conn),
        "pulled_utc": db.utc_now(),
        **counts,
    }
