"""What models this node actually has, and which of them can do a given job.

Until 2026-09-19 the model each step used was a constant in the code
(``worker.DEFAULT_PLANNER_MODEL``, ``executor.DEFAULT_MODEL``) and the New job
page offered the planner as a two-way choice — ``local`` or ``paid`` — where
``local`` meant "whatever the worker was configured with". The node's models
now live on their own 148 GB volume (``OLLAMA_MODELS=/mnt/models``) instead of
the root disk, so there is room for more of them than the pipeline was written
to name, and the operator wants to pick between them per job. This module is
what turns "what is installed" into a list the UI can render and the validator
can check against.

**Two endpoints, not one.** ``GET /api/tags`` lists the models and *claims* to
carry each one's capabilities. It is not reliable: on this node ``gemma4:e4b``
— the planner's own default, and the model the spec calls the one on this box
that can see — is listed by ``/api/tags`` as ``completion, thinking, tools``
and by ``POST /api/show`` as ``audio, completion, thinking, tools, vision``.
The tags list is written from the manifest as it was pulled; ``/api/show``
reads the model. Filtering the planner menu on the tags list would therefore
have dropped the current default planner out of its own menu. So the list comes
from ``/api/tags`` and every capability decision comes from ``/api/show``.

**Nothing here raises.** An Ollama that is down, slow or speaking a shape we do
not recognise is an answer — an empty catalogue — not an error: the New job page
has to render either way, and a page that 500s because a model host blinked is
worse than a page offering one model. :func:`catalogue` returns ``[]`` and the
caller falls back to the configured default.

The catalogue is cached for :data:`CACHE_TTL_S` seconds because one call is
about a dozen HTTP round trips and the form is re-rendered on every keystroke's
worth of navigation. ``refresh=True`` skips the cache.

Python 3.12, stdlib only. Every timestamp is UTC, ISO 8601, with a Z.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

__all__ = [
    "CACHE_TTL_S",
    "DEFAULT_HOST",
    "PLANNER_CAPABILITY",
    "EXECUTOR_CAPABILITY",
    "Model",
    "catalogue",
    "forget",
    "is_paid",
    "label",
    "paid_models",
    "ran_off_node",
    "with_capability",
]

DEFAULT_HOST = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")

#: How long a read of the model list stands before it is read again. The set of
#: installed models changes when a person runs `ollama pull`, which is minutes
#: of download away from mattering, so a minute of staleness costs nothing and
#: saves a dozen round trips per page.
CACHE_TTL_S = 60.0

#: Per-call ceiling. The whole catalogue is read inside :data:`TOTAL_TIMEOUT_S`
#: however many models there are: a New job page that waits on a wedged model
#: host is a New job page nobody can use.
TIMEOUT_S = 2.0
TOTAL_TIMEOUT_S = 6.0

#: How many ``/api/show`` calls are in flight at once. Ollama answers these from
#: metadata without loading anything, so this is bounded for politeness rather
#: than for cost.
SHOW_WORKERS = 6

#: What a model must be able to do to be offered as a planner. The planner's own
#: call is text-only today (:mod:`sketchgen.planner` sends no images), but the
#: model named in a job's ``planner`` column is the one the spec treats as the
#: model on this box that can see — it is the judge's and the critic's model too
#: — so the menu offers the ones that could hold that whole role. Widening the
#: menu is this one line.
PLANNER_CAPABILITY = "vision"

#: The executor writes code and never looks at an image; what it needs is to
#: complete. Kept beside the planner's so the two are read together.
EXECUTOR_CAPABILITY = "completion"


@dataclass(frozen=True)
class Model:
    """One installed model, as both the menu and the validator need it."""

    name: str
    capabilities: frozenset[str]
    size_bytes: int = 0
    parameter_size: str = ""
    family: str = ""
    #: True for a model Ollama proxies to ollama.com rather than runs here.
    #: Its prompts leave the node, so the UI groups it apart from the rest and
    #: never counts it as local.
    remote: bool = False

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


# ---------------------------------------------------------------------------
# The two calls
# ---------------------------------------------------------------------------


def _get(url: str, body: dict | None = None, timeout: float = TIMEOUT_S) -> dict | None:
    """One JSON object from Ollama, or None. Never raises."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError, TimeoutError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _capabilities(host: str, name: str) -> frozenset[str]:
    """``/api/show``'s capability list for one model. See the module docstring
    for why the one in ``/api/tags`` is not good enough."""
    body = _get(host.rstrip("/") + "/api/show", {"model": name})
    words = (body or {}).get("capabilities")
    if not isinstance(words, list):
        return frozenset()
    return frozenset(str(word) for word in words if word)


