"""Publication: move one entry from the holding pen into the public gallery.

Publishing is a git commit and a push, and nothing else. The entry's files land
in ``e/<entry_id>/`` inside a checkout of the gallery repository, the commit is
made by that checkout's own git identity, and the push goes over the scoped,
write-only deploy key (``sketchgen keygen gallery``, docs/OPERATIONS.md). Only
when the push has succeeded does the database row become ``published`` and
record the commit the site now serves. A publish that fails leaves the checkout
and the database exactly as it found them.

:func:`publish_many` publishes several entries in one go — one commit each,
then a single push and a single index pass for all of them. It is the same
work in the same order, not a second implementation of it: the shared steps
live in ``_stage_and_commit`` and ``_stamp_row``, and :func:`publish` is a
batch of one. The rule above is what the batch is careful about, so it holds
per entry: a row becomes ``published`` only after the push that carried its
commit has succeeded, and a push that fails leaves every entry in the batch
exactly as it found them.

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

import contextlib
import fcntl
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import db
from . import webimg

__all__ = [
    "DEFAULT_GALLERY_DIR",
    "DEFAULT_KEY_PATH",
    "DEFAULT_URL_BASE",
    "PUBLISHABLE",
    "ManyResult",
    "PublishFailed",
    "PublishRefused",
    "Plan",
    "Published",
    "entry_url",
    "gallery_checkout",
    "publish",
    "publish_many",
    "reject",
    "scan_for_personal_data",
]

#: The states a person may publish from. ``failed-kept`` is here on purpose:
#: a sketch the gate failed is still a result, and the gallery shows it as one.
#: ``rejected`` joined them in the lineage ledger's §5.2, for the same reason
#: read from the other end: a sketch a *person* refused is also a result, and
#: rejecting now means publishing it to the rejections catalog with the reason.
#: The entry keeps its state through the publish; only ``held`` becomes
#: ``published``.
PUBLISHABLE = frozenset({"held", "failed-kept", "rejected"})

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


@dataclass
class ManyResult:
    """What one batch of publications did, entry by entry.

    A batch is not all-or-nothing, because the three ways it can go wrong are
    not the same thing. ``refused`` is an entry the publisher would not touch
    (wrong state, the personal-data scan, the generator, or bytes already in
    the gallery); it was dropped before its commit and the rest of the batch
    carried on. ``failed`` is the one push that carried every commit going
    wrong under all of them at once: those entries are still held, their
    commits are gone, and nothing about them is public. ``published`` is in the
    order the commits were made, and the index is the batch's, not any one
    entry's — hence the two fields here rather than on each
    :class:`Published`.
    """

    published: list[Published]
    refused: dict[int, str] = field(default_factory=dict)
    failed: dict[int, str] = field(default_factory=dict)
    index_commit: str | None = None
    index_note: str | None = None


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


#: How long a publisher waits for another one to finish with the checkout
#: before it gives up. A publish is a render, two commits and two pushes over
#: the network; a minute or two is a slow one, and anything past this is not a
#: queue any more, it is a process that died holding the lock.
#:
#: :func:`publish_many` holds the same lock for a whole batch, and a batch of
#: forty is a render and forty commits — longer than any single publish ever
#: was, and it can pass five minutes. This stays at five minutes anyway. A
#: worker or CLI publisher that waits that long and then reads "another
#: publish has held ... for more than 300s" has been told something true and
#: acted on: it can try again when the batch is done. Raising the timeout to
#: cover the worst batch would only make the other refusal — the process that
#: died holding the lock — take that much longer to arrive, and that is the
#: one a person has to go and fix.
LOCK_TIMEOUT = 300.0

#: The lock file, kept in the repository's git directory and NOT in the work
#: tree. Everything in the work tree is generated output that the publisher
#: stages with `git add -A`, so a lock file there would be committed and
#: published to the gallery.
LOCK_NAME = "sketchgen-publish.lock"


def _lock_file(gallery_dir: str | os.PathLike[str]) -> Path | None:
    """Where this checkout's publish lock lives, or None if it is not a repo.

    None means the caller should carry on without a lock: there is nothing to
    serialise, and the checks in :func:`gallery_checkout` are about to refuse
    this directory anyway with a better message than a lock error would give.
    """
    path = Path(os.path.expanduser(str(gallery_dir)))
    if not path.is_dir():
        return None
    git_dir = _git_out(path, "rev-parse", "--absolute-git-dir")
    return Path(git_dir) / LOCK_NAME if git_dir else None


@contextlib.contextmanager
def _checkout_lock(gallery_dir: str | os.PathLike[str], timeout: float | None = None):
    """Hold an exclusive lock on one gallery checkout for the whole publish.

    Two publishers share one working tree on the node -- the worker's and the
    operator UI's -- and nothing used to keep them apart. On 2026-09-16 two
    overlapping publishes of entry 488 raced: the second had negotiated its
    push against the ref the first then moved, and GitHub refused it with
    "cannot lock ref 'refs/heads/main': is at <x> but expected <y>".

    That one was benign, because the loser reset after the winner had
    finished. The dangerous ordering is the other one: :func:`_undo` runs
    ``git reset --hard`` on the shared tree, so a publish that fails its push
    can pull the tree out from under a publish that is still rendering into
    it, and the survivor then commits and pushes a half-reset gallery. Nothing
    would report that; the pages would just be wrong.

    So the lock covers everything from the clean-tree check to the last push,
    the clean-tree check included -- checking that a tree is clean and then
    letting someone else dirty it is the same race one step earlier. It is an
    advisory ``flock`` on a file in the git directory, which the kernel drops
    if the holder dies, so a killed publisher does not wedge the node.
    """
    # Read at call time, not bound as a default, so a test can shorten it.
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    lock_path = _lock_file(gallery_dir)
    if lock_path is None:
        yield
        return
    try:
        handle = open(lock_path, "w")  # noqa: SIM115
    except OSError as exc:
        raise PublishFailed(f"cannot open the publish lock at {lock_path}: {exc}") from exc
    deadline = time.monotonic() + timeout
    waited = False
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PublishFailed(
                        f"another publish has held {lock_path} for more than "
                        f"{timeout:.0f}s; if nothing is publishing, a process died "
                        "holding it and the lock clears when it is gone"
                    ) from None
                if not waited:
                    print(
                        "sketchgen: waiting for another publish to finish with "
                        f"{Path(os.path.expanduser(str(gallery_dir)))}",
                        file=sys.stderr,
                    )
                    waited = True
                time.sleep(0.25)
        yield
    finally:
        handle.close()  # closing drops the flock


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


def _render_with_generator(
    conn: sqlite3.Connection, entry_id: int, dest: Path, checkout: Path
) -> Path:
    """Ask packet 3.1's generator for the entry's files, or refuse.

    This module never invents an entry's files: either the caller passes a
    directory with --from, or the generator produces one.

    ``dest`` is a staging directory, not the checkout: the personal-data scan
    runs on these bytes before any of them reach the repository. The generator
    reads its config out of the directory it is writing into, and an empty
    staging directory has no config.json, so the config is loaded from the
    ``checkout`` this entry is about to be committed to and passed in. Without
    it every first-published page came out with ``write_path`` empty — no
    critique form, because there was nowhere to send a critique.
    """
    try:
        from . import gallery  # noqa: PLC0415 - optional, lands in packet 3.1
    except ImportError as exc:
        raise PublishRefused("generator not present; pass --from") from exc
    render = getattr(gallery, "render_entry", None)
    if render is None:
        raise PublishRefused("generator not present; pass --from")
    # The web copies of the frames, before the render that looks for them
    # (gallery-hub.md, Step 0). Never a refusal: with no encoder, or a frame
    # it cannot read, the page publishes the PNG as every entry used to.
    frame_pngs = getattr(gallery, "frame_pngs", None)
    if frame_pngs is not None:
        webimg.ensure(frame_pngs(conn, entry_id))
    try:
        # The generator takes the gallery ROOT and writes dest/e/<id>/, which it
        # returns; that returned directory is the entry, and it is what we copy.
        out = render(conn, entry_id, dest, gallery.Config.load(checkout), publishing=True)
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
# One entry's worth of work, so that one publish and a batch do it the same way
# ---------------------------------------------------------------------------
#
# These four are the body of :func:`publish` between the lock and the push,
# lifted out whole rather than copied. :func:`publish` is now a batch of one
# and :func:`publish_many` is the same steps in a loop; there is no second
# implementation of the commit, the scan or the stamp to keep in step with
# this one.


def _prepare_source(
    conn: sqlite3.Connection,
    entry_id: int,
    checkout: Path,
    from_dir: str | os.PathLike[str] | None = None,
) -> tuple[Path, str | None]:
    """Where this entry's files are, and the temp directory to delete after.

    Either the caller passed a directory with ``--from``, in which case there
    is nothing to clean up, or the generator renders into a fresh staging
    directory whose path is returned as the second element so the caller's
    ``finally`` can remove it. A generator that refuses takes its staging
    directory with it, because there is no caller left to clean up for.
    """
    if from_dir is not None:
        source = Path(os.path.expanduser(str(from_dir)))
        if not source.is_dir():
            raise PublishRefused(f"no source directory at {source}")
        return source, None
    temporary = tempfile.mkdtemp(prefix="sketchgen-publish-")
    try:
        source = _render_with_generator(conn, entry_id, Path(temporary), checkout)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return source, temporary


def _files_to_publish(source: Path, entry_id: int) -> list[str]:
    """The files about to be committed, once they have passed the guard.

    Kept apart from :func:`_stage_and_commit` because ``--dry-run`` needs the
    list and the scan without the commit: a dry run that did not scan would
    report a publish that the real one is going to refuse.
    """
    files = _source_files(source)
    if not files:
        raise PublishRefused(f"{source} is empty; there is nothing to publish")
    scan_for_personal_data(source)
    return files


def _stage_and_commit(
    entry_id: int,
    checkout: Path,
    source: Path,
    message: str,
    *,
    files: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Copy the entry into ``e/<entry_id>/``, stage it, commit it, return the sha.

    Nothing here pushes and nothing here touches the database, so a refusal or
    a failure leaves only the checkout to put back — which the caller does,
    because only the caller knows which commit to put it back to.

    ``files`` is the list :func:`_files_to_publish` already produced, when the
    caller has one; without it the guard runs here instead, so no path reaches
    a commit unscanned.
    """
    if files is None:
        files = _files_to_publish(source, entry_id)
    dest = checkout / "e" / str(entry_id)
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
    return sha, files


