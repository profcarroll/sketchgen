"""gallery.py — the static gallery, generated from the database (packet 3.1).

One entry row plus its attempt directory in, a directory of plain files out:

    <gallery>/e/<id>/index.html          the ENTRY page (not the sketch)
    <gallery>/e/<id>/sketch/index.html   the sketch's own page, verbatim but for soundshim.py
    <gallery>/e/<id>/sketch/sketch.js    the file the entry's frame loads
    <gallery>/e/<id>/strip.png           four frames, from the gate
    <gallery>/e/<id>/gate.png            the gate's single frame
    <gallery>/e/<id>/statement.md        the executor's own words, verbatim
    <gallery>/e/<id>/meta.json           every spec §7 field
    <gallery>/index.html                 the grid (published entries)
    <gallery>/rejections.html            the gate's rejections, kept
    <gallery>/compare.html               the paired-judgment shell
    <gallery>/kiosk.html                  the projector shell (spec kiosk.md)
    <gallery>/kiosk.json                  every published entry, for the kiosk
    <gallery>/swipe.html                  the phone shell (spec swipe.md)
    <gallery>/swipe.json                  the same entries, without the prose
    <gallery>/pairs.json                 balanced pairs to offer, and agent verdicts
    <gallery>/lineage.json               every entry's place in its line
    <gallery>/lines/<root>.html          one page per lineage line
    <gallery>/assets/gallery.{css,js}
    <gallery>/config.json                write-path base URL, gallery URL

Three properties this module is built around.

**The gallery is public by construction.** GitHub Pages serves whatever is
committed, so :func:`guard` scans every text file this module writes for an
email-shaped string and for ``instance-`` (the node's hostname shape) and
raises :class:`Unsafe`, deleting everything the run wrote, rather than letting
one land in a public repository. The spec's no-personal-data rule (§7) is a
build-time check here, not a convention.

**Nothing is invented.** The two judgment populations are kept apart and are
rendered from the ``judgments`` table alone, through :mod:`sketchgen.pairs`'s
Bradley–Terry fit (packet 5.1): one score per population per question, never
one aggregate, the 'look' score as the headline and the 'brief' score beside
it. A population that has judged an entry no times says "no pairs yet" and
carries no number at all — a score nobody voted on is not a low score. Views
and likes live behind the write path (packet 3.3) and are rendered by
``gallery.js`` at read time; the generator writes an em dash and no number, and
they are never folded into either score (spec §5).

**The same database gives the same bytes.** No clock is read while rendering:
every timestamp on a page comes from a row. A second ``render_all`` over an
unchanged database rewrites every file identically, which is what makes the
publisher's commit in packet 3.2 mean something.

Python 3.12, stdlib only: ``string.Template``, ``html``, ``json``.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Iterable

from . import pairs as pairs_mod
from . import lineage
from . import qr
from . import soundshim

__all__ = [
    "DEFAULT_GALLERY_URL",
    "DEFAULT_REPOSITORY",
    "LICENCE",
    "META_KEYS",
    "PUBLIC_STATES",
    "Config",
    "Unsafe",
    "UnknownEntry",
    "guard",
    "render_all",
    "render_entry",
    "render_index",
]

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_ROOT / "templates"
ASSET_DIR = PACKAGE_ROOT / "assets"

#: The instructor's Pages site and the repository behind it (plan §3).
DEFAULT_GALLERY_URL = "https://profcarroll.github.io/sketchgen-gallery/"
DEFAULT_REPOSITORY = "https://github.com/profcarroll/sketchgen-gallery"

#: DECIDE[licence], plan §4: CC BY 4.0 with an attribution line built from
#: the entry's own provenance.
LICENCE = "CC BY 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"

#: The states that get a directory under ``e/``. ``held`` and ``archived`` are
#: not public: publication holds for a person (spec §9, DECIDE[publication-gate]).
#: A ``failed-kept`` or ``rejected`` row is public only once that person has
#: acted, which is when the publisher stamps ``published_utc``; before that it
#: is as private as a held entry, and ``_entries`` says so.
#:
#: ``rejected`` joined this tuple in the lineage ledger's §5.2. An operator
#: rejection used to be a state flip and nothing else, so 32 sketches, their
#: files all still on disk, were invisible and eleven published entries
#: descended from a parent the site had never heard of. Rejecting now publishes
#: the entry to the rejections catalog with the reason, next to the gate's own
#: rejections: never deleted, never invisible, still reachable through lineage.
PUBLIC_STATES = ("published", "failed-kept", "rejected")

#: Every key ``meta.json`` carries: the spec §7 provenance, plus the four the
#: packet adds. The tests assert this exactly.
META_KEYS = (
    "entry_id",
    "job_id",
    "state",
    "prompt",
    "brief",
    "statement",
    "submitted_by",
    "planner",
    "planner_prompt_version",
    "executor",
    "executor_prompt_version",
    "rules_file",
    "assertions",
    "gate",
    "attempts",
    "prompt_tokens",
    "completion_tokens",
    "wall_s",
    "shape",
    "seed",
    "lineage",
    "source",
    "created_utc",
    "published_utc",
    "publish_commit",
    "licence",
    "attribution",
    "last_error",
)

# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

#: Deliberately the same pattern the packet's ACCEPT greps with.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: Oracle's instance hostnames start ``instance-``; the spec forbids them and
#: every `sslip.io` name the course has used carries one.
HOSTNAME_MARK = "instance-"

#: Suffixes read as text by the guard. Everything else (PNG) is left alone.
TEXT_SUFFIXES = frozenset(
    {".html", ".css", ".js", ".json", ".md", ".txt", ".svg", ".csv"}
)


class Unsafe(Exception):
    """A file the generator wrote would have published personal data."""


class UnknownEntry(Exception):
    """No entry with that id, or one that is not public."""


class _Written:
    """Every path one render touched, so a refusal can undo all of it.

    Created files are removed on undo; a file that already existed gets its
    previous bytes back. The distinction was learned the hard way: a render
    that died half-way through the index (2026-09-14, entry 71, the web
    process running one version of this module against the next version's
    templates) unlinked assets/ and config.json it had only overwritten, and
    every publish after it was refused for a dirty checkout.
    """

    def __init__(self, dest: Path) -> None:
        self.dest = dest
        self.files: list[Path] = []
        self.dirs: list[Path] = []
        self.previous: dict[Path, bytes] = {}

    def _remember(self, path: Path) -> None:
        if path in self.previous or path in self.files:
            return
        if path.is_file():
            self.previous[path] = path.read_bytes()

    def mkdir(self, path: Path) -> Path:
        missing: list[Path] = []
        walk = path
        while not walk.exists():
            missing.append(walk)
            walk = walk.parent
        path.mkdir(parents=True, exist_ok=True)
        self.dirs.extend(reversed(missing))
        return path

    def write_text(self, path: Path, text: str) -> Path:
        self.mkdir(path.parent)
        self._remember(path)
        path.write_text(text, encoding="utf-8")
        self.files.append(path)
        return path

    def copy(self, src: Path, dst: Path) -> Path:
        self.mkdir(dst.parent)
        self._remember(dst)
        shutil.copyfile(src, dst)
        self.files.append(dst)
        return dst

    def undo(self) -> None:
        for path in self.files:
            try:
                if path in self.previous:
                    path.write_bytes(self.previous[path])
                else:
                    path.unlink()
            except OSError:
                pass
        for directory in reversed(self.dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        self.files.clear()
        self.dirs.clear()
        self.previous.clear()


def _violations(paths: Iterable[Path]) -> list[str]:
    found: list[str] = []
    for path in sorted(set(paths)):
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - unreadable file
            found.append(f"{path}: unreadable ({exc})")
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = EMAIL_RE.search(line)
            if match:
                found.append(f"{path}:{line_no}: email-shaped string {match.group(0)!r}")
            if HOSTNAME_MARK in line:
                found.append(f"{path}:{line_no}: node hostname {HOSTNAME_MARK!r}")
    return found


def guard(dest_dir: str | Path, written: _Written | None = None) -> None:
    """Refuse to leave personal data in a public checkout.

    With ``written``, only that run's files are scanned and all of them are
    deleted before the raise, so a refused render leaves no partial entry.
    Without it, every text file under ``dest_dir`` is scanned and nothing is
    deleted — the read-only form, for checking a checkout before a push.
    """
    dest = Path(dest_dir)
    if written is None:
        paths = [p for p in dest.rglob("*") if p.is_file()]
    else:
        paths = list(written.files)
    problems = _violations(paths)
    if not problems:
        return
    if written is not None:
        written.undo()
    raise Unsafe(
        "refused: the gallery is public and these would have been published:\n  "
        + "\n  ".join(problems)
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """The two URLs the generated pages need, and the repository behind them.

    ``write_path`` is the base URL of the gallery write path (packet 3.3):
    ``/counts``, ``/like``, ``/vote`` and ``/login`` hang off it. It is empty
    until that service exists, and ``gallery.js`` renders an em dash and a
    disabled like button when it is.

    ``kiosk_views`` is whether a sketch played on the projector counts as a
    view (docs/plans/kiosk-views.md §3.3). It lives here rather than in the
    pipeline because the write path has no switch of its own to turn it off
    with: this one is a line in the gallery checkout's ``config.json``, which
    ``load`` reads back and ``render-index`` leaves as it found it.
    """

    write_path: str = ""
    gallery_url: str = DEFAULT_GALLERY_URL
    repository: str = DEFAULT_REPOSITORY
    kiosk_views: bool = True

    @classmethod
    def load(cls, dest_dir: str | Path) -> "Config":
        """The checkout's own config.json, or the defaults if it has none."""
        path = Path(dest_dir) / "config.json"
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            write_path=str(data.get("write_path") or ""),
            gallery_url=str(data.get("gallery_url") or DEFAULT_GALLERY_URL),
            repository=str(data.get("repository") or DEFAULT_REPOSITORY),
            # Absent is on. A gallery whose config.json predates the kiosk
            # counts its views, which is the state every checkout is in the
            # first time this runs.
            kiosk_views=data.get("kiosk_views") is not False,
        )

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "write_path": self.write_path,
                    "gallery_url": self.gallery_url,
                    "repository": self.repository,
                    "kiosk_views": self.kiosk_views,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def entry_url(self, entry_id: int) -> str:
        return f"{self.gallery_url.rstrip('/')}/e/{entry_id}/"

    def tree_url(self, entry_id: int) -> str:
        return f"{self.repository.rstrip('/')}/tree/main/e/{entry_id}"


def _resolve_config(dest_dir: Path, config: Config | None) -> Config:
    return config if config is not None else Config.load(dest_dir)


# ---------------------------------------------------------------------------
# Reading the database
# ---------------------------------------------------------------------------


#: What each public state reads as on a page. The two kinds of rejection are
#: both rejections and a reader deserves to know which: the gate refused one
#: after every attempt, a person refused the other. These are the labels
#: ``web.STATE_LABELS`` already uses on the operator side, so the two halves of
#: the project say the same words about the same row.
STATE_CHIPS = {
    "failed-kept": ("failed", "rejected · automatic"),
    "rejected": ("rejected", "rejected · operator"),
}


def _state_chip(state: str) -> str:
    """The chip a public entry wears: a rejection says so, and says whose."""
    css, label = STATE_CHIPS.get(state, (state, state))
    return f'<span class="chip {_esc(css)}">{_esc(label)}</span>'


def _entry(conn: sqlite3.Connection, entry_id: int, *, publishing: bool = False) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise UnknownEntry(f"no entry {entry_id}")
    if publishing and row["state"] == "held":
        # The publisher renders BEFORE the push that makes the entry public
        # (the row flips to published only on a successful push), so a held
        # entry is admitted here and rendered as the published entry it is
        # about to become. publish_commit stays null until the push lands.
        d = dict(row)
        d["state"] = "published"
        return d
    if row["state"] not in PUBLIC_STATES:
        raise UnknownEntry(
            f"entry {entry_id} is {row['state']}: only "
            f"{' and '.join(PUBLIC_STATES)} entries are public"
        )
    if row["state"] in STATE_CHIPS and not row["published_utc"] and not publishing:
        raise UnknownEntry(
            f"entry {entry_id} is a rejection nobody has published yet: "
            "publish it first (spec §9)"
        )
    return row


def _entries(conn: sqlite3.Connection, state: str) -> list[sqlite3.Row]:
    """The rows in ``state`` that the publisher has put on the site.

    ``published_utc`` is set by a successful push and by nothing else. The
    worker marks a rejection ``failed-kept`` the moment the gate gives up, long
    before anyone decides to show it; without this clause the index would
    print a card, and a 404 behind it, for every rejection nobody published.
    """
    return list(
        conn.execute(
            "SELECT * FROM entries WHERE state = ? AND published_utc IS NOT NULL "
            "ORDER BY created_utc, id",
            (state,),
        )
    )


def _job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _attempt_rows(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM attempts WHERE job_id = ? ORDER BY n", (job_id,))
    )