def _read(host: str) -> list[Model]:
    """The catalogue, read fresh. ``[]`` when the host does not answer usably."""
    body = _get(host.rstrip("/") + "/api/tags", timeout=TOTAL_TIMEOUT_S)
    entries = (body or {}).get("models")
    if not isinstance(entries, list):
        return []

    names: list[tuple[str, dict]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("model") or "").strip()
        if name:
            names.append((name, entry))
    if not names:
        return []

    with ThreadPoolExecutor(max_workers=SHOW_WORKERS) as pool:
        caps = list(pool.map(lambda pair: _capabilities(host, pair[0]), names))

    models: list[Model] = []
    for (name, entry), capabilities in zip(names, caps):
        details = entry.get("details")
        details = details if isinstance(details, dict) else {}
        if not capabilities:
            # /api/show did not answer for this one: fall back to what the tags
            # list claimed rather than dropping the model entirely. The claim
            # can be short (gemma4:e4b) but it is never invented.
            claimed = entry.get("capabilities")
            capabilities = frozenset(
                str(word) for word in claimed if word
            ) if isinstance(claimed, list) else frozenset()
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        models.append(
            Model(
                name=name,
                capabilities=capabilities,
                size_bytes=size,
                parameter_size=str(details.get("parameter_size") or ""),
                family=str(details.get("family") or ""),
                remote=bool(entry.get("remote_model")),
            )
        )
    models.sort(key=lambda model: (model.remote, model.name))
    return models


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, list[Model]]] = {}


def catalogue(
    host: str = DEFAULT_HOST, *, refresh: bool = False, now: float | None = None
) -> list[Model]:
    """Every model this host has, local ones first. ``[]`` if it did not answer.

    Cached for :data:`CACHE_TTL_S`; ``refresh=True`` reads through. An empty
    result is cached too — a host that is down stays down for the next few
    seconds, and re-reading it per request would make every page wait on the
    same timeout.
    """
    key = host.rstrip("/")
    stamp = time.monotonic() if now is None else now
    if not refresh:
        with _LOCK:
            cached = _CACHE.get(key)
        if cached is not None and stamp - cached[0] < CACHE_TTL_S:
            return list(cached[1])
    models = _read(key)
    with _LOCK:
        _CACHE[key] = (stamp, models)
    return list(models)


def forget(host: str | None = None) -> None:
    """Drop the cache — for the tests, and for a page that has just pulled."""
    with _LOCK:
        if host is None:
            _CACHE.clear()
        else:
            _CACHE.pop(host.rstrip("/"), None)


# ---------------------------------------------------------------------------
# Asking the catalogue a question
# ---------------------------------------------------------------------------


def with_capability(
    models: list[Model], capability: str, *, include_remote: bool = True
) -> list[Model]:
    """The models that can do ``capability``, in the order they were given."""
    return [
        model
        for model in models
        if model.can(capability) and (include_remote or not model.remote)
    ]


def _gib(size_bytes: int) -> str:
    return f"{size_bytes / (1024 ** 3):.1f} GB"


