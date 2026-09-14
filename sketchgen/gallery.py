"""gallery.py — the static gallery, generated from the database (packet 3.1).

One entry row plus its attempt directory in, a directory of plain files out:

    <gallery>/e/<id>/index.html          the ENTRY page (not the sketch)
    <gallery>/e/<id>/sketch/index.html   the sketch's own page, verbatim
    <gallery>/e/<id>/sketch/sketch.js    the file the entry's frame loads
    <gallery>/e/<id>/strip.png           four frames, from the gate
    <gallery>/e/<id>/gate.png            the gate's single frame
    <gallery>/e/<id>/statement.md        the executor's own words, verbatim
    <gallery>/e/<id>/meta.json           every spec §7 field
    <gallery>/index.html                 the grid (published entries)
    <gallery>/rejections.html            the gate's rejections, kept
    <gallery>/compare.html               the paired-judgment shell
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
rendered from the ``judgments`` table alone; Bradley–Terry is packet 5.1, so
until it exists a population with no pairs says "no pairs yet" and one with
pairs says how many answers are recorded, never a score. Views and likes live
behind the write path (packet 3.3) and are rendered by ``gallery.js`` at read
time; the generator writes an em dash and no number.

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

#: The states that get a directory under ``e/``. ``held`` and ``rejected`` are
#: not public: publication holds for a person (spec §9, DECIDE[publication-gate]).
PUBLIC_STATES = ("published", "failed-kept")

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
    """Every path one render created, so a refusal can undo all of it."""

    def __init__(self, dest: Path) -> None:
        self.dest = dest
        self.files: list[Path] = []
        self.dirs: list[Path] = []

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
        path.write_text(text, encoding="utf-8")
        self.files.append(path)
        return path

    def copy(self, src: Path, dst: Path) -> Path:
        self.mkdir(dst.parent)
        shutil.copyfile(src, dst)
        self.files.append(dst)
        return dst

    def undo(self) -> None:
        for path in self.files:
            try:
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
    """

    write_path: str = ""
    gallery_url: str = DEFAULT_GALLERY_URL
    repository: str = DEFAULT_REPOSITORY

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
        )

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "write_path": self.write_path,
                    "gallery_url": self.gallery_url,
                    "repository": self.repository,
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
    return row


def _entries(conn: sqlite3.Connection, state: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM entries WHERE state = ? ORDER BY created_utc, id", (state,)
        )
    )


def _job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _attempt_rows(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM attempts WHERE job_id = ? ORDER BY n", (job_id,))
    )


def _judgment_counts(conn: sqlite3.Connection, entry_id: int) -> dict[str, int]:
    counts = {"human": 0, "agent": 0}
    for row in conn.execute(
        "SELECT judge_kind, COUNT(*) AS c FROM judgments "
        "WHERE entry_a = ? OR entry_b = ? GROUP BY judge_kind",
        (entry_id, entry_id),
    ):
        counts[row["judge_kind"]] = int(row["c"])
    return counts


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


def _forest(conn: sqlite3.Connection) -> tuple[dict[int, int | None], dict[int, list[int]]]:
    """(parent by id, children by id) over the public entries, id order."""
    ids = [int(row["id"]) for row in _public_rows(conn)]
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


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _template(name: str) -> Template:
    return Template((TEMPLATE_DIR / name).read_text(encoding="utf-8"))


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