def _stamp_row(
    conn: sqlite3.Connection, entry: sqlite3.Row, sha: str, now: str
) -> str:
    """Write the push's result onto the entry's row and say what state it is in.

    Only a held entry becomes 'published'. A rejection — the gate's
    'failed-kept' or the operator's 'rejected' — keeps its state, which is
    what puts it on the rejections page rather than the grid, and gains the
    timestamp and commit like any other.
    """
    new_state = "published" if entry["state"] == "held" else entry["state"]
    conn.execute(
        "UPDATE entries SET state = ?, published_utc = ?, "
        "publish_commit = ? WHERE id = ?",
        (new_state, now, sha, entry["id"]),
    )
    return new_state


def _follow_job(conn: sqlite3.Connection, entry: sqlite3.Row, new_state: str) -> None:
    """Move the originating job with its entry, so the queue stops saying "held".

    A job already past held is left alone.
    """
    if new_state == "published" and entry["job_id"] is not None:
        job = db.get_job(conn, entry["job_id"])
        if job is not None and job.state == "held":
            db.transition(conn, job.id, "published")


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
    with _checkout_lock(gallery_dir):
        checkout, branch = gallery_checkout(gallery_dir)
        target = remote or _git_out(checkout, "remote", "get-url", "origin")
        if not target:
            raise PublishRefused(
                f"{checkout} has no 'origin' remote and no --remote was given"
            )

        source, temporary = _prepare_source(conn, entry_id, checkout, from_dir)

        try:
            files = _files_to_publish(source, entry_id)
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
            sha, files = _stage_and_commit(
                entry_id, checkout, source, message, files=files
            )

            pushed = _git(
                checkout, "push", target, f"HEAD:refs/heads/{branch}", env=env
            )
            if pushed.returncode != 0:
                _undo(checkout, before)
                raise PublishFailed(pushed.stderr.strip() or "git push failed")

            now = db.utc_now()
            new_state = _stamp_row(conn, entry, sha, now)
            _follow_job(conn, entry, new_state)
            conn.commit()
            # The entry is public. Now the grid, the failures page, compare and the
            # line pages must know about it: re-render the index and push it as a
            # second commit. This never fails the publish; the entry is already up.
            index_sha, index_note = _publish_index(
                conn, checkout, branch, target, env, entry_id,
                regenerate=from_dir is None,
            )
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


