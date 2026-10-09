"""Ollama's API, served in front of one ``llama-server``.

The pipeline speaks Ollama's native API and nothing else: ``/api/generate`` for
the planner, the executor and the critic, ``/api/chat`` with ``images`` for the
judge, ``/api/ps`` for the fence and the model card, ``/api/tags`` and
``/api/show`` for the New job menus. That was the right choice on a node that
runs Ollama. It is the wrong one on a Retroid Flip 2 (``ssh flip2-12g``), where
the model that answers is Gemma 4 E4B QAT Q4_0 under llama.cpp's own
``llama-server``, with the OpenCL and MTP work the ``ollamadreno`` lab did on it
(docs/Findings_20260930_opencl.md), and where Ollama's own ``gemma4:e4b`` is a
9.6 GB Q4_K_M that does not fit beside Chromium and Firefox in 12 GB.

So this serves the five Ollama endpoints the pipeline uses over one llama-server
speaking OpenAI's ``/v1/chat/completions`` plus its own ``/health`` and
``/props``. One upstream, one model name: the name is what the worker is
configured with (``SKETCHGEN_PLANNER_MODEL`` and friends) and is what every
attempt, verdict and critique records as its provenance, so it is given on the
command line and never guessed from the file name — ``gemma4:e4b`` is Ollama's
Q4_K_M, and this is not that. A request for any other name is answered the way
Ollama answers it, ``404 model 'x' not found``, so a misconfigured step fails
loudly rather than being answered by whatever happens to be loaded.

What is not translated:

- ``options.num_ctx`` is accepted and ignored: llama-server's window is the
  ``-c`` it was started with, and ``/api/ps`` reports that one, so the worker's
  resident check (:func:`sketchgen.worker.resident_contexts`) compares against
  the truth and never tries to reload.
- Sampling defaults are llama-server's (``/props``,
  ``default_generation_settings``), not Ollama's. ``seed``, ``temperature``,
  ``top_p`` and ``num_predict`` are passed through when a caller sends them.
- ``"think": false`` becomes ``chat_template_kwargs.enable_thinking = false``,
  which is the switch llama-server's chat templates read; a template without
  one ignores it.
- Nothing streams. The pipeline sends ``stream: false`` everywhere, and a
  streaming client gets a whole reply at the end rather than an error.

Python 3.12, stdlib only. Every timestamp is UTC, ISO 8601, with a Z.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from sketchgen import pngscale

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT_S",
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_REFUSED",
    "LOOPBACK",
    "Shim",
    "ShimServer",
    "UpstreamError",
    "capabilities",
    "window",
    "chat_body",
    "generate_body",
    "main",
    "serve",
]

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3

#: Ollama's own port, so a worker configured with the default
#: ``OLLAMA_HOST_URL`` finds this where it would have found Ollama.
DEFAULT_PORT = 11434
#: How long one upstream completion may take. A critique over two 5132×900
#: strips through the CPU image encoder is minutes, not seconds
#: (ollamadreno Findings_20260928_s2: 184 s per 1600×400 strip), and the
#: pipeline's own per-step timeouts are the ones that should fire first.
DEFAULT_TIMEOUT_S = 1800.0
#: The shim serves loopback and nothing else: the worker it answers is on the
#: same box, and a model host reachable from the room is a different decision
#: (sld-cloud's Ollama was opened to the tailnet by hand on 2026-10-09, behind
#: a firewall that admits only that interface).
LOOPBACK = ("127.0.0.1", "::1", "localhost")
#: ``/health`` and ``/props`` are answered from memory by llama-server; the
#: pipeline asks ``/api/ps`` every 30 s and the console every 2 s.
PROBE_TIMEOUT_S = 5.0
VERSION = "llama-shim 1"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class UpstreamError(Exception):
    """llama-server answered with an error, or did not answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Shim:
    """What one shim serves: one upstream, one name."""

    upstream: str
    name: str
    timeout: float = DEFAULT_TIMEOUT_S
    log: Callable[[str], None] = field(default=lambda line: print(line, file=sys.stderr))
    #: Shrink every image a caller sends so its longer side is at most this
    #: many pixels, before llama-server's encoder sees it. 0 forwards images
    #: as they came. On the Flip (2026-10-09) two of the gate's 5132×900 strips
    #: were 2253 image tokens and 605 s of prompt reading with the encoder on
    #: the Adreno; the model prices an image by its tiles. The strip on disk is
    #: untouched and the log line says what was shown.
    max_image_px: int = 0

    def base(self) -> str:
        return self.upstream.rstrip("/")

    # -- upstream ---------------------------------------------------------

    def get(self, path: str, timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any] | None:
        """One JSON object from llama-server, or None when it does not answer."""
        try:
            with urllib.request.urlopen(self.base() + path, timeout=timeout) as response:
                loaded = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError, TimeoutError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """One JSON object from llama-server. Raises :class:`UpstreamError`."""
        request = urllib.request.Request(
            self.base() + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                loaded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                text = exc.read().decode("utf-8", "replace")[:500]
            raise UpstreamError(502, f"llama-server answered {exc.code}: {text}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise UpstreamError(
                504, f"llama-server did not finish within {self.timeout:.0f} s"
            ) from exc
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise UpstreamError(502, f"llama-server at {self.upstream}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise UpstreamError(502, "llama-server answered something that is not an object")
        return loaded

    def healthy(self) -> bool:
        body = self.get("/health")
        return bool(body) and body.get("status") == "ok"

    def props(self) -> dict[str, Any]:
        return self.get("/props") or {}

    # -- the catalogue ----------------------------------------------------

    def model_entry(self, props: dict[str, Any] | None = None) -> dict[str, Any]:
        """The one ``/api/tags`` row, shaped as :mod:`sketchgen.models` reads it."""
        props = self.props() if props is None else props
        path = str(props.get("model_path") or "")
        try:
            size = os.path.getsize(path) if path else 0
        except OSError:
            # The gguf is on a box this shim cannot see; a size of 0 is what
            # models.py shows for a model whose size it does not know.
            size = 0
        return {
            "name": self.name,
            "model": self.name,
            "modified_at": "",
            "size": size,
            "digest": "",
            "details": {
                "format": "gguf",
                "family": "",
                "families": [],
                "parameter_size": "",
                "quantization_level": "",
            },
            "capabilities": capabilities(props),
        }

    def ps_entry(self, props: dict[str, Any]) -> dict[str, Any]:
        """The one ``/api/ps`` row: resident at llama-server's own window."""
        entry = self.model_entry(props)
        return {
            "name": self.name,
            "model": self.name,
            "size": entry["size"],
            "size_vram": 0,
            "context_length": window(props),
            "expires_at": "",
        }


def window(props: dict[str, Any]) -> int | None:
    """llama-server's context window from ``/props``. The b0-unknown build on
    the Flip (2026-10-09) reports it inside ``default_generation_settings``;
    older builds put ``n_ctx`` at the top. Read both, newest first."""
    settings = props.get("default_generation_settings")
    settings = settings if isinstance(settings, dict) else {}
    for value in (settings.get("n_ctx"), props.get("n_ctx")):
        if isinstance(value, int) and value > 0:
            return value
    return None


def capabilities(props: dict[str, Any]) -> list[str]:
    """Ollama's capability words for what ``/props`` says the model can take."""
    found = ["completion"]
    modalities = props.get("modalities")
    modalities = modalities if isinstance(modalities, dict) else {}
    if modalities.get("vision"):
        found.append("vision")
    if modalities.get("audio"):
        found.append("audio")
    return found


# ---------------------------------------------------------------------------
# The two translations
# ---------------------------------------------------------------------------


def _sampling(body: dict[str, Any], out: dict[str, Any]) -> None:
    """Carry the sampling options a caller sent; leave the rest to the server."""
    options = body.get("options")
    options = options if isinstance(options, dict) else {}
    for ours, theirs in (("seed", "seed"), ("temperature", "temperature"),
                         ("top_p", "top_p"), ("top_k", "top_k"),
                         ("num_predict", "max_tokens")):
        if options.get(ours) is not None:
            out[theirs] = options[ours]
    if body.get("think") is False:
        out["chat_template_kwargs"] = {"enable_thinking": False}


def _image_part(encoded: str, max_px: int = 0) -> dict[str, Any]:
    """One Ollama ``images`` entry (base64) as an OpenAI ``image_url`` part,
    shrunk first when ``max_px`` asks and the bytes are a PNG this can read."""
    if max_px > 0:
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raw = b""
        if raw:
            smaller = pngscale.downscale(raw, max_px)
            if smaller is not raw:
                encoded = base64.b64encode(smaller).decode("ascii")
    head = encoded[:8]
    if head.startswith("iVBOR"):
        mime = "image/png"
    elif head.startswith("/9j/"):
        mime = "image/jpeg"
    elif head.startswith("R0lGOD"):
        mime = "image/gif"
    elif head.startswith("UklGR"):
        mime = "image/webp"
    else:
        mime = "application/octet-stream"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def _message(role: str, content: str, images: list[str] | None,
             max_px: int = 0) -> dict[str, Any]:
    if not images:
        return {"role": role, "content": content}
    parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
    parts.extend(_image_part(str(image), max_px) for image in images)
    return {"role": role, "content": parts}


def generate_body(body: dict[str, Any], max_px: int = 0) -> dict[str, Any]:
    """An ``/api/generate`` body as a ``/v1/chat/completions`` body.

    The prompt is one user turn, which is what Ollama makes of it when it
    applies the model's template; ``system`` becomes a system turn and
    ``images`` go with the user turn, as on ``/api/chat``.
    """
    messages = []
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    images = body.get("images")
    messages.append(_message("user", str(body.get("prompt") or ""),
                             images if isinstance(images, list) else None, max_px))
    out: dict[str, Any] = {"messages": messages, "stream": False}
    _sampling(body, out)
    return out


def chat_body(body: dict[str, Any], max_px: int = 0) -> dict[str, Any]:
    """An ``/api/chat`` body as a ``/v1/chat/completions`` body."""
    messages = []
    for item in body.get("messages") or []:
        if not isinstance(item, dict):
            continue
        images = item.get("images")
        messages.append(_message(str(item.get("role") or "user"),
                                 str(item.get("content") or ""),
                                 images if isinstance(images, list) else None, max_px))
    out: dict[str, Any] = {"messages": messages, "stream": False}
    _sampling(body, out)
    return out


def _completion(shim: Shim, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Run one translated completion; the text and Ollama's accounting fields."""
    started = time.monotonic()
    answer = shim.post("/v1/chat/completions", body)
    wall_ns = int((time.monotonic() - started) * 1e9)
    choices = answer.get("choices") or [{}]
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    content = content if isinstance(content, str) else ""
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    timings = answer.get("timings") if isinstance(answer.get("timings"), dict) else {}
    prompt_ms = float(timings.get("prompt_ms") or 0.0)
    predicted_ms = float(timings.get("predicted_ms") or 0.0)
    finish = first.get("finish_reason")
    fields = {
        "done": True,
        "done_reason": "length" if finish == "length" else "stop",
        "total_duration": wall_ns,
        "load_duration": 0,
        "prompt_eval_count": int(usage.get("prompt_tokens") or 0),
        "prompt_eval_duration": int(prompt_ms * 1e6),
        "eval_count": int(usage.get("completion_tokens") or 0),
        "eval_duration": int(predicted_ms * 1e6),
    }
    return content, fields


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class ShimServer(ThreadingHTTPServer):
    """One thread per request, so ``/api/ps`` answers while a completion runs
    (llama-server queues the second completion itself, ``-np 1``)."""

    daemon_threads = True
    allow_reuse_address = True
    shim: Shim


class _Handler(BaseHTTPRequestHandler):
    server: ShimServer
    protocol_version = "HTTP/1.1"

    # Silence the per-request line; the shim writes its own, with the counts.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    @property
    def shim(self) -> Shim:
        return self.server.shim

    # -- plumbing ---------------------------------------------------------

    def _send(self, status: int, body: dict[str, Any] | str) -> None:
        if isinstance(body, str):
            data = body.encode("utf-8")
            kind = "text/plain; charset=utf-8"
        else:
            data = json.dumps(body).encode("utf-8")
            kind = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            loaded = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._error(400, "the request body is not JSON")
            return None
        if not isinstance(loaded, dict):
            self._error(400, "the request body is not an object")
            return None
        return loaded

    def _named(self, body: dict[str, Any]) -> bool:
        """True when the request names the model this shim serves; else 404,
        in Ollama's own words, so a misconfigured step fails as it would there."""
        asked = str(body.get("model") or "")
        if asked == self.shim.name:
            return True
        self._error(404, f"model '{asked}' not found")
        return False

    # -- GET --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, f"{VERSION} is running")
        elif path == "/api/version":
            props = self.shim.props()
            self._send(200, {"version": f"{VERSION} over llama-server "
                                        f"{props.get('build_info') or '?'}"})
        elif path == "/api/tags":
            self._send(200, {"models": [self.shim.model_entry()]})
        elif path == "/api/ps":
            if self.shim.healthy():
                self._send(200, {"models": [self.shim.ps_entry(self.shim.props())]})
            else:
                self._send(200, {"models": []})
        else:
            self._error(404, f"no such endpoint: {path}")

    # -- POST -------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._body()
        if body is None:
            return
        if path == "/api/show":
            if self._named(body):
                props = self.shim.props()
                entry = self.shim.model_entry(props)
                self._send(200, {"capabilities": entry["capabilities"],
                                 "details": entry["details"], "model_info": {}})
        elif path == "/api/generate":
            if self._named(body):
                self._generate(body)
        elif path == "/api/chat":
            if self._named(body):
                self._chat(body)
        else:
            self._error(404, f"no such endpoint: {path}")

    def _generate(self, body: dict[str, Any]) -> None:
        if not body.get("prompt"):
            # Ollama's load idiom: a prompt-less generate brings the runner up
            # and returns. llama-server loaded at start, so this is a health
            # check with the same answer (sketchgen.executor.warm reads only
            # how long it took).
            if not self.shim.healthy():
                self._error(503, f"llama-server at {self.shim.upstream} is not up")
                return
            self._send(200, {"model": self.shim.name, "created_at": utc_now(),
                             "response": "", "done": True, "done_reason": "load"})
            return
        try:
            content, fields = _completion(self.shim, generate_body(body, self.shim.max_image_px))
        except UpstreamError as exc:
            self.shim.log(f"{utc_now()}  POST /api/generate {self.shim.name}: {exc.message}")
            self._error(exc.status, exc.message)
            return
        self._log("generate", fields)
        self._send(200, {"model": self.shim.name, "created_at": utc_now(),
                         "response": content, **fields})

    def _chat(self, body: dict[str, Any]) -> None:
        try:
            content, fields = _completion(self.shim, chat_body(body, self.shim.max_image_px))
        except UpstreamError as exc:
            self.shim.log(f"{utc_now()}  POST /api/chat {self.shim.name}: {exc.message}")
            self._error(exc.status, exc.message)
            return
        images = sum(len(m.get("images") or []) for m in body.get("messages") or []
                     if isinstance(m, dict))
        self._log("chat", fields, images=images)
        self._send(200, {"model": self.shim.name, "created_at": utc_now(),
                         "message": {"role": "assistant", "content": content}, **fields})

    def _log(self, verb: str, fields: dict[str, Any], *, images: int = 0) -> None:
        pictures = f"  {images} image(s)" if images else ""
        if images and self.shim.max_image_px:
            pictures += f" shown at ≤{self.shim.max_image_px} px"
        self.shim.log(
            f"{utc_now()}  POST /api/{verb} {self.shim.name}  "
            f"{fields['prompt_eval_count']}→{fields['eval_count']} tok  "
            f"{fields['total_duration'] / 1e9:.1f} s{pictures}"
        )


def serve(bind: str, port: int, shim: Shim) -> ShimServer:
    """A server bound and ready; the caller runs ``serve_forever``.

    Refuses (``ValueError``) any bind but loopback: see :data:`LOOPBACK`.
    """
    if bind not in LOOPBACK:
        raise ValueError(f"the shim serves loopback only, not {bind}")
    server = ShimServer((bind, port), _Handler)
    server.shim = shim
    return server


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sketchgen llama-shim",
        description="Serve Ollama's API on loopback in front of one llama-server.",
    )
    parser.add_argument("--upstream", required=True, metavar="URL",
                        help="the llama-server, e.g. http://127.0.0.1:8092")
    parser.add_argument("--name", required=True, metavar="TAG",
                        help="the model name the worker is configured with and "
                             "every record will carry, e.g. gemma4:e4b-qat-q4_0")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="address to bind (127.0.0.1 only; anything else exits 3)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port (default {DEFAULT_PORT}, Ollama's own)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, metavar="S",
                        help=f"ceiling on one completion (default {DEFAULT_TIMEOUT_S:.0f})")
    parser.add_argument("--max-image-px", type=int, default=0, metavar="PX",
                        help="shrink every image sent to the model so its longer side is at "
                             "most PX (default 0: as sent). The strip on disk is untouched.")
    args = parser.parse_args(argv)
    if ":" not in args.name:
        print(f"sketchgen: refused: --name {args.name!r} is not name:tag, which is "
              "what every model id in the database looks like", file=sys.stderr)
        return EXIT_REFUSED
    shim = Shim(upstream=args.upstream, name=args.name, timeout=args.timeout,
                max_image_px=max(0, args.max_image_px))
    try:
        server = serve(args.bind, args.port, shim)
    except ValueError as exc:
        print(f"sketchgen: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as exc:
        print(f"sketchgen: cannot bind {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return EXIT_FAIL
    up = "up" if shim.healthy() else "not answering yet (it will be asked again per request)"
    shim.log(f"{utc_now()}  {VERSION} on http://{args.bind}:{server.server_address[1]}/ "
             f"serving {args.name} over {args.upstream} ({up})"
             + (f"; images shown at ≤{shim.max_image_px} px" if shim.max_image_px else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - operator's Ctrl-C
        pass
    finally:
        server.server_close()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
