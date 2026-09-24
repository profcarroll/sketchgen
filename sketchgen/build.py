"""Which build this node is on, which it is meant to be on, and what is running.

docs/plans/fleet.md, Packet 1. On 2026-09-24 four nodes ran sketchgen and none
of them could say which build it was: sld-cloud reported itself up to date four
PRs behind main because nothing on it fetched, three nodes were held on a72f076
for the hardware A/B by nothing but a detached HEAD that `update.sh`'s
`git pull origin main` fast-forwards away without a word, and sld-cloud's
checkout had moved partway through arm B with nothing recording which attempts
ran on which side of the move. Three facts per node answer "is it on the same
build": where the code is meant to be (the **target**: main, or a declared
**pin**), where the checkout **is**, and what the worker and web processes are
**running** — which is not the checkout after a pull without a restart.

This module is stdlib-only and imports nothing from the package at the top, on
purpose: `bin/fleet` on the laptop sends this very file to a node over ssh and
runs it there (``python3 - --json < build.py``), which is how the fleet reads a
node whose checkout predates this file. Anything the package needs from it is a
plain function; the database is read with ``sqlite3`` directly, read-only.

Writes (the pin, the running stamps) go through ``sketchgen.db`` and are only
ever made from inside the package, never from the ssh probe.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

#: The checkout this file is running from, when it is running from one. For the
#: ssh probe (stdin) there is no file, and ``--root`` says where to look.
ROOT = Path(__file__).resolve().parent.parent if "__file__" in globals() else None

#: What "main" means everywhere here: the remote-tracking ref a fetch updates.
UPSTREAM = "origin/main"

#: The meta rows. A pin is four rows rather than one JSON blob so that each is
#: readable with the same ``get_meta`` everything else uses, and a pin with no
#: reason is visibly incomplete rather than silently parsed.
PIN_SHA, PIN_REASON, PIN_BY, PIN_UTC = "pin.sha", "pin.reason", "pin.by", "pin.utc"
PIN_KEYS = (PIN_SHA, PIN_REASON, PIN_BY, PIN_UTC)
#: ``run.<role>`` = "<build> <utc>", written by each long-lived process at start.
RUN_ROLES = ("worker", "web")

#: Job states that mean work is in hand: a batch is not over while any job is
#: in one. ``needs-laptop`` is parked for an agent; it is still somebody's job.
QUEUED_STATES = ("queued",)
IN_FLIGHT_STATES = ("planning", "executing", "gating", "repairing")
PARKED_STATES = ("needs-laptop",)

#: The transient unit `bin/fleet update` and the Console's button start
#: update.sh under. Its name is the lock they share: systemd refuses a second.
UPDATE_UNIT = "sketchgen-update.service"
UNITS = {"worker": "sketchgen-worker.service", "web": "sketchgen-web.service",
         "update": UPDATE_UNIT}

GIT_TIMEOUT_S = 10


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def git(root: str | os.PathLike[str], *args: str,
        timeout: float = GIT_TIMEOUT_S) -> str | None:
    """``git -C root args``, stripped stdout, or None on any failure."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def describe(sha: str | None, dirty: bool) -> str | None:
    """The build as it is recorded: the full sha, and ``-dirty`` when it is not
    exactly that commit. A node with a hand-edited gate is not running the sha
    it is on, and an attempt stamped with the bare sha would say it was."""
    if not sha:
        return None
    return f"{sha}-dirty" if dirty else sha


def short(build: str | None) -> str:
    """``a72f076`` or ``a72f076-dirty``; ``?`` for nothing."""
    if not build:
        return "?"
    sha, _, suffix = build.partition("-")
    return sha[:7] + (f"-{suffix}" if suffix else "")