def publish_many(
    conn: sqlite3.Connection,
    entry_ids: Sequence[int],
    *,
    gallery_dir: str | os.PathLike[str] = DEFAULT_GALLERY_DIR,
    key: str | None = DEFAULT_KEY_PATH,
    remote: str | None = None,
    by: str | None = None,
    model: str | None = None,
    url_base: str = DEFAULT_URL_BASE,
    on_step: Callable[[str, int | None, str], None] | None = None,
) -> ManyResult:
    """Publish several entries as one transaction: n commits, one push, one index.

    Eight publishes one at a time are eight renders, sixteen commits and
    sixteen pushes, and the operator waits through every one of them. This is
    the same work with the waiting taken out: each entry is still rendered,
    scanned and committed on its own, but there is a single push for all of
    those commits and a single index pass afterwards.

    The invariant from this module's first paragraph holds exactly, and that
    is what decides the order below: a row becomes ``published`` only after
    the push that carried its commit has succeeded. So no row is touched until
    the push returns, and if the push fails then :func:`_undo` puts the
    checkout back where the batch found it, every committed entry is reported
    in ``failed``, and all of them are still held — there is nothing to roll
    back in the database because nothing was written to it.

    An entry the publisher will not take (wrong state, the personal-data scan,
    the generator, bytes already in the gallery) is dropped before its commit
    and lands in ``refused``; the checkout goes back to the last commit so the
    next entry starts from a clean tree, and the batch goes on. There is no
    ``--from`` and no ``dry_run`` here: a batch is the operator UI's path and
    the generator is the only source of its bytes. Use :func:`publish` for
    either of those.

    ``on_step(phase, entry_id, sentence)`` is called before each unit of work
    — ``commit`` once per entry, then ``push``, then ``index`` — so a caller
    can count real steps rather than guess at a duration. It is only ever a
    report: an exception from it is the caller's own and is not caught here.
    """
    ids = list(entry_ids)
    result = ManyResult(published=[])
    if not ids:
        # Nothing to do, and taking the checkout lock to discover that would
        # make an empty press queue behind a real batch for no reason.
        return result

    def step(phase: str, entry_id: int | None, sentence: str) -> None:
        if on_step is not None:
            on_step(phase, entry_id, sentence)

    published_by = by or _os_user()
    with _checkout_lock(gallery_dir):
        # A refusal here is the whole batch's: nothing has happened yet, and
        # the caller reports it against every entry it was asked for.
        checkout, branch = gallery_checkout(gallery_dir)
        target = remote or _git_out(checkout, "remote", "get-url", "origin")
        if not target:
            raise PublishRefused(
                f"{checkout} has no 'origin' remote and no --remote was given"
            )
        env, _ = _push_env(key, target)

        before = _git_out(checkout, "rev-parse", "HEAD")
        # Each entry's own commit, so the gallery's log still reads one entry
        # per line however many were published together.
        committed: list[tuple[sqlite3.Row, str, list[str]]] = []
        at = before  # the commit a refusal rewinds the checkout to
        for entry_id in ids:
            step(
                "commit",
                entry_id,
                f"Entry {entry_id} — rendering, scanning, committing e/{entry_id}/",
            )
            temporary: str | None = None
            try:
                entry = _entry(conn, entry_id)
                if entry["state"] not in PUBLISHABLE:
                    raise PublishRefused(
                        f"entry {entry_id} is {entry['state']}; only "
                        f"{' or '.join(sorted(PUBLISHABLE))} can be published"
                    )
                message = _message(entry, _executor_model(entry, model), published_by)
                source, temporary = _prepare_source(conn, entry_id, checkout)
                sha, files = _stage_and_commit(entry_id, checkout, source, message)
            except PublishRefused as exc:
                result.refused[entry_id] = str(exc)
                _undo(checkout, at)
                continue
            except PublishFailed as exc:
                # git itself would not do it — a broken index, a full disk.
                # Not this entry's fault and not the batch's, but the rest of
                # the batch is still publishable, so report it like a refusal
                # and carry on from a clean tree.
                result.failed[entry_id] = str(exc)
                _undo(checkout, at)
                continue
            finally:
                if temporary:
                    shutil.rmtree(temporary, ignore_errors=True)
            committed.append((entry, sha, files))
            at = sha

        if not committed:
            return result  # no commits, so no push and no index either

        step("push", None, f"Pushing {len(committed)} commits to the gallery")
        pushed = _git(checkout, "push", target, f"HEAD:refs/heads/{branch}", env=env)
        if pushed.returncode != 0:
            why = pushed.stderr.strip() or "git push failed"
            _undo(checkout, before)
            for entry, _sha, _files in committed:
                result.failed[entry["id"]] = why
            return result

        # The push has succeeded, so now the rows may say so. One transaction
        # for all of them: the batch was one push and it is one fact.
        now = db.utc_now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            states = [
                (entry, _stamp_row(conn, entry, sha, now)) for entry, sha, _ in committed
            ]
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        # The jobs follow outside it, because db.transition opens a
        # transaction of its own to check the move is legal, and it is right
        # that it does: an illegal job transition must not take the entry
        # stamps down with it, and the stamps are the part that must not be
        # lost once the commits are public.
        for entry, new_state in states:
            _follow_job(conn, entry, new_state)

        for entry, sha, files in committed:
            result.published.append(
                Published(
                    entry_id=entry["id"],
                    commit=sha,
                    branch=branch,
                    url=entry_url(entry["id"], url_base),
                    published_utc=now,
                    files=files,
                )
            )

        # Every entry of the batch goes through the generator again — the same
        # "later truth" pass a single publish does, for the same reason: the
        # bytes committed above were rendered before the push and so before
        # the row knew its own commit. Then the index, once, for all of them.
        step("index", None, "Re-rendering the index and pushing it")
        result.index_commit, result.index_note = _publish_index(
            conn, checkout, branch, target, env, [entry["id"] for entry, _, _ in committed]
        )
        return result


