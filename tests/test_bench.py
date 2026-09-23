"""Unit tests for `sketchgen bench` — the same measurement on every node.

Run:  python3 -m unittest tests.test_bench

No model host: a fake Ollama answers the five endpoints the bench uses and
records what it was sent. What is held here is the part that makes the
numbers comparable — refusing while the generator runs, a cold load per
model, a first line that differs per request so the prefix cache cannot
answer, cases that do not fit the context skipped rather than truncated, and
--compare matching on digest.
"""

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen.cli import bench  # noqa: E402


class FakeOllama:
    """Enough of Ollama's API for the bench, with every request kept."""

    def __init__(self, models, cached_prompt=False):
        self.models = models          # name -> (digest, capabilities)
        self.loaded: dict[str, int] = {}
        self.sent: list[tuple[str, dict | None]] = []
        self.cached_prompt = cached_prompt

    def __call__(self, host, path, body, timeout):
        self.sent.append((path, body))
        if path == "/api/version":
            return {"version": "0.30.0"}
        if path == "/api/tags":
            return {"models": [
                {"name": n, "digest": d + "0" * 52, "size": 19_000_000_000,
                 "details": {"parameter_size": "30.5B", "quantization_level": "Q4_K_M"}}
                for n, (d, _c) in self.models.items()]}
        if path == "/api/show":
            return {"capabilities": list(self.models[body["model"]][1])}
        if path == "/api/ps":
            return {"models": [{"name": n, "size": 20_000_000_000,
                                "size_vram": 15_000_000_000} for n in self.loaded]}
        if path == "/api/generate":
            if body.get("keep_alive") == 0:
                self.loaded.pop(body["model"], None)
                return {}
            fresh = body["model"] not in self.loaded
            self.loaded[body["model"]] = body["options"]["num_ctx"]
            if "prompt" not in body:
                return {"load_duration": 4_300_000_000}
            prompt_tokens = 12 if self.cached_prompt else len(body["prompt"]) // 4
            return {
                "prompt_eval_count": prompt_tokens,
                "prompt_eval_duration": prompt_tokens * 1_000_000,   # 1000 tok/s
                "eval_count": 512,
                "eval_duration": 5_120_000_000,                      # 100 tok/s
                "load_duration": 4_000_000_000 if fresh else 1_000_000,
            }
        raise AssertionError(f"unexpected {path}")


def args(**over):
    base = dict(compare=None, db="/nonexistent/sketchgen.db", host="http://fake",
                model=["exec:tag", "gemma4:e4b"], repeat=2, parallel=1, num_ctx=None,
                out=None, json=True)
    base.update(over)
    return argparse.Namespace(**base)


@contextlib.contextmanager
def quiet_machine():
    """No nvidia-smi, no systemctl, no git: the machine facts come back blank."""
    with mock.patch.object(bench, "_run", return_value=""):
        yield


