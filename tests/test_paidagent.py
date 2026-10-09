"""The paid-agent drives the node's paid verbs the way AGENTS.md says, with the
local model answering. The node is a fake: a script standing in for ssh that
plays start → next (answer) → import → next (wait) → next (done) from a state
file, so every verb the agent runs and every packet it sends is on record. The
model is a fake Ollama on loopback. No network beyond that, no real node."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sketchgen import paidagent  # noqa: E402

ME = "gemma4:test-q4_0"
CLEAN_PLAN = ("Brief\nA tide of slow lines crossing a pale field.\n\n"
              "Assertions\nmotion(idle)\n")

FAKE_NODE = r'''
import json, sys
state_path, log_path = sys.argv[1], sys.argv[2]
args = sys.argv[3:]
state = json.load(open(state_path))
with open(log_path, "a") as fh:
    fh.write(json.dumps({"args": args, "stdin": sys.stdin.read() if not sys.stdin.isatty() else ""}) + "\n")
verb = args[1] if len(args) > 1 else ""
def out(obj):
    print(json.dumps(obj)); json.dump(state, open(state_path, "w"))
if verb == "start":
    if state.get("not_ready"):
        out({"started": False, "preflight": {"ready": False, "checks": [
            {"ok": False, "check": "worker", "detail": "no worker", "who": "operator", "fix": "start it"}]}})
        sys.exit(3)
    state["job"] = 7; state["turn"] = 0
    out({"started": True, "job": 7, "executor": "local", "budget_say": "10 min, 30,000 tokens",
         "then": "sketchgen paid next --job 7 --as %s" % args[args.index("--as") + 1]})
elif verb == "next":
    state["turn"] += 1
    if state["turn"] == 1 or (state.get("reject") and state["turn"] <= state["reject"]):
        out({"do": "answer", "step": "plan", "job": 7, "model": "", "items": [{
            "key": "job 7", "prompt": "PLAN ME", "guard": "", "prompt_version": "planner-v2",
            "inputs": {"job": 7, "prompt": "a tide"}, "answer": "",
            "usage": {"prompt_tokens": None, "completion_tokens": None},
            "process": {"session_s": None, "output_tokens": None, "thinking_tokens": None,
                        "tool_calls": None, "screenshots": None, "effort": None}}],
             "then": "sketchgen paid import -"})
    elif state["turn"] == 2 + (state.get("reject") or 0) - (1 if state.get("reject") else 0):
        out({"do": "wait", "job": 7, "state": "executing", "say": "the worker is on job 7",
             "worker": {"headline": "Writing the sketch"}, "timed_out": True})
    else:
        out({"do": "done", "job": 7, "state": "held", "entry": 42,
             "verdict": {"exit": 0}, "elapsed": {"say": "elapsed 5 min"}})
elif verb == "import":
    packet = json.loads(open(log_path).read().splitlines()[-1])["stdin"]
    item = json.loads(packet)["items"][0]
    state["imported"] = json.loads(packet)
    if state.get("reject") and state["turn"] <= state["reject"]:
        out({"recorded": 0, "rejected": [{"item": "job 7", "reason": "no Brief heading"}], "then": ""})
    else:
        out({"recorded": 1, "landed": ["job 7"], "rejected": [], "then": "sketchgen paid next --job 7 --as x"})
elif verb == "release":
    state["released"] = args
    out({"released": [7]})
else:
    print(json.dumps({"error": "unknown verb " + verb})); sys.exit(1)
'''


class _FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append(body)
        if body.get("model") != ME:
            data = json.dumps({"error": f"model '{body.get('model')}' not found"}).encode()
            self.send_response(404)
        else:
            data = json.dumps({"model": ME, "response": self.server.reply, "done": True,
                               "prompt_eval_count": 781, "eval_count": 120,
                               "prompt_eval_duration": 22_000_000_000,
                               "eval_duration": 17_000_000_000}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class PaidAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.script = root / "fake_node.py"
        self.script.write_text(FAKE_NODE)
        self.state = root / "state.json"
        self.state.write_text("{}")
        self.log = root / "verbs.log"
        self.log.write_text("")
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
        self.fake.requests = []
        self.fake.reply = CLEAN_PLAN
        thread = threading.Thread(target=self.fake.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.fake.shutdown(), self.fake.server_close(), thread.join(5)))
        self.model = paidagent.Model(name=ME, host=f"http://127.0.0.1:{self.fake.server_address[1]}")
        self.lines = []

    def runner(self, args, stdin):
        done = subprocess.run([sys.executable, str(self.script), str(self.state), str(self.log), *args],
                              input=stdin or "", capture_output=True, text=True)
        return done.returncode, done.stdout, done.stderr

    def node(self):
        return paidagent.Node(host="fake-node", runner=self.runner)

    def verbs(self):
        return [json.loads(line)["args"][:2] for line in self.log.read_text().splitlines()]

    def test_the_loop_runs_start_next_import_next_to_done(self):
        outcome = paidagent.drive(self.node(), self.model, prompt="a tide", by="profcarroll",
                                  note="handheld", log=self.lines.append)
        self.assertEqual((outcome.do, outcome.job, outcome.entry, outcome.exit),
                         ("done", 7, 42, paidagent.EXIT_OK))
        self.assertEqual(self.verbs(), [["paid", "start"], ["paid", "next"], ["paid", "import"],
                                        ["paid", "next"], ["paid", "next"]])
        start = json.loads(self.log.read_text().splitlines()[0])["args"]
        self.assertIn("--since", start)
        self.assertEqual(start[start.index("--executor") + 1], "local")
        self.assertEqual(start[start.index("--note") + 1], "handheld")
        self.assertEqual(start[start.index("--as") + 1], ME)
        # The model got the packet's prompt, verbatim.
        self.assertEqual(self.fake.requests[0]["prompt"], "PLAN ME")

    def test_the_imported_packet_carries_the_answer_the_counts_and_the_model(self):
        paidagent.drive(self.node(), self.model, prompt="a tide", by="profcarroll", log=self.lines.append)
        packet = json.loads(self.state.read_text())["imported"]
        self.assertEqual(packet["model"], ME)
        item = packet["items"][0]
        self.assertEqual(item["answer"], CLEAN_PLAN)
        self.assertEqual(item["usage"], {"prompt_tokens": 781, "completion_tokens": 120})
        self.assertEqual(item["process"]["output_tokens"], 120)
        self.assertEqual((item["process"]["tool_calls"], item["process"]["screenshots"]), (0, 0))
        self.assertIsNone(item["process"]["thinking_tokens"])
        self.assertGreaterEqual(item["process"]["session_s"], 0.0)
        # Nothing but answer, usage, process and model changed.
        self.assertEqual(item["guard"], "")
        self.assertEqual(item["prompt_version"], "planner-v2")

    def test_a_rejected_plan_is_retried_with_a_new_seed_then_released(self):
        self.state.write_text(json.dumps({"reject": 99}))
        outcome = paidagent.drive(self.node(), self.model, prompt="a tide", by="profcarroll",
                                  log=self.lines.append, max_rejections=2)
        self.assertEqual((outcome.do, outcome.exit), ("stop", paidagent.EXIT_FAIL))
        self.assertEqual(len(outcome.rejected), 2)
        self.assertEqual([r["options"]["seed"] for r in self.fake.requests], [1, 2])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["released"][:4], ["paid", "release", "--job", "7"])
        self.assertIn("releasing", outcome.say)

    def test_not_ready_reports_the_checks_and_queues_nothing(self):
        self.state.write_text(json.dumps({"not_ready": True}))
        outcome = paidagent.drive(self.node(), self.model, prompt="a tide", by="profcarroll",
                                  log=self.lines.append)
        self.assertEqual((outcome.do, outcome.exit), ("stop", paidagent.EXIT_REFUSED))
        self.assertIn("NOT READY", outcome.say)
        self.assertIn("FAIL worker: no worker", outcome.say)
        self.assertEqual(self.verbs(), [["paid", "start"]])

    def test_the_wrong_model_name_fails_loudly_and_releases(self):
        model = paidagent.Model(name="gemma4:e4b", host=self.model.host)
        outcome = paidagent.drive(self.node(), model, prompt="a tide", by="profcarroll",
                                  log=self.lines.append)
        self.assertEqual((outcome.do, outcome.exit), ("stop", paidagent.EXIT_FAIL))
        self.assertIn("model 'gemma4:e4b' not found", outcome.say)
        self.assertEqual(json.loads(self.state.read_text())["released"][2:4], ["--job", "7"])

    def test_the_ssh_argv_matches_bin_sg(self):
        node = paidagent.Node(host="sld-cloud", remote="$HOME/sketchgen", ssh=["ssh"])
        argv = node.argv(["paid", "start", "--prompt", "a tide of 'slow' lines"])
        self.assertEqual(argv[:4], ["ssh", "-o", "BatchMode=yes", "sld-cloud"])
        self.assertEqual(argv[4], "$HOME/sketchgen/.venv/bin/python3 $HOME/sketchgen/app/bin/sketchgen "
                                  "paid start --prompt 'a tide of '\"'\"'slow'\"'\"' lines' "
                                  "--db $HOME/sketchgen/sketchgen.db")

    def test_the_verb_passes_its_options_through_after_a_double_dash(self):
        done = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "sketchgen"), "paid-agent",
                               "--", "--as", "gemma", "--by", "x", "--prompt", "p"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, paidagent.EXIT_REFUSED, done.stderr)
        self.assertIn("not name:tag", done.stderr)

    def test_main_refuses_a_bare_name_and_no_prompt(self):
        self.assertEqual(paidagent.main(["--as", "gemma", "--by", "x", "--prompt", "p"]),
                         paidagent.EXIT_REFUSED)
        self.assertEqual(paidagent.main(["--as", ME, "--by", "x"]), paidagent.EXIT_REFUSED)


if __name__ == "__main__":
    unittest.main()
