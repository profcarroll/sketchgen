"""The llama-shim serves Ollama's API well enough for the pipeline's own clients.

A fake llama-server (``/health``, ``/props``, ``/v1/chat/completions``) stands in
for the Flip's; the shim is started in front of it on a free loopback port, and
the real clients -- planner.plan, executor._call_ollama, judge's /api/chat body,
models.catalogue, worker.resident_contexts -- are pointed at it. Each test
reads the body the fake received, which is the translation under test. No
network beyond loopback, no model, no browser.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sketchgen import executor, llamashim, models, planner, worker  # noqa: E402

NAME = "gemma4:test-q4_0"
PLAN_REPLY = ("Brief\nA tide of slow lines crossing a pale field, each one drawn a "
              "little later than the last.\n\nAssertions\nmotion(idle)\n")


class _FakeLlama(BaseHTTPRequestHandler):
    """llama-server as the shim sees it: health, props, one completion."""

    def log_message(self, *args):
        return

    def _send(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/props":
            self._send(200, {
                "model_path": self.server.model_path,
                # Where the Flip's build puts the window (llamashim.window).
                "default_generation_settings": {"n_ctx": 8192, "params": {}},
                "modalities": {"vision": True, "video": True, "audio": True},
                "build_info": "b0-test",
            })
        else:
            self._send(404, {"error": "no"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append((self.path, body))
        if self.server.fail_with:
            self._send(self.server.fail_with, {"error": {"message": "boom"}})
            return
        self._send(200, {
            "choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": self.server.reply}}],
            "usage": {"prompt_tokens": 781, "completion_tokens": 120},
            "timings": {"prompt_ms": 22361.5, "predicted_ms": 17104.2},
        })


class ShimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        gguf = Path(self.tmp.name) / "model.gguf"
        gguf.write_bytes(b"GGUF" * 256)
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlama)
        self.fake.model_path = str(gguf)
        self.fake.requests = []
        self.fake.reply = PLAN_REPLY
        self.fake.fail_with = 0
        self.fake_thread = threading.Thread(target=self.fake.serve_forever, daemon=True)
        self.fake_thread.start()
        self.addCleanup(self._stop, self.fake, self.fake_thread)
        upstream = f"http://127.0.0.1:{self.fake.server_address[1]}"
        self.lines = []
        self.shim = llamashim.Shim(upstream=upstream, name=NAME, timeout=10.0,
                                   log=self.lines.append)
        self.server = llamashim.serve("127.0.0.1", 0, self.shim)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop, self.server, self.thread)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        models.forget()

    @staticmethod
    def _stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def _post(self, path, body):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    # -- the catalogue, as models.py and the worker read it -------------------

    def test_catalogue_lists_the_one_model_with_its_capabilities(self):
        found = models.catalogue(self.url, refresh=True)
        self.assertEqual([m.name for m in found], [NAME])
        self.assertEqual(found[0].capabilities, frozenset({"completion", "vision", "audio"}))
        self.assertEqual(found[0].size_bytes, 1024)
        self.assertFalse(found[0].remote)
        self.assertTrue(found[0].can(models.PLANNER_CAPABILITY))
        self.assertTrue(found[0].can(models.EXECUTOR_CAPABILITY))

    def test_show_refuses_another_name_as_ollama_does(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/show", {"model": "gemma4:e4b"})
        with caught.exception as refused:
            self.assertEqual(refused.code, 404)
            self.assertIn("model 'gemma4:e4b' not found", refused.read().decode("utf-8"))

    def test_ps_reports_the_servers_own_window(self):
        resident, error = worker.resident_contexts(self.url)
        self.assertIsNone(error)
        self.assertEqual(resident, {NAME: 8192})
        names, error = worker.resident_models(self.url)
        self.assertEqual((names, error), ([NAME], None))

    def test_ps_is_empty_when_llama_server_is_down(self):
        self._stop(self.fake, self.fake_thread)
        self.assertEqual(self._get("/api/ps"), {"models": []})

    # -- /api/generate, as the planner and the executor send it -------------

    def test_planner_plans_through_the_shim(self):
        plan = planner.plan("a tide of slow lines", NAME, host=self.url, by="tester")
        self.assertIn("tide of slow lines", plan.brief)
        self.assertEqual(plan.assertions, ["motion(idle)"])
        self.assertEqual(plan.tokens, {"prompt": 781, "response": 120})
        path, body = self.fake.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual([m["role"] for m in body["messages"]], ["user"])
        self.assertIn("a tide of slow lines", body["messages"][0]["content"])
        self.assertEqual(body["seed"], 1)
        self.assertFalse(body["stream"])
        # num_ctx is llama-server's -c, not a request option: never forwarded.
        self.assertNotIn("num_ctx", json.dumps(body))

    def test_executor_call_maps_think_false_and_the_durations(self):
        self.fake.reply = "```js\nfunction setup(){}\n```\nA statement.\n"
        answer = executor._call_ollama(self.url, NAME, "write a sketch", 7, 16384, 10.0)
        self.assertEqual(answer["response"], self.fake.reply)
        self.assertEqual(answer["prompt_eval_count"], 781)
        self.assertEqual(answer["eval_count"], 120)
        self.assertEqual(answer["prompt_eval_duration"], 22361500000)
        self.assertEqual(answer["eval_duration"], 17104200000)
        self.assertTrue(answer["done"])
        _, body = self.fake.requests[-1]
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["seed"], 7)

    def test_generate_refuses_another_name(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/generate", {"model": "qwen3-coder:30b", "prompt": "x"})
        with caught.exception as refused:
            self.assertEqual(refused.code, 404)
        self.assertEqual(self.fake.requests, [])

    def test_warm_is_a_health_check(self):
        took = executor.warm(self.url, NAME, 8192, timeout=10.0)
        self.assertGreaterEqual(took, 0.0)
        self.assertEqual(self.fake.requests, [], "a warm must not run a completion")

    def test_an_upstream_error_is_a_502_the_client_sees(self):
        self.fake.fail_with = 500
        with self.assertRaises(planner.PlannerFailed) as caught:
            planner.plan("x", NAME, host=self.url, by="tester")
        cause = caught.exception.__cause__
        if isinstance(cause, urllib.error.HTTPError):
            cause.close()
        self.assertTrue(any("answered 500" in line for line in self.lines))

    # -- /api/chat with images, as the judge sends it -------------------------

    def test_judge_images_become_data_urls_in_order(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 16
        images = [judge_b64(png), judge_b64(jpg)]
        self.fake.reply = "A"
        status, answer = self._post("/api/chat", {
            "model": NAME,
            "messages": [{"role": "user", "content": "Which is closer?", "images": images}],
            "stream": False, "options": {"num_ctx": 8192, "seed": 3},
        })
        self.assertEqual(status, 200)
        self.assertEqual(answer["message"], {"role": "assistant", "content": "A"})
        self.assertEqual(answer["prompt_eval_count"], 781)
        _, body = self.fake.requests[-1]
        parts = body["messages"][0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "Which is closer?"})
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(any("2 image(s)" in line for line in self.lines))

    # -- the verb ------------------------------------------------------------

    def test_main_refuses_a_bind_off_loopback_and_a_bare_name(self):
        self.assertEqual(llamashim.main(["--upstream", "http://x", "--name", NAME,
                                         "--bind", "0.0.0.0", "--port", "0"]),
                         llamashim.EXIT_REFUSED)
        self.assertEqual(llamashim.main(["--upstream", "http://x", "--name", "gemma",
                                         "--port", "0"]),
                         llamashim.EXIT_REFUSED)


def judge_b64(data: bytes) -> str:
    """The judge's own encoding of a strip: base64 text in Ollama's ``images``."""
    return base64.b64encode(data).decode("ascii")


if __name__ == "__main__":
    unittest.main()
