"""pairs.py — paired comparison: picking pairs, recording answers, scoring them.

Packet 5.1. The research idea in spec §5 is that a like is one noisy bit and two
columns of small integers are not a dataset, so two entries are shown side by
side and the same two questions are asked of humans and of agents:

    brief   which is closer to its brief?
    look    which would you rather look at?

A Bradley–Terry fit over those answers gives every entry a score **per judge
population**, and the disagreement between the populations is the measurement
(MEASURE[agent-human-correlation]). Nothing here ever aggregates the two, and
nothing here reads ``engagement`` or ``likes``: engagement is not judgment
(spec §5), and an agent that has seen a like count is no longer answering the
question the humans answered.

Python 3.12, stdlib only. No numpy: the fit below is Hunter (2004)'s MM
algorithm written out in plain floats.

Orientation
-----------
A judgment row names ``entry_a`` and ``entry_b`` in the order the page showed
them, and ``choice`` is 'A', 'B' or 'tie' relative to *that* order. The write
path (``writepath/worker.js``) and :func:`record` both store what was shown, so
the same pair can appear as (7, 9) and as (9, 7). Every reader in this module
canonicalises to ``(min, max)`` and flips the choice with it, so orientation is
a fact about the page, never about the score.

The prior, and its limit
------------------------
Bradley–Terry with no prior diverges the moment an entry wins all of its pairs
or loses all of them: its maximum-likelihood strength is +inf or 0, and the
iteration walks off toward it. A course gallery hands out exactly that case in
week one. So every entry is given **one virtual tie against a reference entry
of fixed strength 1.0** — a Dirichlet-style +0.5 pseudo-count of wins and +0.5
of losses against an opponent that is not in the pool.

Two consequences worth writing down:

* The scale is anchored. A score is readable on its own: 1.00 means "as strong
  as the reference", above 1 means stronger, below weaker. No normalisation
  step is needed, and scores from two separate fits (humans and agents) sit on
  the same scale — which is what makes the divergence legible.
* **It shrinks toward the reference, hardest where there is least data.** An
  entry with two pairs is pulled much closer to 1.00 than an entry with fifty,
  so an early score understates how far apart the field really is, and two
  entries with very different pair counts are not directly comparable in
  magnitude. The *ordering* is what survives a small n; the *margin* is not.
  If the gallery ever needs a defensible margin, report the pair count beside
  the score (the gallery does) and wait for the pairs.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import db

__all__ = [
    "CHOICES",
    "POPULATIONS",
    "QUESTIONS",
    "REFERENCE_STRENGTH",
    "PSEUDO_COUNT",
    "agreement",
    "artefact_hash",
    "bradley_terry",
    "candidate_pairs",
    "pick_pair",
    "record",
    "scores",
]

QUESTIONS = ("brief", "look")
CHOICES = ("A", "B", "tie")
POPULATIONS = ("human", "agent")

#: The virtual opponent every entry is compared against once. See the module
#: docstring: this is the whole of the prior, and the whole of its limit.
REFERENCE_STRENGTH = 1.0
PSEUDO_COUNT = 0.5

#: The fit stops when no strength moves by more than this, relatively.
TOLERANCE = 1e-10
MAX_ITERATIONS = 10_000


# ---------------------------------------------------------------------------
# Reading judgments
# ---------------------------------------------------------------------------


def _canonical(a: int, b: int, choice: str) -> tuple[int, int, str]:
    """One pair and one choice in (low, high) order, the choice flipped with it."""
    if a <= b:
        return a, b, choice
    return b, a, {"A": "B", "B": "A", "tie": "tie"}[choice]


def _judgment_rows(
    conn: sqlite3.Connection,
    *,
    population: str | None = None,
    question: str | None = None,
) -> list[tuple[int, int, str, str, str]]:
    """(low, high, choice, judge_kind, judge_id), canonicalised, row order."""
    sql = (
        "SELECT entry_a, entry_b, choice, judge_kind, judge_id, question "
        "FROM judgments WHERE 1 = 1"
    )
    params: list[Any] = []
    if population is not None:
        sql += " AND judge_kind = ?"
        params.append(population)
    if question is not None:
        sql += " AND question = ?"
        params.append(question)
    sql += " ORDER BY id"
    out: list[tuple[int, int, str, str, str]] = []
    for row in conn.execute(sql, params):
        a, b, choice = _canonical(
            int(row["entry_a"]), int(row["entry_b"]), str(row["choice"])
        )
        if a == b or choice not in CHOICES:
            continue
        out.append((a, b, choice, str(row["judge_kind"]), str(row["judge_id"])))
    return out


# ---------------------------------------------------------------------------
# Bradley–Terry, Hunter (2004) MM
# ---------------------------------------------------------------------------


def bradley_terry(
    wins: Mapping[tuple[int, int], float],
    *,
    reference: float = REFERENCE_STRENGTH,
    pseudo: float = PSEUDO_COUNT,
    tolerance: float = TOLERANCE,
    max_iterations: int = MAX_ITERATIONS,
) -> dict[int, float]:
    """Fit strengths from ``wins[(i, j)] = how often i beat j`` (ties count 0.5).

    The model is ``P(i beats j) = p_i / (p_i + p_j)``. Hunter's minorisation–
    maximisation update is

        p_i  <-  W_i / SUM_j  n_ij / (p_i + p_j)

    with ``W_i`` the wins of i and ``n_ij`` the number of comparisons between i
    and j. Every entry additionally carries one virtual tie against an opponent
    of fixed strength ``reference``, which adds ``pseudo`` to the numerator and
    ``2 * pseudo / (p_i + reference)`` to the denominator. That term is what
    keeps an undefeated entry finite and fixes the scale, so no normalisation
    follows; see the module docstring for what it costs.

    The update never produces a non-positive strength (every numerator is at
    least ``pseudo``), so the iteration cannot leave the feasible region.
    """
    players = sorted({i for pair in wins for i in pair})
    if not players:
        return {}

    # n_ij, symmetric, built once.
    counts: dict[int, dict[int, float]] = {i: {} for i in players}
    won: dict[int, float] = {i: 0.0 for i in players}
    for (i, j), value in wins.items():
        if value <= 0:
            counts[i].setdefault(j, 0.0)
            counts[j].setdefault(i, 0.0)
            continue
        won[i] += value
        counts[i][j] = counts[i].get(j, 0.0) + value
        counts[j][i] = counts[j].get(i, 0.0) + value

    strength = {i: 1.0 for i in players}
    for _ in range(max_iterations):
        nxt: dict[int, float] = {}
        for i in players:
            denominator = 2.0 * pseudo / (strength[i] + reference)
            for j, n_ij in counts[i].items():
                if n_ij:
                    denominator += n_ij / (strength[i] + strength[j])
            nxt[i] = (won[i] + pseudo) / denominator
        moved = max(
            abs(nxt[i] - strength[i]) / max(strength[i], 1e-12) for i in players
        )
        strength = nxt
        if moved < tolerance:
            break
    return strength


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------


def scores(
    conn: sqlite3.Connection,
    *,
    population: str,
    question: str,
) -> dict[int, dict[str, float | int]]:
    """Bradley–Terry scores for one population and one question.

    Returns ``{entry_id: {"score", "n", "wins", "losses", "ties"}}``. A tie is
    half a win each way in the fit; ``wins`` and ``losses`` count only decisive
    answers, and ``ties`` counts the tied ones, so the three add to ``n``.

    An entry with no judgments in this population is **absent** from the result.
    The gallery renders its absence as "no pairs yet" rather than as a zero: a
    score that nobody voted on is not a low score, it is no score.
    """
    if population not in POPULATIONS:
        raise ValueError(f"unknown population {population!r}")
    if question not in QUESTIONS:
        raise ValueError(f"unknown question {question!r}")

    wins: dict[tuple[int, int], float] = {}
    tally: dict[int, dict[str, int]] = {}

    def slot(entry_id: int) -> dict[str, int]:
        return tally.setdefault(
            entry_id, {"n": 0, "wins": 0, "losses": 0, "ties": 0}
        )

    for low, high, choice, _kind, _judge in _judgment_rows(
        conn, population=population, question=question
    ):
        left, right = slot(low), slot(high)
        left["n"] += 1
        right["n"] += 1
        if choice == "A":
            wins[(low, high)] = wins.get((low, high), 0.0) + 1.0
            wins.setdefault((high, low), 0.0)
            left["wins"] += 1
            right["losses"] += 1
        elif choice == "B":
            wins[(high, low)] = wins.get((high, low), 0.0) + 1.0
            wins.setdefault((low, high), 0.0)
            right["wins"] += 1
            left["losses"] += 1
        else:
            wins[(low, high)] = wins.get((low, high), 0.0) + 0.5
            wins[(high, low)] = wins.get((high, low), 0.0) + 0.5
            left["ties"] += 1
            right["ties"] += 1

    fitted = bradley_terry(wins)
    return {
        entry_id: {
            "score": fitted.get(entry_id, REFERENCE_STRENGTH),
            "n": counts["n"],
            "wins": counts["wins"],
            "losses": counts["losses"],
            "ties": counts["ties"],
        }
        for entry_id, counts in sorted(tally.items())
    }


# ---------------------------------------------------------------------------
# Agreement between the two populations
# ---------------------------------------------------------------------------


def _majority(votes: list[str]) -> str | None:
    """The choice most of a population gave, or None when it did not vote.

    A plurality is enough. A dead heat between two choices is reported as
    ``'tie'``: the population did not prefer either entry, which is what a tie
    means, and inventing a winner from a coin flip would put noise into the one
    number this packet exists to produce.
    """
    if not votes:
        return None
    counts = {choice: votes.count(choice) for choice in CHOICES}
    best = max(counts.values())
    winners = [choice for choice in CHOICES if counts[choice] == best]
    return winners[0] if len(winners) == 1 else "tie"


def agreement(conn: sqlite3.Connection) -> dict[str, dict[str, float | int]]:
    """How often the two populations reach the same verdict on the same pair.

    Only pairs answered by **both** populations count; a pair only humans have
    seen says nothing about correlation. Each population's verdict on a pair is
    its majority choice (see :func:`_majority`), and the pair agrees when the
    two verdicts are equal.

    Returns ``{question: {"n", "agree", "agreement"}}`` for 'brief' and 'look',
    plus an ``'overall'`` row over both. ``agreement`` is None when ``n`` is 0 —
    no pairs, no fraction. This is the first number MEASURE[agent-human-
    correlation] asks for; with a hundred pairs behind it, it is the packet's
    whole point.
    """
    out: dict[str, dict[str, float | int]] = {}
    total_n = total_agree = 0
    for question in QUESTIONS:
        per_pair: dict[tuple[int, int], dict[str, list[str]]] = {}
        for low, high, choice, kind, _judge in _judgment_rows(
            conn, question=question
        ):
            bucket = per_pair.setdefault((low, high), {"human": [], "agent": []})
            if kind in bucket:
                bucket[kind].append(choice)
        n = agree = 0
        for votes in per_pair.values():
            human = _majority(votes["human"])
            agent = _majority(votes["agent"])
            if human is None or agent is None:
                continue
            n += 1
            if human == agent:
                agree += 1
        out[question] = {
            "n": n,
            "agree": agree,
            "agreement": (agree / n) if n else None,
        }
        total_n += n
        total_agree += agree
    out["overall"] = {
        "n": total_n,
        "agree": total_agree,
        "agreement": (total_agree / total_n) if total_n else None,
    }
    return out


# ---------------------------------------------------------------------------
# What the judge saw
# ---------------------------------------------------------------------------


def _entry_row(conn: sqlite3.Connection | None, value: Any) -> Mapping[str, Any]:
    """An entry row, from a row/mapping as given or from the database by id."""
    if isinstance(value, sqlite3.Row):
        return {key: value[key] for key in value.keys()}
    if isinstance(value, Mapping):
        return value
    if conn is None:
        raise ValueError(
            "artefact_hash needs entry rows, or entry ids and conn=<connection>"
        )
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (int(value),)).fetchone()
    if row is None:
        raise ValueError(f"no entry {value}")
    return {key: row[key] for key in row.keys()}


def artefact_hash(entry_a: Any, entry_b: Any, *, conn: sqlite3.Connection | None = None):
    """sha256 over both entries' strip.png bytes and briefs, in A-then-B order.

    A verdict is only interpretable against what the judge actually saw. Re-run
    a sketch, regenerate its strip, and the hash changes; the old verdict is
    still in the table and is now visibly about a different artefact. Both
    arguments may be entry rows (or any mapping with ``id``, ``brief`` and
    ``strip_path``), or entry ids when ``conn`` is given.

    A missing or unreadable strip hashes as the literal ``b'no-strip'`` rather
    than raising, so a judgment recorded before an artefact was copied is still
    tied to *something* honest.
    """
    digest = hashlib.sha256()
    for value in (entry_a, entry_b):
        row = _entry_row(conn, value)
        digest.update(b"entry\x00")
        digest.update(str(row.get("id", "")).encode("utf-8"))
        digest.update(b"\x00brief\x00")
        digest.update(str(row.get("brief") or "").encode("utf-8"))
        digest.update(b"\x00strip\x00")
        strip = row.get("strip_path")
        data = b"no-strip"
        if strip:
            try:
                data = Path(str(strip)).read_bytes()
            except OSError:
                data = b"no-strip"
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(data)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Picking a pair
# ---------------------------------------------------------------------------


def _published(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id, rules_file, seed FROM entries WHERE state = 'published' "
            "ORDER BY id"
        )
    )


def _pair_load(conn: sqlite3.Connection) -> dict[tuple[int, int], int]:
    """How many judgments each canonical pair already carries, any judge."""
    load: dict[tuple[int, int], int] = {}
    for low, high, _choice, _kind, _judge in _judgment_rows(conn):
        load[(low, high)] = load.get((low, high), 0) + 1
    return load


def _answered_by(
    conn: sqlite3.Connection, judge_kind: str, judge_id: str
) -> set[tuple[int, int]]:
    """Canonical pairs this judge has answered **both** questions for."""
    seen: dict[tuple[int, int], set[str]] = {}
    for row in conn.execute(
        "SELECT entry_a, entry_b, question FROM judgments "
        "WHERE judge_kind = ? AND judge_id = ?",
        (judge_kind, judge_id),
    ):
        a, b = int(row["entry_a"]), int(row["entry_b"])
        key = (min(a, b), max(a, b))
        seen.setdefault(key, set()).add(str(row["question"]))
    return {key for key, questions in seen.items() if set(QUESTIONS) <= questions}


def _ranked(
    conn: sqlite3.Connection,
    *,
    judge_id: str,
    judge_kind: str,
    exclude_seen: bool,
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """[(balance key, pair)], best first. The key is the balance rule itself:

    1. fewest judgments already recorded on the pair — every pair gets looked
       at before any pair gets looked at twice;
    2. a pair that mixes the two rules files before one that does not, so the
       control/treatment A/B in spec §9 actually gets scored rather than
       accumulating pairs inside one arm.

    Pairs sort by entry id after that, so the list is deterministic; the two
    equal-key pairs that fall out of it are what the rng chooses between.
    """
    rows = _published(conn)
    if len(rows) < 2:
        return []
    rules = {int(row["id"]): (row["rules_file"] or "") for row in rows}
    ids = [int(row["id"]) for row in rows]
    load = _pair_load(conn)
    skip = _answered_by(conn, judge_kind, judge_id) if exclude_seen else set()
    both_arms = {"control", "treatment"} <= set(rules.values())

    def mixed(low: int, high: int) -> bool:
        left, right = rules[low], rules[high]
        return bool(left) and bool(right) and left != right

    out: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for index, low in enumerate(ids):
        for high in ids[index + 1 :]:
            if (low, high) in skip:
                continue
            key = (
                load.get((low, high), 0),
                0 if (both_arms and mixed(low, high)) else 1,
            )
            out.append((key, (low, high)))
    out.sort(key=lambda item: (item[0], item[1]))
    return out


def candidate_pairs(
    conn: sqlite3.Connection,
    *,
    judge_id: str,
    judge_kind: str,
    exclude_seen: bool = True,
) -> list[tuple[int, int]]:
    """Every pair this judge could usefully be shown, best first (see :func:`_ranked`).

    ``exclude_seen`` drops pairs this judge has already answered both questions
    for. Never the same entry twice, and never a pair that is not two published
    entries; fewer than two published entries gives an empty list.
    """
    return [
        pair
        for _key, pair in _ranked(
            conn,
            judge_id=judge_id,
            judge_kind=judge_kind,
            exclude_seen=exclude_seen,
        )
    ]


def pick_pair(
    conn: sqlite3.Connection,
    *,
    judge_id: str,
    judge_kind: str,
    exclude_seen: bool = True,
    rng: random.Random,
) -> tuple[int, int] | None:
    """One balanced pair for this judge, or None when there is not one to give.

    Deterministic given ``rng``: the rng chooses among the pairs that tie for
    best on the balance key, and decides which of the two entries is shown as
    A, so position carries no signal. ``None`` means fewer than two published
    entries, or this judge has already answered every pair.
    """
    ranked = _ranked(
        conn, judge_id=judge_id, judge_kind=judge_kind, exclude_seen=exclude_seen
    )
    if not ranked:
        return None
    best_key = ranked[0][0]
    tied = [pair for key, pair in ranked if key == best_key]
    left, right = tied[rng.randrange(len(tied))]
    return (right, left) if rng.random() < 0.5 else (left, right)


# ---------------------------------------------------------------------------
# Recording an answer
# ---------------------------------------------------------------------------

_UPSERT = (
    "INSERT INTO judgments (entry_a, entry_b, judge_kind, judge_id, question, "
    "choice, prompt_version, artefact_hash, created_utc) VALUES (?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT (judge_kind, judge_id, entry_a, entry_b, question) "
    "DO UPDATE SET choice = excluded.choice, "
    "prompt_version = excluded.prompt_version, "
    "artefact_hash = excluded.artefact_hash, "
    "created_utc = excluded.created_utc "
    "RETURNING id"
)


def record(
    conn: sqlite3.Connection,
    *,
    entry_a: int,
    entry_b: int,
    judge_kind: str,
    judge_id: str,
    question: str,
    choice: str,
    prompt_version: str | None = None,
    artefact_hash: str | None = None,
) -> int:
    """Store one answer, replacing this judge's earlier answer to this question.

    The unique constraint in migration 001 is ``(judge_kind, judge_id, entry_a,
    entry_b, question)``, and this is its upsert: a judge may change their mind,
    and the later answer is the one that counts. Orientation is stored as given
    — the pair was shown that way round — and every reader in this module
    canonicalises it back.

    Returns the judgment row id.
    """
    if judge_kind not in POPULATIONS:
        raise ValueError(f"judge_kind must be one of {POPULATIONS}, not {judge_kind!r}")
    if question not in QUESTIONS:
        raise ValueError(f"question must be one of {QUESTIONS}, not {question!r}")
    if choice not in CHOICES:
        raise ValueError(f"choice must be one of {CHOICES}, not {choice!r}")
    entry_a, entry_b = int(entry_a), int(entry_b)
    if entry_a == entry_b:
        raise ValueError("a pair needs two different entries")
    if not str(judge_id).strip():
        raise ValueError("judge_id is required")
    row = conn.execute(
        _UPSERT,
        (
            entry_a,
            entry_b,
            judge_kind,
            judge_id,
            question,
            choice,
            prompt_version,
            artefact_hash,
            db.utc_now(),
        ),
    ).fetchone()
    return int(row["id"])


# ---------------------------------------------------------------------------
# What the static compare page is handed
# ---------------------------------------------------------------------------


def offer(
    conn: sqlite3.Connection,
    *,
    seeds: Iterable[int] = range(8),
    judge_id: str = "gallery",
    judge_kind: str = "human",
) -> list[dict[str, int]]:
    """Balanced candidate pairs for the static compare page, one per seed.

    The published gallery has no server to ask for a pair, so the generator
    bakes a short list of them into the page (and into ``pairs.json``) and the
    browser picks one. Each seed is a fresh :class:`random.Random`, so the list
    is deterministic — two renders of an unchanged database give identical
    bytes — while still spreading the offers over the balance order rather than
    handing every visitor the same pair.

    ``exclude_seen`` is off here: the generator does not know who is visiting,
    and a per-judge exclusion belongs to :func:`pick_pair` with a real judge id.
    """
    out: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for seed in seeds:
        pair = pick_pair(
            conn,
            judge_id=judge_id,
            judge_kind=judge_kind,
            exclude_seen=False,
            rng=random.Random(seed),
        )
        if pair is None:
            break
        key = (min(pair), max(pair))
        if key in seen:
            continue
        seen.add(key)
        out.append({"a": pair[0], "b": pair[1]})
    return out


def agent_verdicts(conn: sqlite3.Connection) -> dict[str, list[dict[str, str]]]:
    """Every agent verdict, keyed by ``"<low>-<high>"`` and canonicalised.

    The compare page carries these in a block that stays hidden until the human
    has answered both questions (spec §5: blind the human too, until they vote —
    being shown the agent's answer first would anchor their own). Packet 5.2
    fills the table; until then this is an empty dict and the page says so.
    """
    out: dict[str, list[dict[str, str]]] = {}
    for row in conn.execute(
        "SELECT entry_a, entry_b, judge_id, question, choice, prompt_version "
        "FROM judgments WHERE judge_kind = 'agent' "
        "ORDER BY judge_id, entry_a, entry_b, question"
    ):
        low, high, choice = _canonical(
            int(row["entry_a"]), int(row["entry_b"]), str(row["choice"])
        )
        if low == high:
            continue
        out.setdefault(f"{low}-{high}", []).append(
            {
                "judge": str(row["judge_id"]),
                "question": str(row["question"]),
                "choice": choice,
                "prompt_version": row["prompt_version"] or "",
            }
        )
    return out