def publish_index(
    conn: sqlite3.Connection,
    gallery_dir: str | os.PathLike[str],
    *,
    key: str | None = None,
    remote: str | None = None,
    write_path: str | None = None,
    on_step: Callable[[int, int, str], None] | None = None,
) -> tuple[str | None, str | None]:
    """Re-render every published entry and the index, commit and push.

    ``write_path`` overrides the gallery write-path URL recorded in the
    checkout's config.json; the re-render carries it into every page.

    ``on_step(done, total, label)`` is called as each step begins, ``label``
    naming it ("entry 431", "index", "staging", "commit", "push"), and once
    more at the end with ``done == total``. The total is every entry plus
    those four. It is how the deploy script draws its bar: five hundred pages
    is minutes, and a minutes-long silence reads as a hang.

    For template or asset changes: no entry's state changes. Returns
    (sha, None) on success or (None, why); refuses on a dirty checkout.
    """
    with _checkout_lock(gallery_dir):
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
            # replace(), not a new Config from the three fields this once
            # knew: that reset the kiosk's sites, buildings and switches to
            # their defaults, and this commits and pushes the config.json
            # render_index writes. Any run with SKETCHGEN_WRITEPATH_URL set,
            # which the node's units set and the deploy's ssh does not, would
            # have taken the D12 kiosk's site and hours off the gallery
            # (docs/plans/kiosk-mac.md §1.3). Found in review, 2026-09-24.
            config = replace(config, write_path=write_path.rstrip("/"))
        # Only what a person has published: a kept rejection without a
        # published_utc is still waiting for that decision (spec §9), and a
        # re-render is not the place it gets made.
        rows = conn.execute(
            "SELECT id FROM entries WHERE state IN ('published', 'failed-kept', "
            "'rejected') AND published_utc IS NOT NULL ORDER BY id"
        ).fetchall()
        total = len(rows) + 4  # the entries, then index, staging, commit, push
        done = 0

        def step(label: str) -> None:
            if on_step is not None:
                on_step(done, total, label)

        for row in rows:
            step(f"entry {row['id']}")
            gallery.render_entry(conn, row["id"], checkout, config)
            done += 1
        step("index")
        gallery.render_index(conn, checkout, config)
        done += 1
        step("staging")
        _git(checkout, "rm", "-q", "--ignore-unmatch", "--", "failed.html")  # renamed to rejections.html
        # Everything in the checkout is generated output (the generator also writes
        # files INDEX_PATHS does not list, pairs.json for one), so stage it all.
        added = _git(checkout, "add", "-A", "--", ".")
        if added.returncode != 0:
            return None, f"git add failed: {added.stderr.strip()}"
        done += 1
        if _git(checkout, "diff", "--cached", "--quiet").returncode == 0:
            step("unchanged")
            return None, "site unchanged"
        step("commit")
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
        done += 1
        sha = _git_out(checkout, "rev-parse", "HEAD")
        step("push")
        pushed = _git(checkout, "push", target, f"HEAD:refs/heads/{branch}", env=env)
        if pushed.returncode != 0:
            _undo(checkout, before)
            return None, f"push failed: {pushed.stderr.strip() or 'git push failed'}"
        done += 1
        step("pushed")
        return sha, None