def checkout(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Where the checkout at ``root`` is: sha, subject, branch, dirty.

    Dirty is tracked files only. Untracked files (a ``__pycache__`` the
    ignore file missed, a note left in the tree) do not change what runs and
    do not stop a fast-forward, so they are not what the word means here.
    """
    sha = git(root, "rev-parse", "HEAD")
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    porcelain = git(root, "status", "--porcelain", "--untracked-files=no")
    return {
        "sha": sha,
        "subject": git(root, "log", "-1", "--format=%s") if sha else None,
        "branch": branch or None,
        "detached": sha is not None and not branch,
        "dirty": bool(porcelain) if porcelain is not None else None,
        "build": describe(sha, bool(porcelain)),
    }


def fetch(root: str | os.PathLike[str], timeout: float = GIT_TIMEOUT_S) -> str | None:
    """``git fetch origin main``. None on success, else one line saying why.

    Never raises: a node that cannot reach GitHub still knows what it is on,
    and says how old its idea of main is instead of failing the reading.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "fetch", "--quiet", "origin", "main"],
            capture_output=True, text=True, timeout=timeout, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return f"git fetch timed out after {timeout:.0f} s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git fetch: {exc}"
    if done.returncode != 0:
        lines = [line for line in (done.stderr or "").splitlines() if line.strip()]
        return "git fetch: " + (lines[-1].strip() if lines else f"exit {done.returncode}")
    return None


def fetched_utc(root: str | os.PathLike[str]) -> str | None:
    """When ``origin/main`` was last updated, from the ref's own file time.

    FETCH_HEAD is written by any fetch and the ref file only when it moved or
    was rewritten, so the later of the two is "last looked". A worktree keeps
    its FETCH_HEAD in its own git dir and its refs in the common one.
    """
    dirs = {git(root, "rev-parse", "--absolute-git-dir"),
            git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")}
    times = []
    for gitdir in filter(None, dirs):
        for name in ("FETCH_HEAD", "refs/remotes/origin/main"):
            try:
                times.append((Path(gitdir) / name).stat().st_mtime)
            except OSError:
                continue
    if not times:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(times)))


def resolve(root: str | os.PathLike[str], ref: str) -> str | None:
    """A ref as a full commit sha, or None when the checkout does not have it."""
    return git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def is_ancestor(root: str | os.PathLike[str], older: str, newer: str) -> bool:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", older, newer],
            capture_output=True, timeout=GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def prs_between(root: str | os.PathLike[str], older: str, newer: str) -> int | None:
    """How many PRs ``newer`` is ahead of ``older``: merges on the first-parent
    line, because that is the unit the operator thinks in ("four PRs behind"),
    not the 26 commits those PRs happen to contain."""
    out = git(root, "rev-list", "--first-parent", "--count", f"{older}..{newer}")
    try:
        return int(out) if out is not None else None
    except ValueError:
        return None


def commits(root: str | os.PathLike[str], old: str, new: str,
            limit: int = 40) -> list[dict[str, Any]]:
    """The PRs between two commits, newest first, as the operator names them.

    GitHub's merge commit says "Merge pull request #174 from owner/branch" on
    its first line and the PR's own title on the third; the title is the part
    anyone recognises, so it is what is returned beside the number.
    """
    out = git(root, "log", "--first-parent", f"--max-count={limit}",
              "--format=%H%x1f%s%x1f%b%x1e", f"{old}..{new}")
    found = []
    for record in (out or "").split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) < 2 or not parts[0]:
            continue
        sha, subject = parts[0].strip(), parts[1].strip()
        body = parts[2] if len(parts) > 2 else ""
        pr = None
        title = subject
        if subject.startswith("Merge pull request #"):
            pr = subject.split("#", 1)[1].split()[0]
            first = next((line.strip() for line in body.splitlines() if line.strip()), "")
            title = first or subject
        found.append({"sha": sha, "pr": pr, "title": title})
    return found