def run(fake, **over):
    out, err = io.StringIO(), io.StringIO()
    with quiet_machine(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bench.cmd(args(**over), post=fake)
    return code, out.getvalue(), err.getvalue()


MODELS = {"exec:tag": ("06c1097efce0", ["completion", "tools"]),
          "gemma4:e4b": ("c6eb396dbd59", ["completion", "thinking", "vision"])}


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sketchgen-bench-")
        self.db_path = str(Path(self._tmp.name) / "sketchgen.db")
        db.init(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def set_state(self, state):
        conn = db.connect(self.db_path)
        db.set_control(conn, state, "test")
        conn.commit()
        conn.close()

    def test_refuses_while_the_generator_runs_and_sends_nothing(self):
        for state in ("running", "pausing"):
            self.set_state(state)
            fake = FakeOllama(MODELS)
            code, _out, err = run(fake, db=self.db_path)
            self.assertEqual(code, bench.EXIT_REFUSED, state)
            self.assertIn(state, err)
            self.assertEqual(fake.sent, [])

    def test_runs_when_paused(self):
        self.set_state("paused")
        code, out, _err = run(FakeOllama(MODELS), db=self.db_path)
        self.assertEqual(code, bench.EXIT_OK)
        self.assertEqual(len(json.loads(out)["models"]), 2)

    def test_refuses_a_model_that_is_not_installed(self):
        code, _out, err = run(FakeOllama(MODELS), model=["qwen3-coder:30b"])
        self.assertEqual(code, bench.EXIT_REFUSED)
        self.assertIn("qwen3-coder:30b", err)

    def test_no_model_host_is_a_failure(self):
        def down(*_a):
            raise OSError("connection refused")
        code, _out, err = run(down)
        self.assertEqual(code, bench.EXIT_FAIL)
        self.assertIn("connection refused", err)


class MeasurementTests(unittest.TestCase):
    def test_result_shape_and_rates(self):
        fake = FakeOllama(MODELS)
        code, out, _err = run(fake)
        self.assertEqual(code, bench.EXIT_OK)
        result = json.loads(out)
        self.assertEqual(result["bench_version"], bench.BENCH_VERSION)
        self.assertEqual(result["prompts"], bench.prompts_digest())
        executor = result["models"][0]
        self.assertEqual(executor["model"]["digest"], "06c1097efce0")
        self.assertEqual(executor["load"]["load_s"], 4.3)
        self.assertEqual(executor["load"]["on_gpu_pct"], 75)
        self.assertEqual(executor["load"]["num_ctx"], bench.EXECUTOR_NUM_CTX)
        short = executor["cases"]["short"]
        self.assertEqual(short["decode_tok_s"], 100.0)
        self.assertEqual(short["prefill_tok_s"], 1000.0)
        self.assertEqual(len(short["runs"]), 2)
        self.assertNotIn("flags", short)

    def test_every_model_is_cold_loaded_after_an_unload(self):
        fake = FakeOllama(MODELS)
        fake.loaded["gemma4:e4b"] = 8192       # resident before the bench starts
        run(fake)
        unloads = [b["model"] for p, b in fake.sent
                   if p == "/api/generate" and b.get("keep_alive") == 0]
        self.assertIn("gemma4:e4b", unloads)
        loads = [b["model"] for p, b in fake.sent
                 if p == "/api/generate" and "prompt" not in b and b.get("keep_alive") != 0]
        self.assertEqual(loads, ["exec:tag", "gemma4:e4b"])

    def test_no_two_requests_share_a_first_line(self):
        fake = FakeOllama(MODELS)
        run(fake, parallel=2)
        firsts = [(b["model"], b["prompt"].splitlines()[0]) for p, b in fake.sent
                  if p == "/api/generate" and "prompt" in b]
        self.assertEqual(len(firsts), len(set(firsts)))

    def test_the_planner_runs_at_its_own_context_and_skips_what_does_not_fit(self):
        _code, out, _err = run(FakeOllama(MODELS))
        planner = json.loads(out)["models"][1]
        self.assertEqual(planner["load"]["num_ctx"], bench.PLANNER_NUM_CTX)
        self.assertIn("skipped", planner["cases"]["source"])
        self.assertIn("decode_tok_s", planner["cases"]["rules"])

    def test_think_is_sent_only_to_a_model_that_can_think(self):
        fake = FakeOllama(MODELS)
        run(fake)
        for path, body in fake.sent:
            if path == "/api/generate" and "prompt" in body:
                self.assertEqual("think" in body, body["model"] == "gemma4:e4b")

    def test_requests_are_deterministic(self):
        fake = FakeOllama(MODELS)
        run(fake)
        for path, body in fake.sent:
            if path == "/api/generate" and "prompt" in body:
                self.assertEqual(body["options"]["seed"], bench.SEED)
                self.assertEqual(body["options"]["temperature"], 0)
                self.assertEqual(body["options"]["num_predict"], bench.NUM_PREDICT)
                self.assertFalse(body["stream"])

    def test_a_load_without_a_counter_falls_back_to_the_clock(self):
        fake = FakeOllama(MODELS)
        real = fake.__call__

        def no_counter(host, path, body, timeout):
            reply = real(host, path, body, timeout)
            if path == "/api/generate" and body and "prompt" not in body:
                reply.pop("load_duration", None)
            return reply
        _code, out, _err = run(no_counter, model=["exec:tag"])
        loaded = json.loads(out)["models"][0]["load"]
        self.assertFalse(loaded["load_counted"])
        self.assertEqual(loaded["load_s"], loaded["wall_s"])

    def test_the_fit_check_uses_the_measured_ratio(self):
        # 12k asked is ~16k real: it fits 16384 with 512 out, and not 8192.
        self.assertTrue(bench.fits(12000, 16384))
        self.assertFalse(bench.fits(12000, 8192))
        self.assertFalse(bench.fits(4000, 5500))

    def test_a_cached_prompt_is_flagged(self):
        _code, out, _err = run(FakeOllama(MODELS, cached_prompt=True))
        self.assertIn("prompt cached", json.loads(out)["models"][0]["cases"]["short"]["flags"])

    def test_parallel_reports_an_aggregate(self):
        _code, out, _err = run(FakeOllama(MODELS), parallel=3, model=["exec:tag"])
        short = json.loads(out)["models"][0]["cases"]["short"]
        self.assertIn("aggregate_decode_tok_s", short)
        self.assertEqual(short["runs"][0]["parallel"], 3)

    def test_prompts_grow_with_the_case(self):
        sizes = [len(bench.prompt_for(c, t, 1)) for c, t in bench.CASES]
        self.assertEqual(sizes, sorted(sizes))
        self.assertAlmostEqual(sizes[-1] / 4, bench.CASES[-1][1], delta=10)


class HostTests(unittest.TestCase):
    def test_the_worker_units_host_is_the_default(self):
        unit = "Environment=OLLAMA_HOST_URL=http://100.107.156.77:11434 SKETCHGEN_X=1"
        with mock.patch.object(bench, "_run", return_value=unit.split("=", 1)[1]):
            self.assertEqual(bench.default_host(), "http://100.107.156.77:11434")

    def test_loopback_without_a_unit(self):
        with mock.patch.object(bench, "_run", return_value=""):
            self.assertEqual(bench.default_host(), "http://127.0.0.1:11434")

    def test_host_none_resolves_before_the_first_request(self):
        seen = []
        fake = FakeOllama(MODELS)

        def post(host, *rest):
            seen.append(host)
            return fake(host, *rest)
        run(post, host=None, model=["exec:tag"])
        self.assertEqual(set(seen), {"http://127.0.0.1:11434"})


class CompareTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sketchgen-bench-")
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def saved(self, name, hostname, tag, prompts=None):
        _code, out, _err = run(FakeOllama({tag: MODELS["exec:tag"]}), model=[tag])
        result = json.loads(out)
        result["node"]["hostname"] = hostname
        if prompts:
            result["prompts"] = prompts
        path = self.dir / name
        path.write_text(json.dumps(result))
        return str(path)

    def test_nodes_line_up_on_digest_whatever_the_tag(self):
        a = self.saved("a.json", "d12-node", "qwen3-coder:30b")
        b = self.saved("b.json", "sld-cloud", "qwen3-coder:30b-a3b-q4_K_M")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench.compare([a, b])
        self.assertEqual(code, bench.EXIT_OK)
        lines = [line for line in out.getvalue().splitlines() if "short" in line]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("06c1097efce0" in line for line in lines))
        self.assertEqual(err.getvalue(), "")

    def test_different_prompts_are_warned_about(self):
        a = self.saved("a.json", "d12-node", "exec:tag")
        b = self.saved("b.json", "sld-cloud", "exec:tag", prompts="000000000000")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            bench.compare([a, b])
        self.assertIn("not comparable", err.getvalue())


if __name__ == "__main__":
    unittest.main()