INDEX_PATHS = ("index.html", "rejections.html", "compare.html", "lines", "assets", "config.json")


def _publish_index(
    conn: sqlite3.Connection,
    checkout: Path,
    branch: str,
    target: str,
    env: dict[str, str] | None,
    entries: int | Iterable[int],
    *,
    regenerate: bool = True,
) -> tuple[str | None, str | None]:
    """Re-render the entries and the gallery index, commit and push them.

    The entry pages go in again because the copies committed a moment ago were
    rendered BEFORE the push: their Provenance said the entry had no published
    date and no publish commit, and its own lineage ledger called it "not
    published", because ``published_utc`` was still null when those bytes were
    made. The rows carry both stamps by the time this runs, so the same render
    against the same database now writes them down. Same generator, later truth.

    ``entries`` is one entry id or several: a batch's whole push gets one index
    pass, because the index is a single rendering of the database as it now
    stands and re-rendering it once per entry would only be the same file
    written n times. One id is spelt "entry 431" in the commit and several
    "entries 431, 432, 435", so the gallery's log says which push this index
    belongs to.

    ``regenerate`` is false when the caller passed ``--from``: those are a
    person's own bytes and this is not the place to overwrite them with the
    generator's. (A later ``publish-index`` still will — the generator is the
    source of truth for a published page, and ``--from`` is an override of one
    commit, not of every render afterwards.)

    Returns (sha, None) on success, (None, why) otherwise. A failure here is
    reported, never raised: the entries themselves are already published.
    """
    entry_ids = [entries] if isinstance(entries, int) else list(entries)
    label = (
        f"entry {entry_ids[0]}"
        if len(entry_ids) == 1
        else "entries " + ", ".join(str(i) for i in entry_ids)
    )
    try:
        from . import gallery  # noqa: PLC0415
    except ImportError:
        return None, "generator not present; index not re-rendered"
    render_index = getattr(gallery, "render_index", None)
    if render_index is None:
        return None, "generator has no render_index; index not re-rendered"
    render_entry = getattr(gallery, "render_entry", None) if regenerate else None
    before = _git_out(checkout, "rev-parse", "HEAD")
    if render_entry is not None:
        for entry_id in entry_ids:
            try:
                render_entry(conn, entry_id, checkout)
            except Exception as exc:  # the generator's own refusal, reported
                _undo(checkout, before)
                return None, f"entry re-render failed: {exc}"
    try:
        render_index(conn, checkout)
    except Exception as exc:  # the generator's own refusal, reported
        # The generator undoes what it wrote, but the checkout is git's: put
        # it back exactly at the entry commit, so the next publish is not
        # refused for a dirty tree that this one left behind.
        _undo(checkout, before)
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
        input=f"gallery index after {label}\n",
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
            input=f"gallery index after {label}, second pass\n\n{leftover}\n",
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
    """Put the checkout back where it was before the failed publish.

    Tracked files go back to ``before``; untracked ones the failed render
    created are removed too, because the next publish refuses any dirty
    tree, untracked included. Everything in the checkout is generated
    output, so there is nothing of a person's to protect here.
    """
    if before:
        _git(checkout, "reset", "--hard", before)
    else:  # pragma: no cover - a gallery with no commit at all
        _git(checkout, "reset", "--hard", "HEAD~1")
    _git(checkout, "clean", "-fdq", "--", ".")


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

    Since migration 007 the reason has a column of its own, ``reject_reason``;
    it is also still written to the originating job's ``last_error``, where
    this function has always put it and where the operator UI's job page reads
    it. This is the CLI's path and it stops at the state flip: the UI's
    ``web.reject_entry`` goes on to publish the entry to the rejections
    catalog (§5.2). Either way no file is touched and nothing is deleted.
    """
    entry = _entry(conn, entry_id)
    if entry["state"] != "held":
        raise PublishRefused(
            f"entry {entry_id} is {entry['state']}; only held can be rejected"
        )
    db.entry_transition(
        conn, entry_id, "rejected", reject_reason=(reason or "").strip() or None
    )
    job = db.get_job(conn, entry["job_id"]) if entry["job_id"] is not None else None
    if job is not None and job.state == "held":
        db.transition(conn, job.id, "rejected", last_error=f"entry rejected: {reason or ''}".rstrip(": "))
    if reason:
        conn.execute(
            "UPDATE jobs SET last_error = ?, updated_utc = ? WHERE id = ?",
            (f"entry rejected: {reason}", db.utc_now(), entry["job_id"]),
        )
    return reason or ""