def relation(root: str | os.PathLike[str], sha: str | None,
             main: str | None) -> dict[str, Any]:
    """Where ``sha`` sits against ``main``: behind by N, at it, ahead, or off it.

    ``on_main`` is False for a commit that is not in main's history at all —
    the fix committed on sld-cloud on 2026-09-22 and never pushed is the case,
    and it is the one a deploy cannot fast-forward past.
    """
    if not sha or not main:
        return {"on_main": None, "behind": None, "ahead": None}
    if sha == main:
        return {"on_main": True, "behind": 0, "ahead": 0}
    if is_ancestor(root, sha, main):
        return {"on_main": True, "behind": prs_between(root, sha, main), "ahead": 0}
    if is_ancestor(root, main, sha):
        # Ahead of what this checkout last fetched: main moved and the fetch
        # has not caught up. On main as far as anyone here can tell.
        return {"on_main": True, "behind": 0, "ahead": prs_between(root, main, sha)}
    return {"on_main": False, "behind": None, "ahead": None}


# ---------------------------------------------------------------------------
# The process's own build
# ---------------------------------------------------------------------------

_RUNNING: str | None = None
_RUNNING_SET = False


def running() -> str | None:
    """The build this process is running: its checkout when it first asked.

    Cached for the life of the process, which is the point — a worker that
    asked at start keeps answering with what it imported, however many pulls
    land under it. ``SKETCHGEN_BUILD`` wins, for a tarball deploy with no git
    and for tests. None where there is no checkout to read.
    """
    global _RUNNING, _RUNNING_SET
    if not _RUNNING_SET:
        forced = (os.environ.get("SKETCHGEN_BUILD") or "").strip()
        if forced:
            _RUNNING = forced
        elif ROOT is not None:
            _RUNNING = checkout(ROOT)["build"]
        _RUNNING_SET = True
    return _RUNNING


def node_commit() -> str | None:
    """The commit of the checkout this code is running from, or None.

    DECIDE[freshness]. An agent reads AGENTS.md in its own clone and drives a
    node that may be ahead of it: on 2026-09-21 a session read the file at
    #131 and answered packets the node had cut under #132, whose items carried
    a `usage` slot nothing had told it about. Null is a fine answer — a
    tarball deploy, no git, a checkout it cannot read — and it is never a
    failure: this is a line of information, not a check. Uncached, unlike
    :func:`running`: it answers "what is on disk now".
    """
    if ROOT is None:
        return None
    return git(ROOT, "rev-parse", "HEAD", timeout=5)


def stamp_running(conn: sqlite3.Connection, role: str) -> str | None:
    """Write ``run.<role>`` = "<build> <utc>" and return the build.

    The worker calls this as it goes resident and the web server as it binds.
    A pull that is not followed by a restart then shows as the running build
    differing from the checkout — OPERATIONS.md's own "restart only the
    operator UI by hand" leaves the worker exactly there.
    """
    from sketchgen import db  # the package's writer; this module stays standalone

    build = running()
    db.set_meta(conn, f"run.{role}", f"{build or '?'} {db.utc_now()}")
    return build


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------


def set_pin(conn: sqlite3.Connection, sha: str, reason: str, by: str) -> None:
    from sketchgen import db

    db.set_meta(conn, PIN_SHA, sha)
    db.set_meta(conn, PIN_REASON, reason)
    db.set_meta(conn, PIN_BY, by)
    db.set_meta(conn, PIN_UTC, db.utc_now())


def clear_pin(conn: sqlite3.Connection) -> None:
    from sketchgen import db

    for key in PIN_KEYS:
        db.set_meta(conn, key, None)


# ---------------------------------------------------------------------------
# Reading the node
# ---------------------------------------------------------------------------