def _all_scores(conn: sqlite3.Connection) -> dict[str, dict[str, dict]]:
    """Every Bradley–Terry table the pages need, fitted once per render.

    ``{population: {question: {entry_id: {...}}}}``. Four fits, not four per
    entry: the fit is over the whole pool, so doing it per card would be both
    slower and no different. Packet 5.1; see sketchgen/pairs.py for the prior
    and what it costs.
    """
    return {
        population: {
            question: pairs_mod.scores(conn, population=population, question=question)
            for question in pairs_mod.QUESTIONS
        }
        for population in pairs_mod.POPULATIONS
    }


def _lineage_row(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM lineage WHERE child_entry_id = ?", (entry_id,)
    ).fetchone()


def _parent_of(conn: sqlite3.Connection, entry_id: int) -> int | None:
    """The parent from the lineage table, falling back to the entry column."""
    line = _lineage_row(conn, entry_id)
    if line is not None and line["parent_entry_id"] is not None:
        return int(line["parent_entry_id"])
    row = conn.execute(
        "SELECT parent_entry_id FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None or row["parent_entry_id"] is None:
        return None
    return int(row["parent_entry_id"])


def _public_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for state in PUBLIC_STATES:
        rows.extend(_entries(conn, state))
    return rows


def _forest(
    conn: sqlite3.Connection, *, admit: int | None = None
) -> tuple[dict[int, int | None], dict[int, list[int]]]:
    """(parent by id, children by id) over the public entries, id order.

    ``admit`` is the entry being published: the publisher renders it while the
    row is still ``held``, because the row only flips once the push of these
    files lands. Without it the entry is absent from the forest, its own parent
    lookup misses, and ``null`` is frozen into its meta.json forever — which is
    how 74 of 101 public non-root entries came to claim they have no parent.
    Admitting the one entry we are in the middle of publishing is the fix; the
    site's tree pages, which render entries that are already public, pass
    nothing and are unchanged.
    """
    ids = [int(row["id"]) for row in _public_rows(conn)]
    if admit is not None and int(admit) not in ids:
        ids.append(int(admit))
    parent: dict[int, int | None] = {}
    children: dict[int, list[int]] = {i: [] for i in ids}
    for entry_id in ids:
        mother = _parent_of(conn, entry_id)
        parent[entry_id] = mother if mother in children else None
    for entry_id in sorted(ids):
        mother = parent[entry_id]
        if mother is not None:
            children[mother].append(entry_id)
    return parent, children


def _root_of(parent: dict[int, int | None], entry_id: int) -> int:
    seen: set[int] = set()
    walk = entry_id
    while True:
        mother = parent.get(walk)
        if mother is None or mother in seen:
            return walk
        seen.add(walk)
        walk = mother


#: A ``lineage.json`` larger than this is a page weight problem, not a ledger.
#: The whole file is fetched by every entry page.
LINEAGE_JSON_LIMIT = 100 * 1024
#: What a root prompt shrinks to if the file ever crosses that line.
LINEAGE_ROOT_PROMPT_CAP = 200


def _offplan(row: Any) -> list[str]:
    """The assertions the kept attempt missed, for an entry that still runs.

    ``offplan_json`` is written by the worker when a job spends its attempts
    without ever failing a QA check: the sketch works and diverged from a
    machine-written brief. Empty for every entry that passed the gate outright,
    and for every row written before migration 011, which had no such column.
    """
    try:
        raw = row["offplan_json"]
    except (IndexError, KeyError):
        return []
    if not raw:
        return []
    try:
        names = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(name) for name in names] if isinstance(names, list) else []


def _is_public(row: Any) -> bool:
    """On the site: a public state AND the stamp a successful push leaves."""
    return row["state"] in PUBLIC_STATES and bool(row["published_utc"])