def _score_slot(count: int) -> tuple[str, str]:
    """(value, note) for one judge population. No number is ever invented."""
    if count <= 0:
        return (
            "no pairs yet",
            "A Bradley–Terry score needs pairs; none has been judged.",
        )
    return (
        "pending",
        f"{count} answer{'s' if count != 1 else ''} recorded; the score arrives "
        "with the Bradley–Terry fit (packet 5.1).",
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


def _byline(row: sqlite3.Row, meta: dict[str, Any]) -> str:
    by = _esc(row["submitted_by"] or "an operator")
    planner = _esc(row["planner"] or "no planner on record")
    executor = _esc(row["executor"] or "no executor on record")
    attempts = meta["attempts"] or 1
    if row["state"] == "published":
        tail = f"passed the gate on attempt {attempts}"
    else:
        tail = (
            f"did not pass the gate in {attempts} attempt"
            f"{'s' if attempts != 1 else ''}"
        )
    return (
        f"Prompt by {by}. Planned by {planner}, written by {executor}, {tail}."
    )


def _frame(has_sketch: bool, title: str) -> str:
    if not has_sketch:
        return (
            '<p class="none">The sketch source is not in this checkout, so there '
            "is nothing to run here.</p>"
        )
    return (
        f'<iframe class="sketch" src="sketch/" title="{_esc(title)}" '
        'loading="lazy" sandbox="allow-scripts"></iframe>'
    )


def _lineage_panel(meta: dict[str, Any], line_page: bool) -> str:
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
    parent, children = _forest(conn)
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
            written.copy(index_html, out / "sketch" / "index.html")
            has_sketch = True
    for which, name in (("strip", "strip.png"), ("png", "gate.png")):
        artefact = _artefact(row, attempts, which)
        if artefact is not None:
            written.copy(artefact, out / name)

    statement = meta["statement"] or ""
    written.write_text(
        out / "statement.md",
        (statement + "\n") if statement else "(the executor wrote no statement)\n",
    )
    written.write_text(out / "meta.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")

    counts = _judgment_counts(conn, entry_id)
    human_value, human_note = _score_slot(counts["human"])
    agent_value, agent_note = _score_slot(counts["agent"])
    root = meta["lineage"]["root_entry_id"]
    has_line = bool(_descendants(children, root))
    title = " ".join(str(row["prompt"] or f"entry {entry_id}").split())
    failed_note = ""
    if row["state"] == "failed-kept":
        job = _job(conn, int(row["job_id"]))
        reason = (job["last_error"] if job is not None else None) or "reason not recorded"
        failed_note = (
            '<p class="chip failed">REJECTED BY THE GATE — kept, because a gallery that only '
            f"shows successes is not a record of anything: {_esc(reason)}</p>"
        )

    page = _template("entry.html").substitute(
        root="../../",
        page_title=_esc(title[:80]),
        entry_id=entry_id,
        title=_esc(title),
        byline=_byline(row, meta),
        failed_note=failed_note,
        frame=_frame(has_sketch, title),
        seed=_dash(meta["seed"]),
        state_chip=f'<span class="chip {_esc(row["state"])}">{_esc(row["state"])}</span>',
        brief=_paragraphs(str(row["brief"] or ""), "No brief was recorded for this job."),
        statement_model=_esc(row["executor"] or "an unrecorded model"),
        statement=_paragraphs(statement, "The executor wrote no statement."),
        human_value=_esc(human_value),
        human_note=_esc(human_note),
        agent_value=_esc(agent_value),
        agent_note=_esc(agent_note),
        compare_href=f"../../compare.html?a={entry_id}",
        source_rows=_source_rows(meta),
        provenance_rows=_provenance_rows(meta),
        lineage=_lineage_panel(meta, has_line),
    )
    written.write_text(out / "index.html", page)
    return out


# ---------------------------------------------------------------------------
# The grid, the failures, compare, the lines
# ---------------------------------------------------------------------------


def _card(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    failed: bool,
    template: Template,
) -> str:
    entry_id = int(row["id"])
    counts = _judgment_counts(conn, entry_id)
    human_value, _ = _score_slot(counts["human"])
    agent_value, _ = _score_slot(counts["agent"])
    prompt = " ".join(str(row["prompt"] or f"entry {entry_id}").split())
    reason = ""
    chip = ""
    if failed:
        job = _job(conn, int(row["job_id"]))
        text = (job["last_error"] if job is not None else None) or "reason not recorded"
        reason = f'<p class="reason">{_esc(text)}</p>'
        chip = '<span class="chip failed">REJECTED</span> '
    return template.substitute(
        entry_id=entry_id,
        href=f"e/{entry_id}/",
        strip=f"e/{entry_id}/strip.png",
        prompt=_esc(prompt),
        chip=chip,
        rules=_esc(row["rules_file"] or "unrecorded"),
        executor=_esc(row["executor"] or "unrecorded"),
        submitted_by=_esc(row["submitted_by"] or "unknown"),
        human=_esc(human_value),
        agent=_esc(agent_value),
        reason=reason,
    )


def _filters(rows: list[sqlite3.Row], page: str) -> str:
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


def _grid_page(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    heading: str,
    intro: str,
    page: str,
    failed: bool,
) -> str:
    card = _template("card.html")
    if rows:
        cards = "\n      ".join(_card(conn, row, failed, card) for row in rows)
    else:
        cards = '<p class="none">Nothing here yet.</p>'
    return _template("grid.html").substitute(
        root="./",
        page_title=_esc(heading),
        heading=_esc(heading),
        intro=_esc(intro),
        filters=_filters(rows, page),
        cards=cards,
    )


def _compare_page(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> str:
    entries = [
        {
            "id": int(row["id"]),
            "prompt": " ".join(str(row["prompt"] or "").split()),
            "brief": row["brief"] or "",
            "seed": row["seed"],
            "strip": f"e/{int(row['id'])}/strip.png",
            "href": f"e/{int(row['id'])}/",
        }
        for row in rows
    ]
    return _template("compare.html").substitute(
        root="./",
        page_title="Compare two sketches",
        entries_json=_json_block(entries),
    )


def _line_node(
    conn: sqlite3.Connection,
    entry_id: int,
    children: dict[int, list[int]],
    by_id: dict[int, sqlite3.Row],
    depth: int,
) -> str:
    row = by_id.get(entry_id)
    prompt = " ".join(str(row["prompt"] or "").split()) if row is not None else ""
    state = row["state"] if row is not None else "unknown"
    inner = "\n".join(
        _line_node(conn, kid, children, by_id, depth + 1)
        for kid in children.get(entry_id, [])
    )
    return (
        f'<div class="node depth-{min(depth, 6)}">'
        f'<p class="node-head"><a href="../e/{entry_id}/">entry {entry_id}</a> '
        f'<span class="chip {_esc(state)}">{_esc(state)}</span></p>'
        f'<p class="node-prompt">{_esc(prompt)}</p>'
        f"{inner}</div>"
    )


def _line_page(
    conn: sqlite3.Connection,
    root: int,
    children: dict[int, list[int]],
    by_id: dict[int, sqlite3.Row],
) -> str:
    count = 1 + len(_descendants(children, root))
    return _template("line.html").substitute(
        root="../",
        page_title=f"Line from entry {root}",
        heading=f"Line from entry {root}",
        intro=_esc(
            f"{count} entries in this line. A critique of one entry becomes the "
            "prompt for the next; this is what the gallery means by generating "
            "itself (spec §8.1)."
        ),
        tree=_line_node(conn, root, children, by_id, 0),
    )


def render_index(
    conn: sqlite3.Connection,
    dest_dir: str | Path,
    config: Config | None = None,
) -> list[Path]:
    """Write the grid, the failures, compare, the line pages, assets and config."""
    dest = Path(dest_dir)
    config = _resolve_config(dest, config)
    published = _entries(conn, "published")
    failed = _entries(conn, "failed-kept")
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
                heading="sketchgen",
                intro=(
                    "A gallery that generates itself: every entry is one prompt, "
                    "one brief, one gate and the model's own account of what it "
                    "built. Rejections are kept too."
                ),
                page="index.html",
                failed=False,
            ),
        )
        written.write_text(
            dest / "rejections.html",
            _grid_page(
                conn,
                failed,
                heading="Rejections",
                intro=(
                    "Entries the gate rejected after every attempt, kept on purpose: a "
                    "gallery that only shows successes is not a record of anything."
                ),
                page="rejections.html",
                failed=True,
            ),
        )
        written.write_text(dest / "compare.html", _compare_page(conn, published))
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