def _connect_ro(db_path: str | os.PathLike[str]) -> sqlite3.Connection | None:
    path = Path(os.path.expanduser(str(db_path)))
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _meta(conn: sqlite3.Connection) -> dict[str, str | None]:
    try:
        return {row["key"]: row["value"]
                for row in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.Error:
        return {}


def read_database(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    """The rows a reading needs, read-only: meta, control, schema, the queue."""
    conn = _connect_ro(db_path)
    if conn is None:
        return {"present": False, "meta": {}, "control": None, "schema": None,
                "work": None}
    try:
        meta = _meta(conn)
        try:
            row = conn.execute("SELECT state, reason, updated_utc FROM control").fetchone()
            control = dict(row) if row is not None else None
        except sqlite3.Error:
            control = None
        try:
            schema = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        except sqlite3.Error:
            schema = None
        try:
            counts = {row[0]: row[1] for row in conn.execute(
                "SELECT state, COUNT(*) FROM jobs GROUP BY state")}
            work = {
                "queued": sum(counts.get(s, 0) for s in QUEUED_STATES),
                "in_flight": sum(counts.get(s, 0) for s in IN_FLIGHT_STATES),
                "parked": sum(counts.get(s, 0) for s in PARKED_STATES),
                "max_job": conn.execute("SELECT MAX(id) FROM jobs").fetchone()[0],
            }
        except sqlite3.Error:
            work = None
    finally:
        conn.close()
    return {"present": True, "meta": meta, "control": control, "schema": schema,
            "work": work}


def unit_states(units: dict[str, str] = UNITS) -> dict[str, str | None]:
    """``systemctl --user is-active`` for each unit; None where it cannot say."""
    names = list(units.values())
    try:
        done = subprocess.run(
            ["systemctl", "--user", "is-active", *names],
            capture_output=True, text=True, timeout=5, check=False,
        )
        lines = done.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        lines = []
    if len(lines) != len(names):
        return {role: None for role in units}
    return {role: line.strip() for role, line in zip(units, lines)}


def _run_stamp(value: str | None) -> dict[str, str | None] | None:
    if not value:
        return None
    build, _, utc = value.partition(" ")
    return {"build": None if build == "?" else build, "utc": utc or None}


def reading(root: str | os.PathLike[str], db_path: str | os.PathLike[str],
            do_fetch: bool = False,
            units: bool | dict[str, str | None] = True) -> dict[str, Any]:
    """Everything `sketchgen build`, the Console chip and `bin/fleet` show.

    ``problems`` is the list of reasons the node is not on target, each a
    short phrase; ``on_target`` is its emptiness. A fact that cannot be read
    is a problem only where not knowing it would let a wrong build pass: a
    live worker whose build was never stamped is one (it predates this file,
    and nothing can say what it runs), a node with no web unit is not.
    """
    fetch_error = fetch(root) if do_fetch else None
    here = checkout(root)
    data = read_database(db_path)
    meta = data["meta"]
    main = git(root, "rev-parse", "--verify", "--quiet", UPSTREAM)

    pin_sha = meta.get(PIN_SHA)
    if pin_sha:
        target = {"kind": "pin", "sha": pin_sha, "reason": meta.get(PIN_REASON),
                  "by": meta.get(PIN_BY), "utc": meta.get(PIN_UTC)}
    else:
        target = {"kind": "main", "sha": main, "reason": None, "by": None, "utc": None}

    against_main = relation(root, here["sha"], main)
    # True asks systemd; a dict is the answer already (tests, and a caller
    # that has just asked); False is "do not know".
    if units is True:
        states = unit_states()
    else:
        states = {role: (units or {}).get(role) for role in UNITS}
    run = {role: _run_stamp(meta.get(f"run.{role}")) for role in RUN_ROLES}

    problems: list[str] = []
    if here["sha"] is None:
        problems.append("no git checkout here")
    if here["dirty"]:
        problems.append("dirty tree (tracked files changed on the node)")
    if here["branch"] and here["branch"] != "main":
        problems.append(f"on branch {here['branch']}, not main")
    if target["kind"] == "pin":
        if here["sha"] and here["sha"] != pin_sha:
            problems.append(f"not at its pin {pin_sha[:7]}")
    elif here["sha"]:
        if against_main["on_main"] is False:
            problems.append("checkout is not on main")
        elif against_main["behind"]:
            n = against_main["behind"]
            problems.append(f"{n} PR{'s' if n != 1 else ''} behind main")
        elif main is None:
            problems.append("has never fetched main")
    for role in RUN_ROLES:
        if states.get(role) != "active":
            continue
        stamp = run[role]
        if stamp is None or not stamp["build"]:
            problems.append(f"{role} build unrecorded (it predates build stamps)")
        elif here["build"] and stamp["build"] != here["build"]:
            problems.append(f"{role} runs {short(stamp['build'])}, checkout is "
                            f"{short(here['build'])}: restart pending")
    if states.get("update") in ("active", "activating", "reloading"):
        problems.append("an update is running")

    return {
        "utc": utc_now(),
        "root": str(root),
        "checkout": here,
        "target": target,
        "upstream": {"sha": main, "fetched_utc": fetched_utc(root),
                     "fetch_error": fetch_error},
        "main": against_main,
        "running": run,
        "units": states,
        "schema": data["schema"],
        "control": data["control"],
        "work": data["work"],
        "database": data["present"],
        # Whether this checkout's update.sh honours a pin (the same PR brought
        # both). One that does not pulls main whatever the node is held on.
        "knows_pins": (Path(root) / "sketchgen" / "cli" / "build.py").is_file(),
        "problems": problems,
        "on_target": not problems,
    }


def render(doc: dict[str, Any]) -> str:
    """The reading as `sketchgen build` prints it."""
    here, target, up, rel = doc["checkout"], doc["target"], doc["upstream"], doc["main"]
    where = "detached" if here["detached"] else f"on {here['branch'] or '?'}"
    lines = [
        f"checkout  {short(here['build']):<14} {(here['subject'] or '')[:60]}",
        f"          {'dirty' if here['dirty'] else 'clean'}, {where}",
    ]
    if target["kind"] == "pin":
        why = f' — "{target["reason"]}"' if target.get("reason") else ""
        who = ", ".join(x for x in (target.get("by"), target.get("utc")) if x)
        lines.append(f"target    pinned {short(target['sha'])}{why}"
                     + (f" ({who})" if who else ""))
    else:
        lines.append(f"target    main → {short(up['sha'])}")
    if rel["on_main"] is False:
        lines.append("main      this commit is not on main")
    elif rel["behind"] is not None:
        gap = (f"{rel['behind']} PR{'s' if rel['behind'] != 1 else ''} behind main"
               if rel["behind"] else "at main")
        if rel.get("ahead"):
            gap = f"{rel['ahead']} ahead of the last fetch of main"
        lines.append(f"main      {gap}")
    looked = up.get("fetched_utc") or "never"
    lines.append(f"          main as of the last fetch, {looked}"
                 + (f" ({up['fetch_error']})" if up.get("fetch_error") else ""))
    for role in RUN_ROLES:
        stamp = doc["running"].get(role)
        state = doc["units"].get(role) or "?"
        if stamp:
            lines.append(f"{role:<9} {short(stamp['build']):<14} since {stamp['utc'] or '?'}"
                         f"  ({state})")
        else:
            lines.append(f"{role:<9} {'?':<14} not recorded  ({state})")
    control = doc.get("control") or {}
    work = doc.get("work") or {}
    lines.append(f"schema    {doc.get('schema') if doc.get('schema') is not None else '?'}")
    lines.append(f"generator {control.get('state') or '?'}"
                 + (f" — {control['reason']}" if control.get("reason") else ""))
    if work:
        lines.append(f"work      {work.get('queued', 0)} queued, "
                     f"{work.get('in_flight', 0)} in flight, "
                     f"{work.get('parked', 0)} parked for an agent")
    lines.append("on target" if doc["on_target"]
                 else "NOT ON TARGET: " + "; ".join(doc["problems"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Whether a deploy has to re-render the gallery
# ---------------------------------------------------------------------------

#: The modules step 4 of update.sh runs; everything they import is the render
#: path. Computed, not listed, so a new import cannot silently escape it.
RENDER_ROOTS = ("sketchgen/gallery.py", "sketchgen/publish.py",
                "sketchgen/cli/publishindex.py", "sketchgen/cli/webframes.py",
                "sketchgen/webimg.py")

#: Paths outside the package that no page is made from. OPERATIONS.md already
#: deploys a critic prompt with --no-render; the gate judges attempts and
#: writes nothing a page is rendered from but the database; writepath/ is
#: deployed by wrangler, not by this script. Anything not named here renders.
INERT_PREFIXES = ("docs/", "tests/", "systemd/", "rig/", "gate/", "prompts/",
                  "writepath/", "bin/")
INERT_FILES = ("update.sh", "install.sh", "requirements.txt", "LICENSE",
               "fleet.example", ".gitignore")


def _module_of(path: str) -> str | None:
    if not (path.startswith("sketchgen/") and path.endswith(".py")):
        return None
    return path[:-3].replace("/", ".")


def render_closure(root: str | os.PathLike[str]) -> set[str]:
    """Every ``sketchgen.*`` module the render path imports, transitively,
    read from the tree at ``root`` (the one being deployed)."""
    base = Path(root)
    seen: set[str] = set()
    todo = [m for m in (_module_of(p) for p in RENDER_ROOTS) if m]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = base / (name.replace(".", "/") + ".py")
        if not path.is_file():
            path = base / name.replace(".", "/") / "__init__.py"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        package = name.rsplit(".", 1)[0] if not path.name == "__init__.py" else name
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo += [a.name for a in node.names if a.name.startswith("sketchgen")]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".")
                    parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    mod = ".".join(parts + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                if not mod.startswith("sketchgen"):
                    continue
                todo.append(mod)
                # `from sketchgen import db` names a module, not an attribute.
                todo += [f"{mod}.{a.name}" for a in node.names]
    return {m for m in seen if (base / (m.replace(".", "/") + ".py")).is_file()
            or (base / m.replace(".", "/") / "__init__.py").is_file()}


def render_verdict(root: str | os.PathLike[str],
                   paths: Iterable[str]) -> tuple[bool, str]:
    """(render?, why) for a deploy that changes ``paths``.

    Renders when any path can reach a page: a module in the render closure,
    a template that is not the operator UI's, an asset, a migration (a
    backfill can change what a page shows), or anything this does not know.
    """
    paths = sorted(set(paths))
    if not paths:
        return False, "nothing changed"
    closure = render_closure(root)
    for path in paths:
        name = Path(path).name
        if path.startswith("sketchgen/templates/"):
            if name.startswith("op_"):
                continue
            return True, f"{path} is a page template"
        if path.startswith("sketchgen/assets/"):
            return True, f"{path} is a gallery asset"
        module = _module_of(path)
        if module is not None:
            if module in closure:
                return True, f"{path} is on the render path"
            continue
        if path in INERT_FILES or path.startswith(INERT_PREFIXES) or (
                "/" not in path and path.endswith(".md")):
            continue
        return True, f"{path} is not known to be outside the render path"
    return False, f"{len(paths)} file{'s' if len(paths) != 1 else ''}, none on the render path"


def changed_paths(root: str | os.PathLike[str], old: str, new: str) -> list[str] | None:
    out = git(root, "diff", "--name-only", old, new)
    return None if out is None else [line for line in out.splitlines() if line]


# ---------------------------------------------------------------------------
# The ssh probe: `python3 - --root R --db D < build.py`
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="Read one node's build as JSON (bin/fleet sends this file over ssh).")
    parser.add_argument("--root", default=str(Path("~/sketchgen/app").expanduser()))
    parser.add_argument("--db", default=str(Path("~/sketchgen/sketchgen.db").expanduser()))
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args(argv)
    doc = reading(Path(args.root).expanduser(), Path(args.db).expanduser(),
                  do_fetch=args.fetch)
    json.dump(doc, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through bin/fleet
    sys.exit(main())
