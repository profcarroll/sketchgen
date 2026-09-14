"""Publication: move one entry from the holding pen into the public gallery.

Publishing is a git commit and a push, and nothing else. The entry's files land
in ``e/<entry_id>/`` inside a checkout of the gallery repository, the commit is
made by that checkout's own git identity, and the push goes over the scoped,
write-only deploy key (``sketchgen keygen gallery``, docs/OPERATIONS.md). Only
when the push has succeeded does the database row become ``published`` and
record the commit the site now serves. A publish that fails leaves the checkout
and the database exactly as it found them.

Two rules this module enforces rather than trusts:

  - **A person decides.** DECIDE[publication-gate] is *hold for a person*
    (spec section 9), so the entry must be ``held`` or ``failed-kept`` before
    anything is committed, and the username of whoever clicked is written into
    the commit as ``Published-By:``.
  - **No personal data reaches a public repository.** Every file about to be
    committed is scanned for an email-shaped string and for the node's
    ``instance-`` hostname token, and one hit refuses the whole publish
    (CLAUDE.md; spec section 7).

Attribution follows course policy (ATTRIBUTION.md): the gallery commit names
the executor model that actually wrote the sketch, taken from the entry's own
``executor`` column, as a ``Co-Authored-By:`` trailer.

Python 3.12, stdlib only; git is driven through subprocess. Every timestamp is
UTC, ISO 8601, with a Z.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import db

__all__ = [
    "DEFAULT_GALLERY_DIR",
    "DEFAULT_KEY_PATH",
    "DEFAULT_URL_BASE",
    "PUBLISHABLE",
    "PublishFailed",
    "PublishRefused",
    "Plan",
    "Published",
    "entry_url",
    "gallery_checkout",
    "publish",
    "reject",
    "scan_for_personal_data",
]

#: The states a person may publish from. ``failed-kept`` is here on purpose:
#: a sketch the gate failed is still a result, and the gallery shows it as one.
PUBLISHABLE = frozenset({"held", "failed-kept"})

DEFAULT_GALLERY_DIR = os.environ.get(
    "SKETCHGEN_GALLERY", str(Path.home() / "sketchgen" / "gallery")
)
DEFAULT_KEY_PATH = os.environ.get(
    "SKETCHGEN_GALLERY_KEY", str(Path.home() / ".ssh" / "sketchgen-gallery")
)
DEFAULT_URL_BASE = os.environ.get(
    "SKETCHGEN_GALLERY_URL", "https://profcarroll.github.io/sketchgen-gallery"
)

#: What must never be committed to a public repository.
EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BINARY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".wav", ".woff", ".woff2", ".ico"})
HOSTNAME_TOKEN = b"instance-"

#: Shown instead of the deploy key's path under --dry-run.
MASK = "<key>"

SSH_REMOTE_RE = re.compile(r"^(ssh://|[^/:]+@[^/:]+:)")


class PublishRefused(Exception):
    """The publish was refused before anything changed. Exit 3."""


class PublishFailed(Exception):
    """The publish was attempted and failed. Exit 1, nothing left behind."""


@dataclass
class Plan:
    """What a --dry-run would do, and what a real publish is about to do."""

    entry_id: int
    state: str
    source_dir: str
    files: list[str]
    dest: str
    message: str
    push_command: str
    url: str


@dataclass
class Published:
    """The result of a completed publish."""

    entry_id: int
    commit: str
    branch: str
    url: str
    published_utc: str
    files: list[str] = field(default_factory=list)
    index_commit: str | None = None   # the gallery index re-rendered after this entry
    index_note: str | None = None     # why there is no index commit, when there is none


# ---------------------------------------------------------------------------
# git, as a subprocess
# ---------------------------------------------------------------------------


def _git(
    cwd: str | os.PathLike[str],
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one git command in ``cwd``. Never raises on a non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _git_out(cwd: str | os.PathLike[str], *args: str) -> str | None:
    result = _git(cwd, *args)
    return result.stdout.strip() if result.returncode == 0 else None


def _default_branch(gallery_dir: Path) -> str | None:
    """The branch the gallery repository considers its default.

    ``refs/remotes/origin/HEAD`` is what ``git clone`` wrote down, so it is the
    honest answer. A checkout that has none (a repository made locally, as in
    the tests before the first push) falls back to the conventional names.
    """
    head = _git_out(gallery_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head.split("/", 1)[1] if "/" in head else head
    current = _git_out(gallery_dir, "rev-parse", "--abbrev-ref", "HEAD")
    if current in ("main", "master"):
        return current
    return None


def gallery_checkout(gallery_dir: str | os.PathLike[str]) -> tuple[Path, str]:
    """Check the gallery checkout and return it with its default branch.

    Refuses unless it is a git work tree, its tree is clean, and it is sitting
    on its default branch — publishing from a dirty or detached checkout would
    commit whatever else is lying around.
    """
    path = Path(os.path.expanduser(str(gallery_dir)))
    if not path.is_dir():
        raise PublishRefused(f"no gallery checkout at {path}")
    inside = _git_out(path, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        raise PublishRefused(f"{path} is not a git repository")
    status = _git(path, "status", "--porcelain")
    if status.returncode != 0:
        raise PublishRefused(f"{path}: git status failed: {status.stderr.strip()}")
    if status.stdout.strip():
        raise PublishRefused(
            f"{path} has uncommitted changes; commit or clean it before publishing"
        )
    branch = _default_branch(path)
    current = _git_out(path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        raise PublishRefused(
            f"{path}: cannot tell which branch is the default "
            "(no refs/remotes/origin/HEAD, and HEAD is not main or master)"
        )
    if current != branch:
        raise PublishRefused(
            f"{path} is on {current}, not its default branch {branch}"
        )
    return path, branch


# ---------------------------------------------------------------------------
# The entry, its files, and the no-personal-data guard
# ---------------------------------------------------------------------------


def _entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row:
    try:
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    except sqlite3.Error as exc:
        raise PublishRefused(f"cannot read entry {entry_id}: {exc}") from exc
    if row is None:
        raise PublishRefused(f"no entry {entry_id}")
    return row


def _source_files(source: Path) -> list[str]:
    """Every file under ``source``, as paths relative to it, in sorted order."""
    return sorted(
        str(item.relative_to(source))
        for item in source.rglob("*")
        if item.is_file()
    )


def scan_for_personal_data(source: Path) -> None:
    """Refuse if any file carries an email address or the node's hostname token.

    The gallery repository is public by construction (spec section 4), so this
    runs on the bytes that are about to be committed, not on a sample of them.
    """
    for relative in _source_files(source):
        path = source / relative
        # Only text is scanned. A PNG's bytes matched the email pattern by
        # chance on the node (entry 5, 2026-09-14) and refused a real publish;
        # an image cannot carry an address a reader would see anyway.
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            continue  # not text, whatever its name
        if EMAIL_RE.search(data):
            raise PublishRefused(
                f"{relative} contains an email-shaped string; the gallery is "
                "public and this repo's rule is GitHub usernames only"
            )
        if HOSTNAME_TOKEN in data:
            raise PublishRefused(
                f"{relative} contains 'instance-'; that is the node's hostname "
                "and it does not go into a public repository"
            )


def _render_with_generator(conn: sqlite3.Connection, entry_id: int, dest: Path) -> Path:
    """Ask packet 3.1's generator for the entry's files, or refuse.

    This module never invents an entry's files: either the caller passes a
    directory with --from, or the generator produces one.
    """
    try:
        from . import gallery  # noqa: PLC0415 - optional, lands in packet 3.1
    except ImportError as exc:
        raise PublishRefused("generator not present; pass --from") from exc
    render = getattr(gallery, "render_entry", None)
    if render is None:
        raise PublishRefused("generator not present; pass --from")
    try:
        # The generator takes the gallery ROOT and writes dest/e/<id>/, which it
        # returns; that returned directory is the entry, and it is what we copy.
        out = render(conn, entry_id, dest, publishing=True)
    except Exception as exc:  # the generator's own refusals, reported, not raised
        raise PublishRefused(f"generator refused entry {entry_id}: {exc}") from exc
    return Path(out) if out else dest / "e" / str(entry_id)


# ---------------------------------------------------------------------------
# The commit
# ---------------------------------------------------------------------------


def _subject(entry: sqlite3.Row) -> str:
    prompt = " ".join((entry["prompt"] or "").split())
    return f"entry {entry['id']}: {prompt[:60]}"


def _message(entry: sqlite3.Row, model: str, by: str) -> str:
    """Subject, then the trailers. Attribution is course policy, not courtesy."""
    return (
        f"{_subject(entry)}\n"
        "\n"
        f"Co-Authored-By: {model} <noreply@localhost>\n"
        f"Published-By: {by}\n"
    )


def _executor_model(entry: sqlite3.Row, model: str | None) -> str:
    if model:
        return model
    recorded = entry["executor"]
    if not recorded:
        raise PublishRefused(
            f"entry {entry['id']} records no executor model; ATTRIBUTION.md "
            "requires the commit to name one — pass --model"
        )
    return str(recorded)


def entry_url(entry_id: int, base: str = DEFAULT_URL_BASE) -> str:
    return f"{base.rstrip('/')}/e/{entry_id}/"


def _is_ssh_remote(remote: str) -> bool:
    if remote.startswith("file://") or remote.startswith("/"):
        return False
    return bool(SSH_REMOTE_RE.match(remote))


def _push_env(key: str | None, remote: str) -> tuple[dict[str, str] | None, str]:
    """The environment for the push, and the command line to show a person."""
    if not _is_ssh_remote(remote):
        return None, ""
    if not key:
        raise PublishRefused(f"{remote} is an ssh remote but no deploy key was given")
    key_path = Path(os.path.expanduser(key))
    if not key_path.is_file():
        raise PublishRefused(
            f"no deploy key at {key_path} (make one with: sketchgen keygen gallery)"
        )
    ssh = (
        f"ssh -i {key_path} -o IdentitiesOnly=yes "
        "-o StrictHostKeyChecking=accept-new"
    )
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = ssh
    masked = (
        f"GIT_SSH_COMMAND='ssh -i {MASK} -o IdentitiesOnly=yes "
        "-o StrictHostKeyChecking=accept-new' "
    )
    return env, masked


# ---------------------------------------------------------------------------
# publish and reject
# ---------------------------------------------------------------------------


def publish(
    conn: sqlite3.Connection,
    entry_id: int,
    *,
    gallery_dir: str | os.PathLike[str] = DEFAULT_GALLERY_DIR,
    from_dir: str | os.PathLike[str] | None = None,
    key: str | None = DEFAULT_KEY_PATH,
    remote: str | None = None,
    by: str | None = None,
    model: str | None = None,
    url_base: str = DEFAULT_URL_BASE,
    dry_run: bool = False,
) -> Plan | Published:
    """Commit ``e/<entry_id>/`` to the gallery checkout and push it.

    Returns a :class:`Plan` under ``dry_run`` (nothing is changed) and a
    :class:`Published` otherwise. Raises :class:`PublishRefused` (exit 3) when
    the entry, the checkout or the files are not fit to publish, and
    :class:`PublishFailed` (exit 1) when the push fails — in which case the
    local commit is undone and the database is untouched.
    """
    entry = _entry(conn, entry_id)
    if entry["state"] not in PUBLISHABLE:
        raise PublishRefused(
            f"entry {entry_id} is {entry['state']}; only "
            f"{' or '.join(sorted(PUBLISHABLE))} can be published"
        )
    executor_model = _executor_model(entry, model)
    published_by = by or _os_user()
    checkout, branch = gallery_checkout(gallery_dir)
    target = remote or _git_out(checkout, "remote", "get-url", "origin")
    if not target:
        raise PublishRefused(
            f"{checkout} has no 'origin' remote and no --remote was given"
        )

    temporary: str | None = None
    if from_dir is not None:
        source = Path(os.path.expanduser(str(from_dir)))
        if not source.is_dir():
            raise PublishRefused(f"no source directory at {source}")
    else:
        temporary = tempfile.mkdtemp(prefix="sketchgen-publish-")
        source = Path(temporary)
        try:
            source = _render_with_generator(conn, entry_id, source)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    try:
        files = _source_files(source)
        if not files:
            raise PublishRefused(f"{source} is empty; there is nothing to publish")
        scan_for_personal_data(source)
        message = _message(entry, executor_model, published_by)
        env, masked = _push_env(key, target)
        push_display = f"{masked}git push {target} HEAD:refs/heads/{branch}"
        dest = checkout / "e" / str(entry_id)

        if dry_run:
            return Plan(
                entry_id=entry_id,
                state=entry["state"],
                source_dir=str(source),
                files=files,
                dest=str(dest),
                message=message,
                push_command=push_display,
                url=entry_url(entry_id, url_base),
            )

        before = _git_out(checkout, "rev-parse", "HEAD")
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest)

        added = _git(checkout, "add", "--", f"e/{entry_id}")
        if added.returncode != 0:
            raise PublishFailed(f"git add failed: {added.stderr.strip()}")
        if _git(checkout, "diff", "--cached", "--quiet").returncode == 0:
            raise PublishRefused(
                f"nothing to commit: e/{entry_id} is already in the gallery "
                "with these exact bytes"
            )
        committed = subprocess.run(
            ["git", "commit", "-F", "-"],
            cwd=str(checkout),
            input=message,
            capture_output=True,
            text=True,
            check=False,
        )
        if committed.returncode != 0:
            raise PublishFailed(f"git commit failed: {committed.stderr.strip()}")
        sha = _git_out(checkout, "rev-parse", "HEAD")
        if not sha:
            raise PublishFailed("git rev-parse HEAD failed after the commit")

        pushed = _git(
            checkout, "push", target, f"HEAD:refs/heads/{branch}", env=env
        )
        if pushed.returncode != 0:
            _undo(checkout, before)
            raise PublishFailed(pushed.stderr.strip() or "git push failed")

        now = db.utc_now()
        # Only a held entry becomes 'published'. A kept rejection stays
        # 'failed-kept' — that is what puts it on the rejections page rather
        # than the grid — and gains the timestamp and commit like any other.
        new_state = "published" if entry["state"] == "held" else entry["state"]
        conn.execute(
            "UPDATE entries SET state = ?, published_utc = ?, "
            "publish_commit = ? WHERE id = ?",
            (new_state, now, sha, entry_id),
        )
        # The job's row follows its entry, so the queue stops saying "held"
        # about work that is public. A job already past held is left alone.
        if new_state == "published" and entry["job_id"] is not None:
            job = db.get_job(conn, entry["job_id"])
            if job is not None and job.state == "held":
                db.transition(conn, job.id, "published")
        conn.commit()
        # The entry is public. Now the grid, the failures page, compare and the
        # line pages must know about it: re-render the index and push it as a
        # second commit. This never fails the publish; the entry is already up.
        index_sha, index_note = _publish_index(conn, checkout, branch, target, env, entry_id)
        return Published(
            entry_id=entry_id,
            commit=sha,
            branch=branch,
            url=entry_url(entry_id, url_base),
            published_utc=now,
            files=files,
            index_commit=index_sha,
            index_note=index_note,
        )
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def publish_index(
    conn: sqlite3.Connection,
    gallery_dir: str | os.PathLike[str],
    *,
    key: str | None = None,
    remote: str | None = None,
    write_path: str | None = None,
) -> tuple[str | None, str | None]:
    """Re-render every published entry and the index, commit and push.

    ``write_path`` overrides the gallery write-path URL recorded in the
    checkout's config.json; the re-render carries it into every page.

    For template or asset changes: no entry's state changes. Returns
    (sha, None) on success or (None, why); refuses on a dirty checkout.
    """
    checkout, branch = gallery_checkout(gallery_dir)
    target = remote or _git_out(checkout, "remote", "get-url", "origin")
    if not target:
        raise PublishRefused(f"{checkout} has no 'origin' remote and no --remote was given")
    env, _ = _push_env(key, target)
    try:
        from . import gallery  # noqa: PLC0415
    except ImportError as exc:
        raise PublishRefused("generator not present") from exc
    before = _git_out(checkout, "rev-parse", "HEAD")
    config = gallery.Config.load(checkout)
    if write_path is not None:
        config = gallery.Config(
            write_path=write_path.rstrip("/"),
            gallery_url=config.gallery_url,
            repository=config.repository,
        )
    # Only what a person has published: a kept rejection without a
    # published_utc is still waiting for that decision (spec §9), and a
    # re-render is not the place it gets made.
    rows = conn.execute(
        "SELECT id FROM entries WHERE state IN ('published', 'failed-kept') "
        "AND published_utc IS NOT NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        gallery.render_entry(conn, row["id"], checkout, config)
    gallery.render_index(conn, checkout, config)
    _git(checkout, "rm", "-q", "--ignore-unmatch", "--", "failed.html")  # renamed to rejections.html
    # Everything in the checkout is generated output (the generator also writes
    # files INDEX_PATHS does not list, pairs.json for one), so stage it all.
    added = _git(checkout, "add", "-A", "--", ".")
    if added.returncode != 0:
        return None, f"git add failed: {added.stderr.strip()}"
    if _git(checkout, "diff", "--cached", "--quiet").returncode == 0:
        return None, "site unchanged"
    committed = subprocess.run(
        ["git", "commit", "-F", "-"],
        cwd=str(checkout),
        input="gallery: re-render every page\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if committed.returncode != 0:
        return None, f"commit failed: {committed.stderr.strip()}"
    sha = _git_out(checkout, "rev-parse", "HEAD")
    pushed = _git(checkout, "push", target, f"HEAD:refs/heads/{branch}", env=env)
    if pushed.returncode != 0:
        _undo(checkout, before)
        return None, f"push failed: {pushed.stderr.strip() or 'git push failed'}"
    return sha, None


INDEX_PATHS = ("index.html", "rejections.html", "compare.html", "lines", "assets", "config.json")


def _publish_index(
    conn: sqlite3.Connection,
    checkout: Path,
    branch: str,
    target: str,
    env: dict[str, str] | None,
    entry_id: int,
) -> tuple[str | None, str | None]:
    """Re-render the gallery index into the checkout, commit and push it.

    Returns (sha, None) on success, (None, why) otherwise. A failure here is
    reported, never raised: the entry itself is already published.
    """
    try:
        from . import gallery  # noqa: PLC0415
    except ImportError:
        return None, "generator not present; index not re-rendered"
    render_index = getattr(gallery, "render_index", None)
    if render_index is None:
        return None, "generator has no render_index; index not re-rendered"
    before = _git_out(checkout, "rev-parse", "HEAD")
    try:
        render_index(conn, checkout)
    except Exception as exc:  # the generator's own refusal, reported
        return None, f"index render failed: {exc}"
    if not any((checkout / p).exists() for p in INDEX_PATHS):
        return None, "index render wrote nothing"
    added = _git(checkout, "add", "-A", "--", ".")
    if added.returncode != 0:
        return None, f"git add of the index failed: {added.stderr.strip()}"
    if _git(checkout, "diff", "--cached", "--quiet").returncode == 0:
        return None, "index unchanged"
    committed = subprocess.run(
        ["git", "commit", "-F", "-"],
        cwd=str(checkout),
        input=f"gallery index after entry {entry_id}\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if committed.returncode != 0:
        return None, f"index commit failed: {committed.stderr.strip()}"
    sha = _git_out(checkout, "rev-parse", "HEAD")
    # Seen once on the node (2026-09-14, entry 4 from the operator UI): the
    # checkout was left with a modified pairs.json after this commit, which
    # made the next publish refuse as dirty. Not reproduced from the CLI. A
    # second pass commits any leftover generated file and says so, rather
    # than leaving a landmine for the next publish.
    leftover = (_git_out(checkout, "status", "--porcelain") or "").strip()
    if leftover:
        _git(checkout, "add", "-A", "--", ".")
        second = subprocess.run(
            ["git", "commit", "-F", "-"],
            cwd=str(checkout),
            input=f"gallery index after entry {entry_id}, second pass\n\n{leftover}\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if second.returncode == 0:
            sha = _git_out(checkout, "rev-parse", "HEAD")
            print(f"sketchgen: index second pass committed: {leftover.replace(chr(10), '; ')}", file=sys.stderr)
    pushed = _git(checkout, "push", target, f"HEAD:refs/heads/{branch}", env=env)
    if pushed.returncode != 0:
        _undo(checkout, before)
        return None, f"index push failed: {pushed.stderr.strip() or 'git push failed'}"
    return sha, None


def _undo(checkout: Path, before: str | None) -> None:
    """Put the checkout back where it was before the failed publish."""
    if before:
        _git(checkout, "reset", "--hard", before)
    else:  # pragma: no cover - a gallery with no commit at all
        _git(checkout, "reset", "--hard", "HEAD~1")


def _os_user() -> str:
    """Who is running this, for ``Published-By:``. A username, never a name."""
    import getpass  # noqa: PLC0415 - only needed for the default

    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry, no LOGNAME
        return "unknown"


def reject(
    conn: sqlite3.Connection, entry_id: int, reason: str | None = None
) -> str:
    """Mark a held entry rejected. Touches no git repository and no files.

    The schema has no column for a reason, so an explanatory ``--reason`` is
    recorded in the originating job's ``last_error`` — the one free-text field
    that belongs to the same piece of work — and returned for printing.
    """
    entry = _entry(conn, entry_id)
    if entry["state"] != "held":
        raise PublishRefused(
            f"entry {entry_id} is {entry['state']}; only held can be rejected"
        )
    conn.execute("UPDATE entries SET state = 'rejected' WHERE id = ?", (entry_id,))
    job = db.get_job(conn, entry["job_id"]) if entry["job_id"] is not None else None
    if job is not None and job.state == "held":
        db.transition(conn, job.id, "rejected", last_error=f"entry rejected: {reason or ''}".rstrip(": "))
    if reason:
        conn.execute(
            "UPDATE jobs SET last_error = ?, updated_utc = ? WHERE id = ?",
            (f"entry rejected: {reason}", db.utc_now(), entry["job_id"]),
        )
    return reason or ""