def _every_entry(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM entries ORDER BY id"))


def _whole_forest(
    conn: sqlite3.Connection, ids: list[int]
) -> tuple[dict[int, int | None], dict[int, list[int]]]:
    """Like :func:`_forest`, but over every entry whatever its state.

    The site's tree pages must not link to a held or rejected entry, which is
    why ``_forest`` stops at the public ones. The ledger has the opposite job:
    a generation that is not published is still a generation, and a line whose
    middle is missing counts wrong (spec §1.6).
    """
    parent: dict[int, int | None] = {}
    children: dict[int, list[int]] = {i: [] for i in ids}
    for entry_id in ids:
        mother = _parent_of(conn, entry_id)
        parent[entry_id] = mother if mother in children and mother != entry_id else None
    for entry_id in sorted(ids):
        mother = parent[entry_id]
        if mother is not None:
            children[mother].append(entry_id)
    return parent, children


def _ledger_stamp(conn: sqlite3.Connection) -> str:
    """The ledger's ``generated_utc``: the newest stamp the database holds.

    Not the wall clock. The ledger is a rendering of the database as it stands,
    so it is dated by the last thing the database recorded — an entry created
    or published, a lineage row written — and the same database gives the same
    bytes however many times it is rendered. That is what lets a re-render
    that changed nothing be the no-op ``publish_index`` promises instead of a
    commit that moves one timestamp. An empty gallery is dated by its schema:
    the newest migration stamp, which every initialised database carries.
    """
    row = conn.execute(
        """
        SELECT MAX(stamp) FROM (
            SELECT created_utc AS stamp FROM entries
            UNION ALL SELECT published_utc FROM entries
            UNION ALL SELECT created_utc FROM lineage
            UNION ALL SELECT applied_utc FROM schema_version
        )
        """
    ).fetchone()
    stamp = row[0] if row is not None else None
    # Unreachable on a migrated database; still no clock.
    return str(stamp) if stamp else "1970-01-01T00:00:00Z"


def _lineage_index(conn: sqlite3.Connection) -> dict[str, Any]:
    """The ledger's data file: every entry, its place in its line, one shape.

    The entry page carries its own ancestry as static HTML — it was true when
    the entry was published and it stays true — but siblings, children and the
    state of a not-yet-published relative all change afterwards, and a page
    that was rendered last month cannot know them. ``gallery.js`` paints those
    from this file, so it holds every entry in the table whatever its state
    and resolves parents through ``_parent_of`` regardless of the parent's
    state, which is where it deliberately differs from ``_forest``.

    Nothing private leaves: a non-public entry carries its shape in the line
    and nothing of its own — no strip, no prompt, not even who submitted it.
    """
    rows = _every_entry(conn)
    ids = [int(row["id"]) for row in rows]
    parent, children = _whole_forest(conn, ids)

    def payload(cap: int | None) -> dict[str, Any]:
        entries: dict[str, Any] = {}
        for row in rows:
            entry_id = int(row["id"])
            link = _lineage_row(conn, entry_id)
            public = _is_public(row)
            item: dict[str, Any] = {
                "state": row["state"],
                "public": public,
                "parent": parent[entry_id],
                "children": list(children[entry_id]),
                # The same number meta.json carries, computed the same way, so
                # a page and this file never disagree about a generation.
                "generation": int(link["generation"]) if link is not None else 1,
                "root": _root_of(parent, entry_id),
                "critique": link["critique"] if link is not None else None,
                "critique_by": link["critique_by"] if link is not None else None,
            }
            if public:
                root_prompt, _ = lineage.split_prompt(str(row["prompt"] or ""))
                if cap is not None:
                    root_prompt = root_prompt[:cap]
                item["submitted_by"] = row["submitted_by"]
                item["strip"] = f"e/{entry_id}/strip.png"
                item["root_prompt"] = root_prompt
                # So a ledger tile the script paints routes a listening sketch to
                # its own tab, the same as the server-rendered tiles do.
                item["mic"] = _needs_mic(
                    Path(row["source_dir"]) if row["source_dir"] else None
                )
            entries[str(entry_id)] = item
        return {"generated_utc": _ledger_stamp(conn), "entries": entries}

    data = payload(None)
    if len(_lineage_bytes(data)) > LINEAGE_JSON_LIMIT:
        # The critiques are the point of the file, so the prompts are what
        # gives. It buys about two kilobytes in two hundred and sixty-eight
        # entries: if this file ever needs real slimming it is the critiques
        # that have to move, not the prompts.
        data = payload(LINEAGE_ROOT_PROMPT_CAP)
    return data


def _lineage_bytes(data: dict[str, Any]) -> bytes:
    """``lineage.json`` as it is written: no indent, because nothing reads it.

    ``pairs.json`` is indented because a person opens it; this one is fetched
    by every entry page on the site and the indentation costs 28 KB of the
    100 KB budget for whitespace nobody sees.
    """
    return (
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _descendants(children: dict[int, list[int]], root: int) -> list[int]:
    out: list[int] = []
    stack = list(children.get(root, []))
    while stack:
        node = stack.pop(0)
        out.append(node)
        stack.extend(children.get(node, []))
    return out


# ---------------------------------------------------------------------------
# Reading the attempt directory
# ---------------------------------------------------------------------------


def _source_dir(row: sqlite3.Row, attempts: list[sqlite3.Row]) -> Path | None:
    candidates = [row["source_dir"]]
    if attempts:
        candidates.append(attempts[-1]["source_dir"])
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


def _gate_report(attempt: sqlite3.Row) -> dict[str, Any]:
    paths: list[Path] = []
    if attempt["gate_report_path"]:
        paths.append(Path(attempt["gate_report_path"]))
    if attempt["source_dir"]:
        paths.append(Path(attempt["source_dir"]) / ".gate" / "report.json")
    for path in paths:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                return data
    return {}


def _artefact(row: sqlite3.Row, attempts: list[sqlite3.Row], which: str) -> Path | None:
    """The strip or the gate frame, from the entry row or the gate's own dir."""
    column = "strip_path" if which == "strip" else "png_path"
    filename = "strip.png" if which == "strip" else "gate.png"
    candidates: list[Path] = []
    if row[column]:
        candidates.append(Path(row[column]))
    source = _source_dir(row, attempts)
    if source is not None:
        candidates.append(source / ".gate" / filename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _statement(row: sqlite3.Row, source: Path | None) -> str:
    if row["statement"]:
        return str(row["statement"]).strip()
    if source is not None:
        path = source / "statement.md"
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:  # pragma: no cover - unreadable statement
                return ""
    return ""


#: The one thing the kiosk cannot ask the sketch itself. Its frame is
#: ``allow-scripts`` without ``allow-same-origin``, which is opaque by design,
#: so there is no channel to read the canvas size over — and a fixed-size canvas
#: in a stage-sized frame sits top-left, where p5 puts it, which is why the
#: kiosk scales the frame rather than resizing it (kiosk-fullscreen.md §1). The
#: generator can read the source, so the generator says (spec §4.3). Two
#: integer literals and nothing else counts: a third argument is left outside
#: the match, so
#: ``createCanvas(800, 600, WEBGL)`` still gives a size, while ``windowWidth``,
#: a variable or an expression gives none and the kiosk fills the stage.
_CANVAS_RE = re.compile(r"createCanvas\(\s*(\d+)\s*,\s*(\d+)")


def _canvas_size(row: sqlite3.Row, attempts: list[sqlite3.Row]) -> list[int] | None:
    """``[width, height]`` from the sketch's own source, or ``None``.

    The same file :func:`_write_entry` copies to ``e/<id>/sketch/sketch.js``,
    read where it still lives — the attempt directory — so a manifest row and
    the page's frame can never disagree about what is on screen.
    """
    source = _source_dir(row, attempts)
    if source is None:
        return None
    path = source / "sketch.js"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - unreadable sketch
        return None
    match = _CANVAS_RE.search(text)
    if match is None:
        return None
    return [int(match.group(1)), int(match.group(2))]


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


_TEMPLATES: dict[str, Template] = {}


def _template(name: str) -> Template:
    """A template, read once per process.

    Read once so a long-running process (the operator UI, which publishes
    from inside itself) renders with the templates that match the code it
    loaded, not with whatever a later `git pull` put on disk. The version skew
    is what killed the index render for entry 71 on 2026-09-14: old code
    filling a template that asked for placeholders it did not know. A
    restart picks up both halves together.
    """
    template = _TEMPLATES.get(name)
    if template is None:
        template = Template((TEMPLATE_DIR / name).read_text(encoding="utf-8"))
        _TEMPLATES[name] = template
    return template


def _paragraphs(text: str, empty: str) -> str:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
    if not blocks:
        return f'<p class="none">{_esc(empty)}</p>'
    return "\n    ".join(
        "<p>" + _esc(block).replace("\n", "<br>") + "</p>" for block in blocks
    )


def _rows(pairs: Iterable[tuple[str, str]]) -> str:
    return "\n      ".join(
        f"<tr><th scope=\"row\">{_esc(label)}</th><td>{value}</td></tr>"
        for label, value in pairs
    )


def _dash(value: Any) -> str:
    """A value, or an em dash when the database has none. Never a guess."""
    if value is None or value == "":
        return "—"
    return _esc(value)


def _json_block(data: Any) -> str:
    """JSON for a <script type="application/json"> block, safely terminated."""
    return json.dumps(data, indent=2, sort_keys=True).replace("</", "<\\/")


NO_PAIRS = "no pairs yet"


def _one_score(table: dict, entry_id: int) -> str:
    """One population/question cell: the score to two decimals, with its n."""
    row = table.get(entry_id)
    if row is None:
        return NO_PAIRS
    n = int(row["n"])
    return f"{float(row['score']):.2f} over {n} pair{'' if n == 1 else 's'}"


def _score_slot(
    scores: dict[str, dict[str, dict]], population: str, entry_id: int
) -> tuple[str, str, str]:
    """(headline, brief line, note) for one judge population.

    The headline is the 'look' score — *which would you rather look at*, the
    question the gallery is about — and the 'brief' score is shown beside it,
    never folded into it. An entry this population has never judged gets
    :data:`NO_PAIRS` and no number: a score nobody voted on is not a low score,
    it is no score.
    """
    look = scores[population]["look"]
    brief = scores[population]["brief"]
    if entry_id not in look and entry_id not in brief:
        return (
            NO_PAIRS,
            "",
            "A Bradley–Terry score needs pairs; none has been judged.",
        )
    return (
        _one_score(look, entry_id),
        f"closer to its brief: {_one_score(brief, entry_id)}",
        "Bradley–Terry over this population's pairs alone. 1.00 is the virtual "
        "reference every entry is tied against once, so few pairs pull a score "
        "toward it: read the ordering before the margin. Views and likes are "
        "engagement, not judgment, and are not in this number.",
    )


def _brief_line(text: str) -> str:
    """The second question's score, as its own element, or nothing at all."""
    return f'<p class="score-brief">{_esc(text)}</p>' if text else ""


# ---------------------------------------------------------------------------
# The card's standing bars (packet 5.4)
#
# Two lines of "1.39 over 1 pair · 0.85 over 12 pairs" asked a reader to hold
# four numbers and a scale nobody explained. The card shows position instead:
# where this entry sits in the pool that population has actually scored, one
# track per question. The numbers do not go away, they go into the hover text.
# ---------------------------------------------------------------------------

#: The two questions, in the order the card asks them, with the words the card
#: uses for each. 'look' first because it is the gallery's headline question.
_BAR_ROWS = (("look", "rather look at it"), ("brief", "closer to its brief"))

#: Below this distance in percentile the two populations are reading the entry
#: the same way. A third of the pool apart is the line; it is a judgement call,
#: made once here rather than differently on every card.
_AGREE_WITHIN = 0.34


def _standing(table: dict, entry_id: int) -> dict | None:
    """Where one entry stands in one population's scored pool.

    ``{"pct", "rank", "pool", "score", "n"}``, or ``None`` when this population
    has not judged the entry: absent from the table is absent from the pool,
    not bottom of it. Rank is by score descending with ties broken by entry id,
    so a second render of an unchanged database places the marks identically.
    ``pct`` runs 0.0 at the weakest entry to 1.0 at the strongest, and is 0.5 in
    a pool of one, which has nothing to be stronger or weaker than.
    """
    row = table.get(entry_id)
    if row is None:
        return None
    order = sorted(
        table.items(), key=lambda item: (-float(item[1]["score"]), int(item[0]))
    )
    pool = len(order)
    rank = next(
        i for i, (other, _) in enumerate(order, 1) if int(other) == int(entry_id)
    )
    return {
        "pct": 0.5 if pool == 1 else (pool - rank) / (pool - 1),
        "rank": rank,
        "pool": pool,
        "score": float(row["score"]),
        "n": int(row["n"]),
    }


def _evidence_opacity(n: int) -> float:
    """How solid a mark is drawn: the number of pairs behind it, in four steps.

    A mark placed on one pair and a mark placed on twenty would otherwise look
    equally certain, and the first is nearly a guess. Opacity is the only thing
    that carries this on the card; the exact count is in the mark's title.
    """
    if n <= 0:
        return 0.0
    if n == 1:
        return 0.4
    if n <= 3:
        return 0.6
    if n <= 7:
        return 0.8
    return 1.0


def _ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th … 11th, 12th, 13th. For the marks' hover text."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _pairs_phrase(n: int) -> str:
    return f"{n} pair{'' if n == 1 else 's'}"


def _mark(stand: dict, population: str, css_class: str) -> str:
    """One population's mark on one track, at its percentile, with the facts."""
    title = (
        f"{population}: {_ordinal(stand['rank'])} of {stand['pool']} · "
        f"{stand['score']:.2f} over {_pairs_phrase(stand['n'])}"
    )
    return (
        f'<span class="mark {css_class}" style="left:{stand["pct"] * 100:.1f}%;'
        f'opacity:{_evidence_opacity(stand["n"])}" title="{_esc(title)}"></span>'
    )


def _standing_bars(scores: dict[str, dict[str, dict]], entry_id: int) -> str:
    """The card's two tracks: the pool weakest to strongest, and where this is.

    A filled dot is where the humans put the entry, a ring is where the agents
    put it, and the short bar between them is the distance between the two
    readings. A question neither population has judged says so rather than
    drawing a mark at zero.
    """
    rows = []
    for question, label in _BAR_ROWS:
        human = _standing(scores["human"][question], entry_id)
        agent = _standing(scores["agent"][question], entry_id)
        marks = ""
        if human is not None and agent is not None:
            low, high = sorted((human["pct"], agent["pct"]))
            marks += (
                f'<span class="gap" style="left:{low * 100:.1f}%;'
                f'width:{(high - low) * 100:.1f}%"></span>'
            )
        for stand, population, css_class in (
            (human, "humans", "human"),
            (agent, "agents", "agent"),
        ):
            if stand is not None:
                marks += _mark(stand, population, css_class)
        if not marks:
            marks = f'<span class="none-note">{_esc(NO_PAIRS)}</span>'
        rows.append(
            f'<div class="bar-row"><span class="bar-label">{_esc(label)}</span>'
            f'<span class="track">{marks}</span></div>'
        )
    legend = (
        '<p class="legend" title="Each mark sits at the entry\'s percentile among '
        'the entries that population has scored on that question — the pool is the '
        'population\'s own, not the whole gallery.">'
        '<span class="mark human inline"></span> humans '
        '<span class="mark agent inline"></span> agents '
        "· fainter = fewer pairs</p>"
    )
    return '<div class="standing">' + "".join(rows) + legend + "</div>"


def _agreement(scores: dict[str, dict[str, dict]], entry_id: int) -> str:
    """The chip that says whether the two populations read this entry alike.

    On the *look* question alone: that is the card's headline question (see
    :func:`_score_slot`), and a chip that averaged the two would be answering
    neither. Nothing at all when neither population has judged the entry —
    there is no disagreement to report, only silence.
    """
    human = _standing(scores["human"]["look"], entry_id)
    agent = _standing(scores["agent"]["look"], entry_id)
    if human is not None and agent is not None:
        close = abs(human["pct"] - agent["pct"]) < _AGREE_WITHIN
        word, css_class = ("agree", "agree") if close else ("disagree", "disagree")
    elif human is not None:
        word, css_class = "humans only", "only"
    elif agent is not None:
        word, css_class = "agents only", "only"
    else:
        return ""
    return f' <span class="chip {css_class}">{word}</span>'


# ---------------------------------------------------------------------------
# The entry page's compass (this packet)
#
# The card's bars answer each question on its own track, which is all a card
# has room for. The entry page has room for the question the bars cannot ask:
# does a population want to look at the thing *and* think it did what it was
# asked, or does it split the two? One square, one mark per population, right
# for "rather look at it" and up for "closer to its brief", both as the same
# percentile the bars use. A mark needs both questions, so it is the entry
# page's mark and not the card's.
# ---------------------------------------------------------------------------

#: The quadrant, in words, keyed by (strong on look, strong on brief). Chosen
#: once here so that two entries in the same quadrant read the same way in week
#: one and in week twelve.
_QUADRANT_WORDS = {
    (True, True): "looks good, on brief",
    (True, False): "looks good, misses the brief",
    (False, True): "on brief, not much to look at",
    (False, False): "neither",
}

#: Which way each axis runs, spelled out under the words. The square has no
#: tick and no number on it; this line is the whole of its scale.
_COMPASS_AXES = "→ rather look at it · ↑ closer to its brief"

#: Above this percentile a population is on the strong half of an axis. The
#: centre lines are drawn at the same 0.5, so the words and the picture cannot
#: disagree about which quadrant a mark is in.
_COMPASS_STRONG = 0.5


def _compass_title(population: str, look: dict, brief: dict) -> str:
    """The facts behind one mark, for its <title>: both ranks, both scores.

    The square shows position and nothing else, the same trade the card's bars
    make. The numbers are not gone, they are one hover away — and on this page
    they are also spelled out in the two score boxes below.
    """
    return (
        f"{population}: look {_ordinal(look['rank'])} of {look['pool']} "
        f"({look['score']:.2f} over {_pairs_phrase(look['n'])}) · "
        f"brief {_ordinal(brief['rank'])} of {brief['pool']} "
        f"({brief['score']:.2f} over {_pairs_phrase(brief['n'])})"
    )


def _compass(
    scores: dict[str, dict[str, dict]], entry_id: int, *, size: int = 160
) -> str:
    """One square: the *look* percentile across, the *brief* percentile up.

    A filled dot is the humans and a ring is the agents, each placed only when
    that population has scored this entry on **both** questions — one
    coordinate is not a point, and inventing the other would put the entry
    somewhere nobody voted for. The mark grows and solidifies with the evidence
    behind it, counted as the smaller of the two questions' pairs, because the
    thinner question is what the point actually rests on. The line between the
    two marks is the gap, the same disagreement the card's bars draw.

    Pure, and keyed off :func:`_standing`, so the pool is the population's own
    scored entries exactly as on the card, and a second render of an unchanged
    database emits the same bytes.
    """
    #: 96 was the mockup's square; every distance below is a proportion of it,
    #: so changing `size` scales the whole drawing rather than parts of it.
    unit = size / 96
    pad = size / 12
    span = size - 2 * pad

    placed: dict[str, tuple[dict, dict, float, float, float]] = {}
    for population in ("human", "agent"):
        look = _standing(scores[population]["look"], entry_id)
        brief = _standing(scores[population]["brief"], entry_id)
        if look is None or brief is None:
            continue
        opacity = _evidence_opacity(min(look["n"], brief["n"]))
        placed[population] = (
            look,
            brief,
            pad + look["pct"] * span,
            (size - pad) - brief["pct"] * span,
            opacity,
        )

    svg = [
        f'<svg class="compass" viewBox="0 0 {size} {size}" width="{size}" '
        f'height="{size}" role="img" aria-label="how this entry stands on '
        'looks, across, against its brief, up">',
        f'<rect x="0.5" y="0.5" width="{size - 1}" height="{size - 1}" '
        f'rx="{6 * unit:.1f}" class="c-ground"/>',
        f'<line x1="{size / 2:.1f}" y1="{4 * unit:.1f}" x2="{size / 2:.1f}" '
        f'y2="{size - 4 * unit:.1f}" class="c-axis"/>',
        f'<line x1="{4 * unit:.1f}" y1="{size / 2:.1f}" '
        f'x2="{size - 4 * unit:.1f}" y2="{size / 2:.1f}" class="c-axis"/>',
    ]
    if len(placed) == 2:
        (_, _, hx, hy, _), (_, _, ax, ay, _) = placed["human"], placed["agent"]
        svg.append(
            f'<line x1="{hx:.1f}" y1="{hy:.1f}" x2="{ax:.1f}" y2="{ay:.1f}" '
            'class="c-gap"/>'
        )
    for population, label in (("human", "humans"), ("agent", "agents")):
        if population not in placed:
            continue
        look, brief, x, y, opacity = placed[population]
        radius = (5 + 2 * opacity) * unit
        svg.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" '
            f'class="c-mark {population}" style="opacity:{opacity}">'
            f"<title>{_esc(_compass_title(label, look, brief))}</title></circle>"
        )
    if not placed:
        svg.append(
            f'<text x="{size / 2:.1f}" y="{size / 2 + 4 * unit:.1f}" '
            'text-anchor="middle" class="c-none">no pairs</text>'
        )
    svg.append("</svg>")

    words = []
    for population, label in (("human", "humans"), ("agent", "agents")):
        if population not in placed:
            continue
        look, brief, _, _, _ = placed[population]
        phrase = _QUADRANT_WORDS[
            (look["pct"] >= _COMPASS_STRONG, brief["pct"] >= _COMPASS_STRONG)
        ]
        words.append(f'<span class="q {population}">{label}: {phrase}</span>')
    if not words:
        words.append(
            '<span class="q none">neither population has scored this entry on '
            "both questions</span>"
        )
    return (
        '<div class="compass-wrap">'
        + "".join(svg)
        + '<div class="quads">'
        + "".join(words)
        + f'<span class="axes">{_esc(_COMPASS_AXES)}</span></div></div>'
    )


def _attribution(row: sqlite3.Row, config: Config) -> str:
    """The CC BY 4.0 attribution line, generated from the entry's provenance."""
    prompt = " ".join(str(row["prompt"] or "").split())
    if len(prompt) > 70:
        prompt = prompt[:69].rstrip() + "…"
    by = row["submitted_by"] or "an anonymous prompt"
    executor = row["executor"] or "an unrecorded model"
    rules = row["rules_file"] or "unrecorded"
    return (
        f"“{prompt}” — sketchgen entry {row['id']}, prompted by {by}, written by "
        f"{executor} under the {rules} rules file. {config.entry_url(int(row['id']))} "
        f"Licensed {LICENCE}."
    )


# ---------------------------------------------------------------------------
# meta.json
# ---------------------------------------------------------------------------


def _assertions(row: sqlite3.Row) -> list[str]:
    raw = row["assertions_json"]
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [str(word) for word in value] if isinstance(value, list) else []


def _timing(report: dict[str, Any], key: str) -> float | None:
    """One of the gate's timings as a number, or None when it is not there.

    The earliest reports have no ``timings.ms_per_frame`` at all — the frame
    budget came later — and a report written by a crashed run can carry a
    string. Either way the honest answer is null, not a guess.
    """
    value = (report.get("timings") or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _gate_log(attempts: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """What each attempt's gate run said — §7's "and what each attempt's gate
    run said", from report.json where there is one."""
    log: list[dict[str, Any]] = []
    for attempt in attempts:
        report = _gate_report(attempt)
        log.append(
            {
                "attempt": int(attempt["n"]),
                "model": attempt["model"],
                "rules_file": attempt["rules_file"],
                "exit": attempt["gate_exit"],
                "checks": report.get("checks") or {},
                "assertions": {
                    name: bool(result.get("pass"))
                    for name, result in (report.get("assertions") or {}).items()
                    if isinstance(result, dict)
                },
                "evidence": attempt["evidence"],
                "source": f"attempt-{int(attempt['n'])}",
                "prompt_tokens": attempt["prompt_tokens"],
                "completion_tokens": attempt["completion_tokens"],
                "wall_s": attempt["wall_s"],
                # How long the gate's own run took, and what a virtual frame
                # cost inside it. 29 published entries are over the budget the
                # gate now enforces (100 ms a frame) and their pages should not
                # start themselves; this is what the page reads to decide.
                # Inside "gate" on purpose, so META_KEYS does not change.
                "total_s": _timing(report, "total_s"),
                "ms_per_frame": _timing(report, "ms_per_frame"),
            }
        )
    return log


def _meta(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    attempts: list[sqlite3.Row],
    config: Config,
    parent: dict[int, int | None],
    children: dict[int, list[int]],
) -> dict[str, Any]:
    entry_id = int(row["id"])
    source = _source_dir(row, attempts)
    job = _job(conn, int(row["job_id"]))
    line = _lineage_row(conn, entry_id)
    kids = children.get(entry_id, [])
    generation = int(line["generation"]) if line is not None else 1
    meta: dict[str, Any] = {
        "entry_id": entry_id,
        "job_id": int(row["job_id"]),
        "state": row["state"],
        "prompt": row["prompt"],
        "brief": row["brief"],
        "statement": _statement(row, source) or None,
        "submitted_by": row["submitted_by"],
        "planner": row["planner"],
        "planner_prompt_version": row["planner_prompt_version"],
        "executor": row["executor"],
        "executor_prompt_version": row["executor_prompt_version"],
        "rules_file": row["rules_file"],
        "assertions": _assertions(row),
        "gate": _gate_log(attempts),
        "attempts": int(row["attempts"] or len(attempts)),
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "wall_s": row["wall_s"],
        "shape": row["shape"],
        "seed": row["seed"],
        "lineage": {
            "parent_entry_id": parent.get(entry_id),
            "children": kids,
            "generation": generation,
            "root_entry_id": _root_of(parent, entry_id),
            "critique_by": line["critique_by"] if line is not None else None,
            "critique": line["critique"] if line is not None else None,
        },
        "source": {
            "entry": config.entry_url(entry_id),
            "repository": config.tree_url(entry_id),
            "sketch_js": "sketch/sketch.js",
            "index_html": "sketch/index.html",
            "attempts": [f"attempt-{int(a['n'])}" for a in attempts],
        },
        "created_utc": row["created_utc"],
        "published_utc": row["published_utc"],
        "publish_commit": row["publish_commit"],
        "licence": LICENCE,
        "attribution": _attribution(row, config),
        "last_error": job["last_error"] if job is not None else None,
    }
    return {key: meta[key] for key in META_KEYS}


# ---------------------------------------------------------------------------
# The entry page
# ---------------------------------------------------------------------------


def _byline(row: sqlite3.Row) -> str:
    by = _esc(row["submitted_by"] or "an operator")
    planner = _esc(row["planner"] or "no planner on record")
    executor = _esc(row["executor"] or "no executor on record")
    # What it took to get here — the attempt count, and whether the run was
    # held or published — is the workshop's business, not a visitor's. It is
    # still on the page, under Provenance, where the machine-facing fields
    # live.
    return f"Prompt by {by}. Planned by {planner}, written by {executor}."


def _critic_chip(who: Any) -> str:
    """Who asked for this revision, as a chip: a model, or a person.

    ``critique_by`` carries a model tag (``gemma4:e4b``) when a critic model
    wrote the critique and a plain username when a person did. The colon is the
    whole of the test, and the chip drops the size suffix: the line is about
    which critic, not which quantisation.
    """
    name = " ".join(str(who or "").split())
    if not name:
        return '<span class="chip">unknown</span>'
    if ":" in name:
        return f'<span class="chip model">{_esc(name.split(":", 1)[0])}</span>'
    return f'<span class="chip person">{_esc(name)}</span>'


def _title_parts(prompt: Any) -> tuple[str, list[str]]:
    """The prompt as a title wants it: the root sentence, then the revisions.

    The split comes first and the whitespace collapse second. ``Revise:`` only
    counts at the start of a line, which is the whole reason a prompt with the
    word in the middle of a sentence stays in one piece — collapse the newlines
    first and there are no line starts left to split on.
    """
    root, revisions = lineage.split_prompt(str(prompt or ""))
    return " ".join(root.split()), revisions


def _subtitle(revisions: list[str], meta: dict[str, Any]) -> str:
    """The ``p.sub`` under the title: the latest revision, and who asked for it.

    A generation-10 prompt is ten sentences stapled together and reads as none
    of them. The heading takes the root; this line takes the newest amendment —
    the one that made *this* entry rather than its parent — and leaves the rest
    counted, for the lineage panel below to show in full.
    """
    if not revisions:
        return ""
    link = meta["lineage"]
    earlier = len(revisions) - 1
    tail = f" · generation {_esc(link['generation'])}"
    if earlier:
        tail += (
            f" · {earlier} earlier revision{'s' if earlier != 1 else ''} "
            "in the lineage below"
        )
    return (
        '<p class="sub"><span class="label">Revise:</span> '
        f"<em>{_esc(revisions[-1])}</em> {_critic_chip(link['critique_by'])}"
        f'<span class="dim">{tail}</span></p>'
    )


#: The critique form, where the apology used to be (plan §5.2).
#:
#: Every state in the HTML and hidden, as the composer keeps them, and the
#: entry's own id on the section because that is the one thing the POST needs
#: that the script cannot read off the page's address. The rules line under the
#: textarea is written by ``gallery.js`` as the visitor types; empty here,
#: because a rule the page states before anyone has typed is not a finding.
CRITIQUE_FORM = """<section class="panel critique-form" data-critique="{entry_id}" hidden>
    <h2>Critique this sketch</h2>
    <p class="signed-out-line" data-critique-out>
      <a class="login" data-login href="../../index.html">Sign in with GitHub</a> to ask for a
      revision. One sentence becomes the next generation's prompt.
    </p>
    <div data-critique-in hidden>
      <p class="note">One sentence, under 40 words, no code.</p>
      <label class="rule" for="critique-text">Say what should change, not how to write it.</label>
      <textarea id="critique-text" rows="2" data-critique-text></textarea>
      <p class="rule" data-critique-rule></p>
      <div class="preview">
        <span class="label">the child's prompt</span>
        {child_prompt}
        <p class="revise"><span class="label">Revise:</span> <em data-critique-echo>…</em></p>
      </div>
      <p class="actions">
        <button type="button" class="primary" data-critique-send>Submit Critique</button>
      </p>
    </div>
    <div data-critique-sent hidden>
      <div class="receipt">
        <p><strong>Critique recorded.</strong> Queued for review.</p>
        <p>Your username appears on the child entry as the critic, the way the critic model's
        name appears on this one.</p>
      </div>
      <div class="preview">
        <span class="label">what you asked for</span>
        <p class="revise"><em data-critique-echo-sent>…</em></p>
      </div>
    </div>
  </section>"""


def _child_prompt(title: str, revisions: list[str]) -> str:
    """The prompt this entry's child would carry, as far as the page knows it.

    The root sentence, then every revision already stapled to it, in the shape
    :func:`_subtitle` renders one of them in — so the live line the script adds
    underneath is the same kind of line as the ones above it, and a
    generation-4 parent does not pretend its child inherits one sentence.
    """
    lines = [f"<p>{_esc(title)}</p>"]
    for revision in revisions:
        lines.append(
            '<p class="revise"><span class="label">Revise:</span> '
            f"<em>{_esc(revision)}</em></p>"
        )
    return "\n        ".join(lines)


def _critique_form(
    row: sqlite3.Row,
    meta: dict[str, Any],
    title: str,
    revisions: list[str],
    config: Config,
) -> str:
    """The form, or nothing at all: a rejected parent gets no box.

    ``lineage.spawn`` refuses a rejected entry, so a form on that page would be
    an offer the pipeline will not honour. With no write path in
    ``config.json`` there is nothing to submit to and the panel is not written
    either — the entry page then simply has one panel fewer.
    """
    if not config.write_path or row["state"] not in lineage.SPAWNABLE:
        return ""
    return CRITIQUE_FORM.format(
        entry_id=int(row["id"]),
        child_prompt=_child_prompt(title, revisions),
    )


#: Over either of these the stage waits for a click instead of starting itself.
#: The second is the gate's own frame budget (gate/README.md: a virtual frame
#: costing more than 100 ms is a sketch the machine cannot keep up with); the
#: first catches the runs that are slow without any one frame being slow. 29
#: published entries are over one or the other, and every one of them used to
#: start playing the moment the page opened.
HEAVY_TOTAL_S = 30.0
HEAVY_MS_PER_FRAME = 100.0


def _heavy(meta: dict[str, Any]) -> dict[str, float | None] | None:
    """The published attempt's timings, when the gate found it expensive.

    The published attempt is the last one, the same attempt ``_source_dir``
    takes; ``None`` means an ordinary sketch and an ordinary autoplaying stage.
    """
    log = meta.get("gate") or []
    if not log:
        return None
    attempt = log[-1]
    total_s = attempt.get("total_s")
    ms_per_frame = attempt.get("ms_per_frame")
    over = (total_s is not None and total_s > HEAVY_TOTAL_S) or (
        ms_per_frame is not None and ms_per_frame > HEAVY_MS_PER_FRAME
    )
    if not over:
        return None
    return {"total_s": total_s, "ms_per_frame": ms_per_frame}


def _heavy_chip(heavy: dict[str, float | None]) -> str:
    ms = heavy.get("ms_per_frame")
    if ms is not None:
        text = f"heavy · {ms:.0f} ms per frame"
    else:
        # No frame rate in the report — the older runs have none — so say the
        # measurement that did put it over the line rather than invent one.
        text = f"heavy · {heavy.get('total_s') or 0:.0f} s to run"
    return f'<span class="chip heavy">{_esc(text)}</span>'


#: Microphone *input* — the one thing a sandboxed, opaque-origin sketch frame
#: cannot do. A published sketch runs in an ``sandbox="allow-scripts"`` iframe with
#: no ``allow="microphone"``, so ``getUserMedia`` is refused and a listening sketch
#: reads a dead mic; it only works opened in its own top-level tab, where the
#: Pages origin (https) is a secure context the browser will grant the mic. Making
#: sound — ``p5.Oscillator``, ``loadSound`` — works in the frame after a click and
#: is deliberately NOT matched here: only listening has to leave the page.
_MIC_RE = re.compile(r"p5\.AudioIn|getUserMedia|mediaDevices")


def _needs_mic(source: Path | None) -> bool:
    """True when the sketch at ``source`` opens the microphone (see :data:`_MIC_RE`)."""
    if source is None:
        return False
    text = ""
    for name in ("sketch.js", "index.html"):
        try:
            text += (source / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return bool(_MIC_RE.search(text))


def _frame(
    has_sketch: bool,
    title: str,
    *,
    heavy: dict[str, float | None] | None = None,
    has_strip: bool = False,
    mic: bool = False,
) -> str:
    """The stage: an iframe that starts itself, or a frame that waits.

    A sketch the gate had to grind through does not get to start itself the
    moment somebody opens the page. It renders as the strip with a play button
    — the same run-in-place helper the ledger tiles use — and says why.

    A microphone sketch never starts in the embedded frame at all: the mic is
    refused an opaque, sandboxed origin, so the strip becomes a link that opens
    the sketch in its own tab, where the origin is real and the browser can grant
    it. Making sound is not this — see :data:`_MIC_RE`.
    """
    if not has_sketch:
        return (
            '<p class="none">The sketch source is not in this checkout, so there '
            "is nothing to run here.</p>"
        )
    if mic:
        poster = (
            f'<img src="strip.png" alt="four frames from {_esc(title)}">'
            if has_strip else "<span data-play-label></span>"
        )
        return (
            '<div class="stage-run stage-mic">\n'
            '      <a class="play" href="sketch/" target="_blank" rel="noopener"'
            f' aria-label="run {_esc(title)} in a new tab">{poster}'
            '<span class="play-label">run in a tab ▸</span></a>\n'
            '      <p class="run-note">This sketch listens to the microphone, which '
            "a browser only opens for a page in its own tab — so it runs there, not "
            "in this embedded frame.</p>\n"
            "    </div>"
        )
    if heavy is not None and has_strip:
        return (
            '<div class="stage-run" data-stage-run>\n'
            '      <button type="button" class="play" data-play data-run-href="sketch/"'
            ' data-run-name="this sketch" aria-label="run this sketch">'
            f'<img src="strip.png" alt="four frames from {_esc(title)}">'
            '<span data-play-label></span></button>\n'
            # The note lives inside the box the frame appears in, because that
            # is where runInPlace looks for it.
            '      <p class="run-note" data-run-note></p>\n'
            '    </div>\n'
            f'    <p class="stage-heavy">{_heavy_chip(heavy)} This one is slow to '
            "draw, so it waits for a click rather than starting itself."
            "</p>"
        )
    return (
        f'<iframe class="sketch" src="sketch/" title="{_esc(title)}" '
        'loading="lazy" sandbox="allow-scripts"></iframe>'
    )


# ---------------------------------------------------------------------------
# The ledger: the lineage panel
# ---------------------------------------------------------------------------

#: Ancestors strictly between the root and the grandparent fold away when there
#: are at least this many. A single one reads better inline than behind a
#: disclosure that says "1 generation folded".
LEDGER_FOLD_MIN = 2


def _ledger_index(conn: sqlite3.Connection) -> dict[str, Any]:
    """The same entries ``lineage.json`` carries, for the server's own use.

    The panel's ancestry is static HTML written at publish time, so it is built
    from exactly the shape the script later paints the rest of the panel from.
    One producer, one vocabulary: a row the server draws and a row the script
    draws cannot disagree about a generation or a chip.
    """
    return _lineage_index(conn)["entries"]


def _ledger_chain(index: dict[str, Any], entry_id: int) -> list[int]:
    """Root first, this entry last. Cycle-safe, because the data is not."""
    chain: list[int] = []
    seen: set[int] = set()
    walk: int | None = int(entry_id)
    while walk is not None and walk not in seen:
        seen.add(walk)
        chain.append(walk)
        item = index.get(str(walk))
        walk = item.get("parent") if item else None
    chain.reverse()
    return chain


def _ledger_generation(item: dict[str, Any] | None) -> int:
    return int((item or {}).get("generation") or 1)


def _generation_label(item: dict[str, Any] | None, *, is_root: bool) -> str:
    """"root", or the generation the database recorded.

    THE NUMBERING IS INCONSISTENT AND THAT IS ON PURPOSE. ``lineage`` counts a
    root as generation 0, so a root's first child is recorded as generation 1;
    a root has no ``lineage`` row at all and ``meta.json`` and ``lineage.json``
    both write it down as generation 1 as well. Two entries one step apart
    therefore both say "generation 1". Every meta.json already published
    carries those numbers and they are frozen at publish time, so nothing here
    renumbers anything: the ledger prints the root as "root" with no number,
    which is the one place the collision showed, and prints every other row's
    recorded generation exactly as the database has it.
    """
    if is_root:
        return "root"
    return f"generation {_ledger_generation(item)}"


def _ledger_critic_chip(item: dict[str, Any] | None, *, is_root: bool) -> str:
    """Who asked for this generation: a model in the agent colour, a person in
    the ok colour. The root's asker is whoever submitted the prompt."""
    item = item or {}
    who = item.get("submitted_by") if is_root else item.get("critique_by")
    who = str(who or "").strip()
    if not who:
        return ""
    if ":" in who:
        # "gemma4:e4b" is one model at one size; the size is in the Provenance
        # table and would be noise five times down a column.
        return f'<span class="chip model">{_esc(who.split(":", 1)[0])}</span>'
    return f'<span class="chip person">{_esc(who)}</span>'


def _ledger_chips(item: dict[str, Any] | None, *, is_root: bool) -> str:
    chips = [_ledger_critic_chip(item, is_root=is_root)]
    if not (item or {}).get("public"):
        # Still a generation, still counted: it just has no page to link to.
        chips.append('<span class="chip unpublished">not published</span>')
    return " ".join(chip for chip in chips if chip)


def _ledger_tile(entry_id: int, item: dict[str, Any] | None, width: str) -> str:
    """The first frame of the strip, as a button that runs the sketch here.

    ``object-fit: cover`` with ``object-position: left`` on a 64:45 box shows
    the first of the strip's four frames without a second file being made.
    """
    if not (item or {}).get("public"):
        return f'<div class="ledger-tile {width} blank" aria-hidden="true"></div>'
    mic = ' data-run-mic="1"' if (item or {}).get("mic") else ""
    return (
        f'<div class="ledger-tile {width}">'
        f'<button type="button" class="play" data-play '
        f'data-run-href="../{entry_id}/sketch/"{mic} '
                f'aria-label="run entry {entry_id}" data-run-name="entry {entry_id}">'
        f'<img src="../{entry_id}/strip.png" loading="lazy" '
        f'alt="the first frame of entry {entry_id}">'
        f"<span data-play-label></span></button></div>"
    )


def _ledger_body(
    entry_id: int,
    item: dict[str, Any] | None,
    *,
    is_root: bool,
    here: bool,
    extra: str = "",
) -> str:
    item = item or {}
    lines: list[str] = []
    if is_root:
        prompt = str(item.get("root_prompt") or "").strip()
        if prompt:
            lines.append(f'<p class="ledger-prompt">{_esc(prompt)}</p>')
    else:
        critique = " ".join(str(item.get("critique") or "").split())
        if critique:
            # Full length, never clamped: the critique is the reason this
            # generation exists and a clamped one reads as a caption.
            lines.append(
                '<p class="ledger-critique"><span class="revise">Revise:</span> '
                f"<em>{_esc(critique)}</em></p>"
            )
    if here or not item.get("public"):
        name = f"entry {entry_id}"
    else:
        name = f'<a href="../{entry_id}/">entry {entry_id}</a>'
    meta_bits = [name, _esc(_generation_label(item, is_root=is_root))]
    if extra:
        meta_bits.append(extra)
    chips = _ledger_chips(item, is_root=is_root)
    if chips:
        meta_bits.append(chips)
    lines.append('<p class="ledger-meta">' + " · ".join(meta_bits) + "</p>")
    return '<div class="ledger-text">' + "".join(lines) + "</div>"


def _ledger_row(
    entry_id: int,
    item: dict[str, Any] | None,
    *,
    is_root: bool = False,
    here: bool = False,
    extra: str = "",
) -> str:
    classes = "ledger-row" + (" here" if here else "")
    current = ' aria-current="true"' if here else ""
    return (
        f'<li class="{classes}"{current}>'
        + _ledger_tile(entry_id, item, "narrow")
        + _ledger_body(entry_id, item, is_root=is_root, here=here, extra=extra)
        + "</li>"
    )


def _fold_summary(folded: list[int], index: dict[str, Any]) -> str:
    ids = ", ".join(str(one) for one in folded)
    critics: list[str] = []
    for one in folded:
        who = str((index.get(str(one)) or {}).get("critique_by") or "").strip()
        who = who.split(":", 1)[0] if who else ""
        if who and who not in critics:
            critics.append(who)
    if not critics:
        by = ""
    elif len(critics) == 1:
        by = f" · all by {critics[0]}"
    else:
        by = " · by " + " and ".join([", ".join(critics[:-1]), critics[-1]])
    plural = "s" if len(folded) != 1 else ""
    return _esc(f"{len(folded)} generation{plural} folded · {ids}{by}")


def _ledger_fold(folded: list[int], index: dict[str, Any]) -> str:
    rows = "".join(
        _ledger_row(one, index.get(str(one))) for one in folded
    )
    return (
        '<li class="ledger-folded"><details class="fold">'
        f'<summary class="fold-label">{_fold_summary(folded, index)}</summary>'
        f'<ol class="ledger">{rows}</ol>'
        "</details></li>"
    )


def _line_href(root: int, line_page: bool) -> str | None:
    return f"../../lines/{root}.html" if line_page else None


def _ledger_heading(
    entry_id: int,
    index: dict[str, Any],
    root: int,
    deepest: int,
    line_page: bool,
) -> str:
    href = _line_href(root, line_page)
    where = (
        f'<a href="{href}">entry {root}</a>' if href else f"entry {root}"
    )
    # The deepest generation is the one number in the heading that goes stale:
    # a page rendered today is read after the line has grown. It sits in its
    # own span so the script can replace the number and leave the link alone.
    depth = f'<span data-ledger-deepest>{deepest}</span>'
    if entry_id == root:
        if deepest > 1:
            text = f"the root of a line {depth} generations deep"
        else:
            text = "a root prompt, no children yet"
    else:
        generation = _ledger_generation(index.get(str(entry_id)))
        text = f"generation {generation} of {depth} in the line from {where}"
    return f'<h2>Lineage · <span data-ledger-head>{text}</span></h2>'


def _lineage_panel(
    meta: dict[str, Any],
    line_page: bool,
    index: dict[str, Any] | None = None,
) -> str:
    """The ledger: one row per generation, oldest first, this entry highlighted.

    What the server writes is the part that cannot change — the ancestry was
    settled the moment this entry existed. Siblings, children and descendants
    arrive later than the page does, so their containers go out empty with the
    ids the script needs, and ``gallery.js`` paints them from ``lineage.json``.
    With no script the page still carries the whole ancestry and the plain
    "Children: entry 124" line it has always had.
    """
    lineage = meta["lineage"]
    entry_id = int(meta["entry_id"])
    index = index or {}
    mine = index.get(str(entry_id))
    if mine is None:
        # No index (a caller that has not built one): the old text panel is
        # still true, and a panel that says less is better than one that lies.
        return _lineage_text_panel(meta, line_page)

    chain = _ledger_chain(index, entry_id)
    root = chain[0]
    line = [
        int(key)
        for key, item in index.items()
        if item.get("root") == root
    ]
    deepest = max(
        [_ledger_generation(index.get(str(one))) for one in line] or [1]
    )
    parent_id = index[str(entry_id)].get("parent")

    rows: list[str] = []
    ancestors = chain[:-1]
    folded: list[int] = []
    if len(ancestors[1:-2]) >= LEDGER_FOLD_MIN:
        folded = ancestors[1:-2]
    shown = [one for one in ancestors if one not in folded]
    for one in shown:
        item = index.get(str(one))
        extra = ""
        kids = list((item or {}).get("children") or [])
        if len(kids) > 1 and one != parent_id:
            # The other branch is a whole line of its own; the line page draws
            # it, and the ledger says where to look.
            href = _line_href(root, line_page)
            extra = (
                f'<a href="{href}">forked</a>' if href else "forked"
            )
        rows.append(
            _ledger_row(one, item, is_root=(one == root), extra=extra)
        )
        if folded and one == root:
            rows.append(_ledger_fold(folded, index))
    rows.append(
        _ledger_row(entry_id, mine, is_root=(entry_id == root), here=True)
    )

    kids = list(lineage["children"] or [])
    if kids:
        links = ", ".join(f'<a href="../{kid}/">entry {kid}</a>' for kid in kids)
        fallback = f"<p class=\"ledger-plain\" data-ledger-plain>Children: {links}.</p>"
        after = f"After this entry: {_count(len(kids), 'child', 'children')}"
    else:
        fallback = '<p class="ledger-plain" data-ledger-plain>No children yet.</p>'
        after = "No children yet."

    href = _line_href(root, line_page)
    whole = (
        f'<a href="{href}">The whole line from entry {root}</a>.'
        if href
        else "This is the whole line so far."
    )
    return "\n    ".join(
        [
            _ledger_heading(entry_id, index, root, deepest, line_page),
            f'<div class="ledger-panel" data-ledger="{entry_id}" '
            f'data-ledger-root="{root}"'
            + (f' data-ledger-parent="{parent_id}"' if parent_id else "")
            + ">",
            f'<ol class="ledger">{"".join(rows)}</ol>',
            '<div class="ledger-forks" data-ledger-forks hidden></div>',
            '<hr class="ledger-rule">',
            f'<p class="ledger-after" data-ledger-after>{_esc(after)}</p>',
            '<div class="ledger-grid" data-ledger-tiles hidden></div>',
            fallback,
            "</div>",
            '<p class="note">Click a frame to run that sketch in place; one at a '
            f"time. {whole}</p>",
        ]
    )


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _lineage_text_panel(meta: dict[str, Any], line_page: bool) -> str:
    """The panel as it was before the ledger: paragraphs, no index needed."""
    lineage = meta["lineage"]
    parts = ["<h2>Lineage</h2>"]
    parent_id = lineage["parent_entry_id"]
    if parent_id:
        parts.append(
            f'<p>Child of <a href="../{parent_id}/">entry {parent_id}</a>, '
            f"generation {_esc(lineage['generation'])}.</p>"
        )
    else:
        parts.append(
            f"<p>A root prompt: generation {_esc(lineage['generation'])}, no parent.</p>"
        )
    if lineage["critique"]:
        parts.append(
            f"<p>Spawned by a critique from {_esc(lineage['critique_by'] or 'unknown')}:</p>"
        )
        parts.append(_paragraphs(str(lineage["critique"]), ""))
    kids = lineage["children"]
    if kids:
        links = ", ".join(
            f'<a href="../{kid}/">entry {kid}</a>' for kid in kids
        )
        parts.append(f"<p>Children: {links}.</p>")
    else:
        parts.append("<p>No children yet.</p>")
    if line_page:
        root = lineage["root_entry_id"]
        parts.append(
            f'<p><a href="../../lines/{root}.html">The whole line from entry '
            f"{root}</a></p>"
        )
    return "\n    ".join(parts)


def _provenance_rows(meta: dict[str, Any]) -> str:
    lineage = meta["lineage"]
    gate_lines = "<br>".join(
        _esc(
            f"attempt {item['attempt']}: gate exit "
            f"{item['exit'] if item['exit'] is not None else 'not run'}"
            + (
                " — " + ", ".join(
                    f"{name} {'pass' if ok else 'fail'}"
                    for name, ok in sorted(item["assertions"].items())
                )
                if item["assertions"]
                else ""
            )
        )
        for item in meta["gate"]
    ) or "—"
    kids = ", ".join(str(kid) for kid in lineage["children"]) or "none"
    pairs: list[tuple[str, str]] = [
        ("Entry", _esc(meta["entry_id"])),
        ("Job", _esc(meta["job_id"])),
        ("State", _esc(meta["state"])),
        ("Prompt", _esc(meta["prompt"])),
        ("Brief", _esc(meta["brief"]) or "—"),
        ("Statement", _esc(meta["statement"]) or "—"),
        ("Submitted by", _dash(meta["submitted_by"])),
        (
            "Planner",
            f"{_dash(meta['planner'])} · prompt {_dash(meta['planner_prompt_version'])}",
        ),
        (
            "Executor",
            f"{_dash(meta['executor'])} · prompt {_dash(meta['executor_prompt_version'])}",
        ),
        ("Rules file", _dash(meta["rules_file"])),
        ("Assertions", _esc(", ".join(meta["assertions"])) or "none"),
        ("Gate", gate_lines),
        ("Attempts", _dash(meta["attempts"])),
        ("Prompt tokens", _dash(meta["prompt_tokens"])),
        ("Completion tokens", _dash(meta["completion_tokens"])),
        ("Wall seconds", _dash(meta["wall_s"])),
        ("Node shape", _dash(meta["shape"])),
        ("Seed", _dash(meta["seed"])),
        (
            "Lineage",
            f"parent {_dash(lineage['parent_entry_id'])} · children {_esc(kids)} · "
            f"generation {_dash(lineage['generation'])} · critique by "
            f"{_dash(lineage['critique_by'])}",
        ),
        (
            "Source",
            f'<a href="{_esc(meta["source"]["repository"])}">repository</a> · '
            f'<a href="sketch/sketch.js">sketch.js</a> · '
            f'<a href="sketch/index.html">index.html</a> · attempts '
            f'{_esc(", ".join(meta["source"]["attempts"])) or "—"}',
        ),
        ("Created (UTC)", _dash(meta["created_utc"])),
        ("Published (UTC)", _dash(meta["published_utc"])),
        ("Publish commit", _dash(meta["publish_commit"])),
        (
            "Licence",
            f'<a href="{LICENCE_URL}">{_esc(meta["licence"])}</a>',
        ),
        ("Attribution", _esc(meta["attribution"])),
    ]
    if meta["last_error"]:
        pairs.append(("Last error", _esc(meta["last_error"])))
    return _rows(pairs)


def _source_rows(meta: dict[str, Any]) -> str:
    return _rows(
        [
            (
                "Repository",
                f'<a href="{_esc(meta["source"]["repository"])}">'
                f'{_esc(meta["source"]["repository"])}</a>',
            ),
            ("Raw sketch", '<a href="sketch/sketch.js">sketch/sketch.js</a>'),
            ("Raw page", '<a href="sketch/index.html">sketch/index.html</a>'),
            ("Attempts", _dash(meta["attempts"])),
            (
                "Licence",
                f'<a href="{LICENCE_URL}">{_esc(LICENCE)}</a>',
            ),
            ("Attribution", _esc(meta["attribution"])),
        ]
    )


def render_entry(
    conn: sqlite3.Connection,
    entry_id: int,
    dest_dir: str | Path,
    config: Config | None = None,
    *,
    publishing: bool = False,
) -> Path:
    """Write ``dest_dir/e/<entry_id>/`` — the entry page and its artefacts.

    ``publishing=True`` is what the publisher passes: it admits a ``held``
    entry and renders it as published, because the row only becomes
    published after the push of these very files succeeds.

    ``dest_dir`` is the gallery checkout root, not the entry directory: this is
    the call packet 3.2's publisher makes, and the directory it commits is the
    path returned.
    """
    dest = Path(dest_dir)
    config = _resolve_config(dest, config)
    row = _entry(conn, int(entry_id), publishing=publishing)
    attempts = _attempt_rows(conn, int(row["job_id"]))
    parent, children = _forest(conn, admit=int(entry_id))
    written = _Written(dest)
    try:
        out = _write_entry(conn, row, attempts, dest, config, parent, children, written)
    except Exception:
        written.undo()
        raise
    guard(dest, written)
    return out


def _write_entry(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    attempts: list[sqlite3.Row],
    dest: Path,
    config: Config,
    parent: dict[int, int | None],
    children: dict[int, list[int]],
    written: _Written,
) -> Path:
    entry_id = int(row["id"])
    out = dest / "e" / str(entry_id)
    written.mkdir(out)
    source = _source_dir(row, attempts)
    meta = _meta(conn, row, attempts, config, parent, children)

    has_sketch = False
    if source is not None:
        sketch_js = source / "sketch.js"
        index_html = source / "index.html"
        if sketch_js.is_file() and index_html.is_file():
            written.copy(sketch_js, out / "sketch" / "sketch.js")
            # Verbatim, with one exception: a page that loads p5.sound gets
            # the shim that lets it start inside a sandboxed frame on WebKit
            # (soundshim.py). Entries published before the executor wrote it
            # pick it up here, on the next render-all; every other page is
            # the bytes the gate ran.
            page = index_html.read_text(encoding="utf-8")
            shimmed = soundshim.with_shim(page)
            if shimmed == page:
                written.copy(index_html, out / "sketch" / "index.html")
            else:
                written.write_text(out / "sketch" / "index.html", shimmed)
            has_sketch = True
    has_strip = False
    for which, name in (("strip", "strip.png"), ("png", "gate.png")):
        artefact = _artefact(row, attempts, which)
        if artefact is not None:
            written.copy(artefact, out / name)
            has_strip = has_strip or which == "strip"

    statement = meta["statement"] or ""
    written.write_text(
        out / "statement.md",
        (statement + "\n") if statement else "(the executor wrote no statement)\n",
    )
    written.write_text(out / "meta.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")

    scores = _all_scores(conn)
    human_value, human_brief, human_note = _score_slot(scores, "human", entry_id)
    agent_value, agent_brief, agent_note = _score_slot(scores, "agent", entry_id)
    root = meta["lineage"]["root_entry_id"]
    has_line = bool(_descendants(children, root))
    # The heading is the root prompt and the subtitle is the newest revision;
    # the accumulated prompt is untouched in meta.json and in Provenance.
    title, revisions = _title_parts(row["prompt"] or f"entry {entry_id}")
    if not title:
        title = f"entry {entry_id}"
    failed_note = ""
    offplan = _offplan(row)
    if offplan:
        # It ran. It threw nothing, it did not freeze, it stayed inside the
        # frame budget — it simply is not what the plan predicted, and the plan
        # was written by a model. Saying which assertion it diverged on is a
        # description; calling it a rejection would be a verdict this gallery
        # has no business handing down (see the note under Judgment: two
        # populations, two scores, never one aggregate).
        failed_note = (
            '<p class="chip offplan">OFF-PLAN — this sketch runs. It differs from '
            "the plan the planner wrote for it.</p>"
        )
    elif row["state"] == "failed-kept":
        reason = _rejection_reason(conn, row)
        failed_note = (
            '<p class="chip failed">REJECTED — kept, because a gallery that only '
            f"shows successes is not a record of anything: {_esc(reason)}</p>"
        )
    elif row["state"] == "rejected":
        # One line, under the stage, in the operator's own words. The chip in
        # stage-meta says the state; this says why, because "rejected" without
        # a reason is a verdict with no evidence behind it.
        failed_note = (
            '<p class="chip rejected">Rejected by the operator: '
            f"{_esc(_rejection_reason(conn, row))}</p>"
        )

    page = _template("entry.html").substitute(
        root="../../",
        page_title=_esc(title[:80]),
        entry_id=entry_id,
        title=_esc(title),
        subtitle=_subtitle(revisions, meta),
        byline=_byline(row),
        failed_note=failed_note,
        frame=_frame(has_sketch, title, heavy=_heavy(meta), has_strip=has_strip,
                     mic=has_sketch and _needs_mic(source)),
        seed=_dash(meta["seed"]),
        state_chip=_state_chip(row["state"]),
        brief=_paragraphs(str(row["brief"] or ""), "No brief was recorded for this job."),
        statement_model=_esc(row["executor"] or "an unrecorded model"),
        statement=_paragraphs(statement, "The executor wrote no statement."),
        human_value=_esc(human_value),
        human_brief=_brief_line(human_brief),
        human_note=_esc(human_note),
        agent_value=_esc(agent_value),
        agent_brief=_brief_line(agent_brief),
        agent_note=_esc(agent_note),
        # Above the two boxes, not instead of them: the square says where the
        # entry sits on both questions at once, the boxes keep the numbers.
        compass=_compass(scores, entry_id),
        compare_href=f"../../compare.html?a={entry_id}",
        # The clean URL, twice: the href and the text under this page's own
        # QR code. The kiosk's code is the one that carries ?kiosk — somebody
        # scanning a laptop on a lectern did not scan a projection, and the
        # param would be a lie in the only place the difference is measurable.
        entry_url=_esc(config.entry_url(entry_id)),
        critique=_critique_form(row, meta, title, revisions, config),
        source_rows=_source_rows(meta),
        provenance_rows=_provenance_rows(meta),
        lineage=_lineage_panel(meta, has_line, _ledger_index(conn)),
    )
    written.write_text(out / "index.html", page)
    return out


# ---------------------------------------------------------------------------
# The grid, the failures, compare, the lines
# ---------------------------------------------------------------------------


def _search_text(entry_id: int, row: sqlite3.Row) -> str:
    """Everything the grid's search box matches a card on, as one string.

    Lowercased and whitespace-collapsed here so that the script does neither:
    it lowercases the query once and asks for substrings, which is the whole
    of the matching.

    The brief is in here and nowhere else on the card. That is the point of
    the attribute: a card shows a prompt, but the entry is *about* what the
    brief says, so searching for a word from a brief ought to find it. The
    statement stays out — it is paragraphs, and every card would carry them.
    """
    parts = [
        # both ways someone writes an entry number
        f"entry {entry_id}",
        f"#{entry_id}",
        row["prompt"] or "",
        row["brief"] or "",
        row["rules_file"] or "",
        row["executor"] or "",
        row["submitted_by"] or "",
    ]
    return _esc(" ".join(" ".join(str(part) for part in parts).split()).lower())


def _rejection_reason(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Why this entry is on the rejections page, in one line.

    An operator rejection carries the sentence a person typed, in the entry's
    own ``reject_reason`` (migration 007). A gate rejection has no such
    sentence — nobody wrote one — so the job's ``last_error`` stands in, which
    is the gate's last word on it and what this page has always shown.
    """
    if str(row["state"]) == "rejected":
        return str(row["reject_reason"] or "reason not recorded")
    job = _job(conn, int(row["job_id"]))
    return str((job["last_error"] if job is not None else None) or "reason not recorded")


def _card(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    failed: bool,
    template: Template,
    scores: dict[str, dict[str, dict]],
) -> str:
    entry_id = int(row["id"])
    # The card shows the root prompt and, under it, the revision that made this
    # entry. data-search below still carries the whole accumulated prompt, so a
    # word from a fourth-generation critique still finds the card.
    prompt, revisions = _title_parts(row["prompt"] or f"entry {entry_id}")
    if not prompt:
        prompt = f"entry {entry_id}"
    revision = ""
    if revisions:
        # The generation comes from the lineage row, which is what the entry
        # page and meta.json say, rather than from counting the headings: the
        # two agree on every entry in the database and the record is the record.
        link = _lineage_row(conn, entry_id)
        generation = (
            int(link["generation"])
            if link is not None and link["generation"] is not None
            else len(revisions)
        )
        revision = (
            f'<p class="card-revision">g{_esc(generation)} · '
            f"{_esc(revisions[-1])}</p>"
        )
    reason = ""
    chip = ""
    if failed:
        text = _rejection_reason(conn, row)
        reason = f'<p class="reason">{_esc(text)}</p>'
        # The chip names which kind this is. Both are rejections and both are
        # kept; only one of them had a person behind it.
        chip = _state_chip(str(row["state"])) + " "
    return template.substitute(
        entry_id=entry_id,
        href=f"e/{entry_id}/",
        strip=f"e/{entry_id}/strip.png",
        prompt=_esc(prompt),
        revision=revision,
        chip=chip,
        rules=_esc(row["rules_file"] or "unrecorded"),
        executor=_esc(row["executor"] or "unrecorded"),
        # The sort control reads this attribute, so the order JavaScript puts
        # the cards in is the order the generator already put them in.
        published=_esc(row["published_utc"] or ""),
        # ...and the search box reads this one, which is the only place the
        # brief reaches a grid page.
        search=_search_text(entry_id, row),
        submitted_by=_esc(row["submitted_by"] or "unknown"),
        # Two tracks and a chip in place of two lines of numbers: where the
        # entry stands in each population's pool, and whether the two agree.
        standing=_standing_bars(scores, entry_id),
        agreement=_agreement(scores, entry_id),
        reason=reason,
    )


#: The prompt composer, in the space the filter disclosure gave up (plan §5.1).
#:
#: Every state it can be in is in the HTML and hidden; ``gallery.js`` reveals
#: one of them once ``/me`` has answered. Nothing here is a credential and
#: nothing here is a number the page could be wrong about: the budget's first
#: figure is filled in from ``/me`` and the second is the cap the Worker
#: enforces (decision 7), written out because the sentence needs a denominator.
COMPOSER = """<section class="compose" data-compose hidden>
    <div class="compose-head">
      <h2>Submit a prompt</h2>
      <p class="quota" data-compose-quota hidden><b data-left>—</b> of 3 left today</p>
    </div>
    <p class="signed-out-line" data-compose-out>
      <a class="login" data-login href="index.html">Sign in with GitHub</a> to submit a prompt.
      Only your GitHub username is shared and published.
    </p>
    <div data-compose-in hidden>
      <label class="rule" for="compose-text">One sentence describing a sketch. No code.</label>
      <textarea id="compose-text" rows="2" data-compose-text placeholder="a tide of small \
triangles that drifts toward whichever corner the cursor last rested in"></textarea>
      <p class="rule bad" data-compose-refusal hidden></p>
      <p class="actions">
        <button type="button" class="primary" data-compose-send>Queue it</button>
        <span class="note">Held for review before it runs</span>
      </p>
    </div>
    <div class="receipt" data-compose-receipt hidden>
      <p><strong>Queued for review.</strong></p>
      <p>After review, check back later to see if your sketch was successfully created.</p>
    </div>
  </section>"""


def _composer(config: Config, page: str) -> str:
    """The composer on the gallery index, and the empty string everywhere else.

    The rejections page and the line pages take no submissions: a prompt is
    asked for once, from the one page that is about the gallery as a whole.
    With no write path in ``config.json`` there is no service to submit to, so
    the block is not written at all — the same rule the like button follows.
    """
    if page != "index.html" or not config.write_path:
        return ""
    return COMPOSER


def _filters(rows: list[sqlite3.Row], page: str) -> str:
    """The filter chips, which no page renders any more (plan §5.3).

    The bar was the only real estate the composer wanted and nobody used it, so
    ``grid.html`` dropped the ``<details class="filters">`` block. This stays,
    and so does the ``filters=`` argument below, because putting the bar back is
    then four lines of template and nothing else. ``?rules=`` and ``?executor=``
    keep filtering the grid either way: that is ``applyVisibility()``, which
    reads the URL and not these links.
    """
    rules = sorted({str(r["rules_file"]) for r in rows if r["rules_file"]})
    executors = sorted({str(r["executor"]) for r in rows if r["executor"]})
    links = [f'<a class="filter" href="{page}">all</a>']
    for value in rules:
        links.append(
            f'<a class="filter" href="{page}?rules={_esc(value)}">rules: {_esc(value)}</a>'
        )
    for value in executors:
        links.append(
            f'<a class="filter" href="{page}?executor={_esc(value)}">'
            f"executor: {_esc(value)}</a>"
        )
    return "\n      ".join(links)


def _newest_first(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """The grid's own order: the most recently published entry first.

    Only the two grid pages are reversed, and only here. ``_entries`` stays
    ascending because the forest, the line pages, compare and the balanced
    pairs all read an entry's ancestors before the entry itself, and because a
    second render of an unchanged database must still be byte-identical: this
    is a total order (the stamp, then the id), so it is.
    """
    return sorted(
        rows,
        key=lambda row: (str(row["published_utc"] or ""), int(row["id"])),
        reverse=True,
    )


def _grid_page(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    heading: str,
    intro: str,
    page: str,
    failed: bool,
    composer: str = "",
) -> str:
    card = _template("card.html")
    scores = _all_scores(conn)
    rows = _newest_first(rows)
    if rows:
        cards = "\n      ".join(_card(conn, row, failed, card, scores) for row in rows)
    else:
        cards = '<p class="none">Nothing here yet.</p>'
    return _template("grid.html").substitute(
        root="./",
        page_title=_esc(heading),
        heading=_esc(heading),
        heading_html=f"<h1>{_esc(heading)}</h1>" if heading else "",
        intro_html=f'<p class="intro">{_esc(intro)}</p>' if intro else "",
        # With no JavaScript the search box is a form that submits to the page
        # it is already on, which reloads it showing everything: harmless.
        page=_esc(page),
        # Unused by the template since §5.3 took the bar out, and passed all
        # the same so that putting it back is a change to one file.
        filters=_filters(rows, page),
        composer=composer,
        cards=cards,
    )


def _compare_page(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> str:
    """The compare shell over every *public* entry, kept rejections included.

    ``rows`` is the public set — published entries plus the kept rejections a
    person has published (:func:`_public_rows`) — which is exactly the set with
    a page under ``e/``. Every entry page links here with ``?a=<itself>``, so
    while this page knew only the published rows a rejection's link arrived at a
    page that had never heard of that id and quietly showed some other pair
    instead. ``state`` travels with each entry because the browser needs it
    twice: to pick a *published* partner for a rejection, and to name the
    rejection in the reveal — never before it (spec §5). The balanced offer
    stays published-only; see :func:`_offered_pairs`.
    """
    entries = [
        {
            "id": int(row["id"]),
            "prompt": " ".join(str(row["prompt"] or "").split()),
            "brief": row["brief"] or "",
            "seed": row["seed"],
            "state": str(row["state"]),
            "strip": f"e/{int(row['id'])}/strip.png",
            "href": f"e/{int(row['id'])}/",
        }
        for row in rows
    ]
    return _template("compare.html").substitute(
        root="./",
        page_title="Compare two sketches",
        entries_json=_json_block(entries),
        pairs_json=_json_block(_offered_pairs(conn)),
        agents_json=_json_block(pairs_mod.agent_verdicts(conn)),
    )


def _offered_pairs(conn: sqlite3.Connection) -> list[dict[str, int]]:
    """The balanced pairs the static compare page may offer, from pick_pair.

    A published gallery has no server to ask "which pair next?", so the
    generator asks for it here — once per seed, each seed a fresh
    :class:`random.Random`, so the list is deterministic and a second render of
    an unchanged database writes the same bytes. The browser picks one of these
    rather than the first two entries in the grid, which is how the balance
    rule (fewest judgments so far, control against treatment) reaches a static
    page at all.

    Published entries only, and deliberately: this is the spec §9
    control-against-treatment measurement, and a rejection is not on either arm
    of it. A kept rejection is compared only when a person asks for it by
    following its own compare link.
    """
    return pairs_mod.offer(conn)


# ---------------------------------------------------------------------------
# The kiosk (spec docs/plans/kiosk.md)
#
# One page that plays the published sketches one after another on a projector,
# and one manifest behind it. The kiosk needs the prompt, brief, statement,
# judgment and provenance of every published entry before it can order them,
# and meta.json per entry is one request per entry — 222 of them before the
# first sketch appears. So the generator serialises the values it has already
# computed for meta.json and the cards a second time, into one file. Nothing
# here is new logic; that is the point, because two files disagreeing about the
# same entry is the failure this shape rules out.
# ---------------------------------------------------------------------------

#: What a manifest row takes straight from :func:`_meta`, by the same names.
KIOSK_META_KEYS = (
    "prompt",
    "brief",
    "statement",
    "submitted_by",
    "planner",
    "executor",
    "rules_file",
    "attempts",
    "seed",
    "created_utc",
    "published_utc",
    "prompt_tokens",
    "completion_tokens",
    "wall_s",
    "licence",
)

#: What it takes from meta.json's ``lineage`` block, flattened into the row:
#: the kiosk's caption reads these four beside the prompt, and a nested object
#: for four values would only be meta.json's shape worn for no reason.
KIOSK_LINEAGE_KEYS = (
    "generation",
    "parent_entry_id",
    "critique_by",
    "root_entry_id",
)

#: The three of :func:`_standing`'s five the kiosk prints. ``rank`` and ``pool``
#: are the card's business: they place a mark on a track, and the kiosk has no
#: track.
KIOSK_STANDING_KEYS = ("score", "n", "pct")


def _kiosk_judgment(scores: dict, entry_id: int) -> dict[str, dict[str, dict]]:
    """``{population: {question: {score, n, pct}}}``, missing where unjudged.

    A population that has not judged this entry on this question is *absent* —
    not ``null``, not zero. A score nobody voted on is not a low score, and an
    ordering built over a zero would sink every unjudged entry to the bottom of
    'most reviewed' as though the pool had spoken. The browser prints *no pairs
    yet* for a missing key, as the entry page does.
    """
    out: dict[str, dict[str, dict]] = {}
    for population, tables in scores.items():
        by_question: dict[str, dict] = {}
        for question, table in tables.items():
            stand = _standing(table, entry_id)
            if stand is None:
                continue
            by_question[question] = {key: stand[key] for key in KIOSK_STANDING_KEYS}
        if by_question:
            out[population] = by_question
    return out


def _manifest_base(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    config: Config,
    parent: dict[int, int | None],
    children: dict[int, list[int]],
) -> dict[str, Any]:
    """Everything both manifests read off one entry, computed once.

    ``kiosk.json`` and ``swipe.json`` are two serialisations of the same row,
    written in the same pass (swipe.md §2). :func:`_meta` walks the attempt
    rows, reads the statement off disk and fits the lineage; :func:`_canvas_size`
    and :func:`_needs_mic` each read the sketch source. Doing that twice per
    entry would double the cost of a 910-entry render for two files that must
    agree by construction, so the shared half is built here and handed to both.
    """
    attempts = _attempt_rows(conn, int(row["job_id"]))
    return {
        "entry_id": int(row["id"]),
        "meta": _meta(conn, row, attempts, config, parent, children),
        "canvas": _canvas_size(row, attempts),
        "mic": _needs_mic(_source_dir(row, attempts)),
    }


def _kiosk_entry(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    config: Config,
    parent: dict[int, int | None],
    children: dict[int, list[int]],
    scores: dict,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One entry as the kiosk reads it: meta.json's values, plus where to run."""
    if base is None:
        base = _manifest_base(conn, row, config, parent, children)
    entry_id = base["entry_id"]
    meta = base["meta"]
    entry: dict[str, Any] = {"id": entry_id}
    entry.update({key: meta[key] for key in KIOSK_META_KEYS})
    entry.update({key: meta["lineage"][key] for key in KIOSK_LINEAGE_KEYS})
    # Relative to the gallery root, as the compare shell's rows are: the kiosk
    # page sits beside index.html and resolves them against the same base.
    entry["sketch"] = f"e/{entry_id}/sketch/"
    entry["source"] = f"e/{entry_id}/sketch/sketch.js"
    entry["href"] = f"e/{entry_id}/"
    # The **kiosk** code, the one whose payload carries ?kiosk: this manifest
    # is read by one page and that page is the projection (qr.md §4).
    entry["qr"] = f"e/{entry_id}/qr-kiosk.svg"
    # And the clean URL, without the param — the same string meta.json already
    # publishes as source.entry. It is what the kiosk *prints* under the code
    # (§5.3), for somebody typing it, and typing is not scanning.
    entry["url"] = config.entry_url(entry_id)
    if base["canvas"] is not None:
        entry["canvas"] = base["canvas"]
    entry["judgment"] = _kiosk_judgment(scores, entry_id)
    return entry


# ---------------------------------------------------------------------------
# Swipe mode (spec docs/plans/swipe.md)
#
# The phone's manifest, and the shell that reads it. swipe.json is the kiosk's
# manifest with the long prose taken out: kiosk.json is 2.8 MB because it
# carries every brief and statement in the gallery, and the swipe page shows
# one of each at a time and fetches e/<id>/meta.json when the words sheet
# opens. Same rows, same fit, same pass, so the two files can never disagree
# about an entry (spec §1.2).
# ---------------------------------------------------------------------------

#: What a swipe row takes straight from :func:`_meta`. The caption prints all
#: seven; ``brief``, ``statement``, ``seed``, ``created_utc``, the two token
#: counts, ``wall_s`` and ``licence`` are the words sheet's, and the words
#: sheet has meta.json.
SWIPE_META_KEYS = (
    "prompt",
    "submitted_by",
    "planner",
    "executor",
    "rules_file",
    "attempts",
    "published_utc",
)

#: Three of the kiosk's four lineage keys. ``root_entry_id`` is the kiosk's
#: 'the root of this line' overlay; the swipe caption prints the generation and
#: the words sheet prints the parent and its critic, and neither walks to a
#: root.
SWIPE_LINEAGE_KEYS = ("generation", "parent_entry_id", "critique_by")

#: ``responds(click)`` → ``click``. The gate's assertion vocabulary is the only
#: place the generator can learn that a sketch has something to give a finger,
#: and the caption says so — *responds to touch · hold to try* — because the
#: shield means nobody finds out by accident (spec §4.4).
_RESPONDS_RE = re.compile(r"responds\((\w+)\)")


def _responds(assertions: Iterable[str]) -> list[str]:
    """The ``<what>`` of every ``responds(<what>)`` assertion, in order."""
    out: list[str] = []
    for assertion in assertions:
        match = _RESPONDS_RE.search(str(assertion))
        if match is not None:
            out.append(match.group(1))
    return out


def _swipe_entry(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    config: Config,
    parent: dict[int, int | None],
    children: dict[int, list[int]],
    scores: dict,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One entry as the phone reads it: the caption's values, and where to run.

    Every value here is one :func:`_kiosk_entry` already computes, minus the
    prose and plus three the kiosk has no use for: what the sketch responds to,
    whether it listens, and the path of its meta.json.
    """
    if base is None:
        base = _manifest_base(conn, row, config, parent, children)
    entry_id = base["entry_id"]
    meta = base["meta"]
    entry: dict[str, Any] = {"id": entry_id}
    entry.update({key: meta[key] for key in SWIPE_META_KEYS})
    entry.update({key: meta["lineage"][key] for key in SWIPE_LINEAGE_KEYS})
    # Absent when there are none, as ``canvas`` is: a sketch that has nothing
    # to give a finger should not be told to hold, and an empty list in the
    # row is one more thing for the script to remember to test for.
    # And only what the gate confirmed: ``assertions`` is what the planner
    # asked for, and an off-plan entry is one the gate published anyway with
    # some of them missed. A caption that says *hold to try* on a sketch the
    # gate proved does not respond is the one case the fact exists to prevent.
    missed = set(_offplan(row))
    responds = _responds(a for a in meta["assertions"] if a not in missed)
    if responds:
        entry["responds"] = responds
    # A listening sketch is framed like any other — the frame is opaque and the
    # mic is refused it, so it runs deaf, and the caption says to open the
    # entry page instead. Skipping it would make the feed lie about the size of
    # the gallery (spec §2).
    if base["mic"]:
        entry["mic"] = True
    if base["canvas"] is not None:
        entry["canvas"] = base["canvas"]
    entry["judgment"] = _kiosk_judgment(scores, entry_id)
    # Relative to the gallery root, as the kiosk's are. ``meta`` is the file
    # the words sheet and the judge sheet fetch: a path from the manifest,
    # never assembled in the script, as ``qr`` is for the kiosk.
    entry["sketch"] = f"e/{entry_id}/sketch/"
    entry["meta"] = f"e/{entry_id}/meta.json"
    entry["href"] = f"e/{entry_id}/"
    entry["url"] = config.entry_url(entry_id)
    return entry


def _manifests(
    conn: sqlite3.Connection,
    config: Config,
    parent: dict[int, int | None],
    children: dict[int, list[int]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(kiosk.json, swipe.json)`` over one pass of the published rows.

    Published entries only, the same rows the grid page gets. A projector in a
    lobby shows the gallery, and the gallery is the published set — a kept
    rejection has a page of its own and a reason printed on it, neither of which
    survives being played for sixty seconds with no one at the keyboard; the
    phone's feed is the same set for the same reason.

    The four Bradley–Terry tables are fitted once here and handed to every row:
    the fit is over the whole pool, so doing it per entry would be slower and no
    different. Both manifests come out of one :func:`_manifest_base` per entry,
    so the second file costs a dict and not a second read of the database and
    the disk. No clock is read and the order is total, so the same database
    gives the same bytes.
    """
    scores = _all_scores(conn)
    rows = sorted(_entries(conn, "published"), key=lambda row: int(row["id"]))
    kiosk: list[dict[str, Any]] = []
    swipe: list[dict[str, Any]] = []
    for row in rows:
        base = _manifest_base(conn, row, config, parent, children)
        kiosk.append(_kiosk_entry(conn, row, config, parent, children, scores, base))
        swipe.append(_swipe_entry(conn, row, config, parent, children, scores, base))
    return {"entries": kiosk}, {"entries": swipe}


def _kiosk_manifest(
    conn: sqlite3.Connection,
    config: Config,
    parent: dict[int, int | None],
    children: dict[int, list[int]],
) -> dict[str, Any]:
    """``kiosk.json``: every published entry, in ascending id order."""
    return _manifests(conn, config, parent, children)[0]


def _swipe_manifest(
    conn: sqlite3.Connection,
    config: Config,
    parent: dict[int, int | None],
    children: dict[int, list[int]],
) -> dict[str, Any]:
    """``swipe.json``: the same entries, in the same order, without the prose."""
    return _manifests(conn, config, parent, children)[1]


def _kiosk_page(config: Config) -> str:
    """The kiosk shell. No entry data: ``kiosk.js`` fetches ``kiosk.json``.

    ``config`` is taken and unused, as the other page builders take it, so that
    a future footer naming the write path is a change to this function alone.
    """
    return _template("kiosk.html").substitute(root="./", page_title="Sketchgen Kiosk")


def _swipe_page(config: Config) -> str:
    """The swipe shell. No entry data: ``swipe.js`` fetches ``swipe.json``.

    ``config`` is taken and unused, as :func:`_kiosk_page` takes it and for the
    same reason.
    """
    return _template("swipe.html").substitute(root="./", page_title="Sketchgen Swipe")


def _line_node(
    item: dict[str, Any],
    by_id: dict[int, sqlite3.Row],
    root: int | None = None,
) -> str:
    """One generation's card: the critique that asked for it, then the entry.

    An entry that is not public — held at the publication gate, or rejected —
    gets the card and nothing of its own: the generation, and that it is
    waiting. The critique above it came from the public parent and is the reason
    the child exists, so it stays (packet 5.3, spec §8.1).

    Only the root card carries the prompt. Every generation's prompt is its
    parent's with one critique appended, so a line of eleven cards used to print
    the root sentence eleven times and the critique twice — once as this card's
    ``Revise:`` line and again at the tail of the next card's prompt.
    """
    entry_id = int(item["entry_id"])
    depth = min(int(item["generation"]), 6)
    public = entry_id in by_id
    head = (
        f'<a href="../e/{entry_id}/">entry {entry_id}</a> '
        + _state_chip(item["state"])
        if public
        else f"entry {entry_id} <span class=\"chip\">not published</span>"
    )
    critique = ""
    if item["critique"]:
        critique = (
            f'<p class="critique">Revise: {_esc(item["critique"])} '
            f'<span class="dim">— critique by '
            f'{_esc(item["critique_by"] or "unknown")}</span></p>'
        )
    is_root = root is not None and entry_id == int(root)
    if is_root:
        body = f'<p class="node-prompt">{_esc(_title_parts(item["prompt"])[0])}</p>'
    elif public:
        body = ""
    else:
        body = (
            '<p class="node-prompt">This generation is not published, so the '
            "gallery shows the critique and nothing else.</p>"
        )
    return (
        f'<div class="node depth-{depth}">'
        f"{critique}"
        f'<p class="node-head">{head} '
        f'<span class="dim">generation {_esc(item["generation"])}</span></p>'
        f"{body}</div>"
    )


#: The card that closes a line standing at DECIDE[lineage-depth].
_WAITS_CARD = (
    '<div class="node depth-{depth} waits">'
    '<p class="node-head">generation {generation} — waits for a person</p>'
    '<p class="node-prompt">DECIDE[lineage-depth] is three: a self-prompted line '
    "runs three generations on its own and then stops until somebody looks at it. "
    "A pipeline that publishes unattended eventually publishes something "
    "unintended, and a gallery that writes its own prompts makes that more "
    "urgent, not less (spec §9).</p></div>"
)


def _line_page(
    conn: sqlite3.Connection,
    root: int,
    children: dict[int, list[int]],
    by_id: dict[int, sqlite3.Row],
) -> str:
    generations = lineage.line(conn, root)
    cards = [_line_node(item, by_id, root) for item in generations]
    deepest = max((int(item["generation"]) for item in generations), default=0)
    if any(item["at_limit"] for item in generations):
        cards.append(
            _WAITS_CARD.format(depth=min(deepest, 6), generation=deepest)
        )
    count = len(generations) or 1 + len(_descendants(children, root))
    return _template("line.html").substitute(
        root="../",
        page_title=f"Line from entry {root}",
        heading=f"Line from entry {root}",
        intro=_esc(
            f"{count} entries in this line. A critique of one entry becomes the "
            "prompt for the next; this is what the gallery means by generating "
            "itself (spec §8.1)."
        ),
        tree="\n".join(cards),
    )


def render_index(
    conn: sqlite3.Connection,
    dest_dir: str | Path,
    config: Config | None = None,
) -> list[Path]:
    """Write the grid, the failures, compare, the kiosk, the swipe page, the
    line pages, assets and config.

    No clock is read: the same database gives the same bytes, which is what
    lets ``publish_index`` tell a re-render that changed nothing from one that
    did. ``lineage.json``'s ``generated_utc`` is the newest stamp the database
    holds (:func:`_ledger_stamp`), not the time of the render.
    """
    dest = Path(dest_dir)
    config = _resolve_config(dest, config)
    published = _entries(conn, "published")
    # Both kinds of rejection on one page, as §5.2 asks: the gate's and the
    # operator's. _grid_page puts them in newest-published order.
    failed = _entries(conn, "failed-kept") + _entries(conn, "rejected")
    parent, children = _forest(conn)
    by_id = {int(row["id"]): row for row in _public_rows(conn)}

    written = _Written(dest)
    try:
        written.mkdir(dest)
        written.write_text(dest / "config.json", config.to_json())
        for asset in sorted(ASSET_DIR.iterdir()):
            if asset.is_file():
                written.copy(asset, dest / "assets" / asset.name)
        written.write_text(
            dest / "index.html",
            _grid_page(
                conn,
                published,
                heading="",   # the header bar names the site; the grid needs no title
                intro="",
                page="index.html",
                failed=False,
                composer=_composer(config, "index.html"),
            ),
        )
        written.write_text(
            dest / "rejections.html",
            _grid_page(
                conn,
                failed,
                heading="Rejections",
                intro=(
                    "Entries that never ran correctly, and entries a person "
                    "rejected, kept on purpose: a gallery that only shows "
                    "successes is not a record of anything."
                ),
                page="rejections.html",
                failed=True,
            ),
        )
        written.write_text(
            dest / "pairs.json",
            json.dumps(
                {
                    "pairs": _offered_pairs(conn),
                    "agents": pairs_mod.agent_verdicts(conn),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        # One pass over the published rows for both files: they are two
        # serialisations of the same entries and must never disagree about one
        # (swipe.md §1.2).
        kiosk_manifest, swipe_manifest = _manifests(conn, config, parent, children)
        written.write_text(
            dest / "kiosk.json",
            json.dumps(kiosk_manifest, indent=2, sort_keys=True) + "\n",
        )
        written.write_text(
            dest / "swipe.json",
            json.dumps(swipe_manifest, indent=2, sort_keys=True) + "\n",
        )
        written.write_text(
            dest / "lineage.json",
            _lineage_bytes(_lineage_index(conn)).decode("utf-8"),
        )
        # Every public entry, not just the published ones: a kept rejection's
        # entry page links here with ?a=<itself> and the page has to know that
        # id to honour it.
        written.write_text(
            dest / "compare.html", _compare_page(conn, _public_rows(conn))
        )
        written.write_text(dest / "kiosk.html", _kiosk_page(config))
        written.write_text(dest / "swipe.html", _swipe_page(config))
        roots = sorted(
            {
                _root_of(parent, entry_id)
                for entry_id in by_id
            }
        )
        for root in roots:
            if not _descendants(children, root):
                continue
            written.write_text(
                dest / "lines" / f"{root}.html",
                _line_page(conn, root, children, by_id),
            )
        # Two QR codes per public entry (spec qr.md §1.4). They depend on the
        # entry's id and on config.gallery_url and on nothing else about the
        # entry, so they belong to the index rather than to the entry page:
        # update.sh runs render-index on every deploy and never re-renders 222
        # entry pages, which is what makes this deployable without a
        # render-all. Every public entry, not just the published ones — a kept
        # rejection has a page too, and that page links its code.
        for entry_id in sorted(by_id):
            url = config.entry_url(entry_id)
            written.write_text(dest / "e" / str(entry_id) / "qr.svg", qr.svg(url))
            # The kiosk's code says it came off a projection; the entry page's
            # does not (§1.8). Six bytes, which is what the version-4 budget
            # affords, and the printed URL under both is the clean one.
            written.write_text(
                dest / "e" / str(entry_id) / "qr-kiosk.svg", qr.svg(url + "?kiosk")
            )
    except Exception:
        written.undo()
        raise
    guard(dest, written)
    return list(written.files)


def render_all(
    conn: sqlite3.Connection,
    dest_dir: str | Path,
    config: Config | None = None,
) -> list[Path]:
    """Every public entry, then the pages that index them."""
    dest = Path(dest_dir)
    config = _resolve_config(dest, config)
    written = render_index(conn, dest, config)
    for row in _public_rows(conn):
        written.append(render_entry(conn, int(row["id"]), dest, config))
    return written