#: Capabilities worth naming in a menu, in this order. ``tools`` and
#: ``thinking`` are not among them: every model in this build has them, and a
#: word on every row tells the reader nothing while costing the width that the
#: tag and the size need. The caller narrows this further — a menu already
#: filtered to vision should not print "vision" on all of its rows.
MENTION = ("vision", "audio")


def label(
    model: Model, *, default: str = "", mention: tuple[str, ...] = MENTION
) -> str:
    """How one model reads in a menu: the tag, then what it costs to run it.

    ``gemma4:e4b — 8.0B · 8.9 GB · audio (default)``
    """
    parts = [model.parameter_size] if model.parameter_size else []
    # A proxied model's "size" is the few hundred bytes of its manifest, not
    # what running it costs — saying 0.0 GB would read as free rather than as
    # elsewhere. Its weights are not on this disk, so there is no size to give.
    if model.size_bytes and not model.remote:
        parts.append(_gib(model.size_bytes))
    extra = [word for word in mention if word in model.capabilities]
    if extra:
        parts.append(", ".join(extra))
    tail = " · ".join(parts)
    text = f"{model.name} — {tail}" if tail else model.name
    if default and model.name == default:
        text += " (default)"
    return text


# ---------------------------------------------------------------------------
# Paid models: names, configured, never asked
# ---------------------------------------------------------------------------

#: A comma- or space-separated list of model ids that are answered off the node
#: — ``claude-opus-5,claude-sonnet-5`` — for the menus and for the worker's
#: decision to park a step at ``needs-laptop`` instead of calling Ollama.
PAID_MODELS_ENV = "SKETCHGEN_PAID_MODELS"

#: The word that has meant "not on this node" since migration 001.
PAID = "paid"

_PAID_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/+-"
)


def paid_models() -> list[str]:
    """The paid model ids this node offers, from :data:`PAID_MODELS_ENV`.

    A list of **names** and nothing else (docs/plans/agentic-cli.md §3.7). No
    credential, and no network call: a name is not a secret, and the node must
    not be able to verify one, because verifying it would mean calling it. Read
    on every call, like the menus' catalogue, so the web and the worker agree
    with whatever their unit files say — and both units must say the same
    thing, or the page offers a model the worker would send to Ollama.

    Order is kept, duplicates dropped, and anything that is not model-id
    shaped — or that is the word ``paid`` or ``local`` itself — is ignored.
    """
    raw = os.environ.get(PAID_MODELS_ENV, "")
    found: list[str] = []
    for word in raw.replace(",", " ").split():
        if word in (PAID, "local") or word in found:
            continue
        if not word[0].isalnum() or len(word) > 128 or set(word) - _PAID_NAME_CHARS:
            continue
        found.append(word)
    return found


def is_paid(name: str | None) -> bool:
    """True for ``paid`` and for any name in :func:`paid_models`."""
    text = (name or "").strip()
    return bool(text) and (text == PAID or text in paid_models())


def ran_off_node(name: str | None) -> bool:
    """Whether a model id recorded on an entry names a model not run here.

    What the gallery's badge reads (docs/plans/agentic-cli.md §1), so it must
    answer from the id alone, with no model host to ask: a page re-rendered a
    year from now has to reach the same verdict about the same row.

    - ``paid`` and anything in :func:`paid_models`: off the node by definition.
    - An id with no tag. Ollama names every model ``name:tag`` — ``/api/tags``
      lists ``gemma4:e4b``, never ``gemma4`` — so an id without a colon did not
      come from this node's Ollama. This is what makes entry 1223, planned by
      ``claude-sonnet-5`` before any list of paid names existed, read true.
    - A ``…cloud`` tag, which Ollama proxies to ollama.com
      (``gpt-oss:120b-cloud``): the node forwarded it, it did not run it.

    Blank, ``local`` and the test suite's ``stub`` are not claims about
    anywhere, and answer False.
    """
    text = (name or "").strip()
    if not text or text in ("local", "stub"):
        return False
    if is_paid(text):
        return True
    _, colon, tag = text.partition(":")
    return not colon or tag.endswith("cloud")
