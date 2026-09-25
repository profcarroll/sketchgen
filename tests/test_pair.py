"""Unit tests for sketchgen.pair — the Console's view of the D12 pair.

Run:  python3 -m unittest discover -s tests -v

docs/plans/pooled-pair.md, Packet 1. Nothing here starts a server, runs ssh or
touches a model: the pool's endpoints are a fake ``get``, the partner is a fake
``fetch``, the link's counters and /proc/net/tcp are strings, and the one pool
URL that is really dialled is a port nothing listens on.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import console, pair, web  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "console" / "sample.json"
DEAD_URL = "http://127.0.0.1:1"

#: The pool's endpoints as llama-server b11173 answered them on flux,
#: 2026-09-25, cut to the fields pair.py reads.
PROPS = {
    "model_path": "/usr/share/ollama/.ollama/models/blobs/sha256-1194192cf2a1",
    "build_info": "b11173-84e76d8a2",
    "total_slots": 1,
    "default_generation_settings": {"n_ctx": 16384},
}
METRICS = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 4984
llamacpp:prompt_seconds_total 1.862
llamacpp:tokens_predicted_total 1001
llamacpp:tokens_predicted_seconds_total 4.66
llamacpp:requests_processing 1
"""


def slots(decoding, task=7, decoded=100):
    slot = {"id": 0, "n_ctx": 16384, "is_processing": decoding, "id_task": task,
            "n_prompt_tokens": 2391,
            "next_token": [{"has_next_token": decoding, "n_remain": -1,
                            "n_decoded": decoded}]}
    return [slot]


class FakePool:
    """``get`` for pool_block: a status and body per path."""

    def __init__(self, health=200, decoding=False, task=7, decoded=100):
        self.health = health
        self.decoding = decoding
        self.task = task
        self.decoded = decoded
        self.asked = []

    def __call__(self, url, timeout):
        path = "/" + url.rsplit("/", 1)[-1]
        self.asked.append(path)
        if path == "/health":
            return self.health, ""
        if path == "/props":
            return 200, json.dumps(PROPS)
        if path == "/slots":
            return 200, json.dumps(slots(self.decoding, self.task, self.decoded))
        if path == "/metrics":
            return 200, METRICS
        return 404, ""


def partner_document(**overrides):
    """A partner's own console document: the fixture, with d12's card in it."""
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["node"]["shape"] = "D12 box 1: Ryzen 7 9800X3D, 64G + RTX 5070 Ti 16G"
    doc["node"]["gpu"] = {"name": "NVIDIA GeForce RTX 5070 Ti",
                          "vram_mb": {"total": 16303.0, "used": 10355.0},
                          "util_pct": 40.0, "apps": []}
    doc["node"]["top"] = [
        {"pid": 711969, "cpu_pct": 31.5, "mem_pct": 0.4,
         "cmd": "ggml-rpc-server -H 127.0.0.1 -p 50052 -c"},
    ]
    doc["worker"]["control"] = "paused"
    doc["worker"]["reason"] = "lent: d12-node-flux"
    doc["model"]["resident"] = []
    for key, value in overrides.items():
        doc[key] = value
    return doc


class TestProcesses(unittest.TestCase):
    def test_the_path_tells_ollama_s_runner_from_the_pool(self):
        self.assertEqual("runner", pair.classify("/usr/local/lib/ollama/llama-server"))
        self.assertEqual("pool", pair.classify("./llama-server"))
        self.assertEqual("pool", pair.classify(
            "/home/d12-node-flux/sketchgen/llama/b11173/llama-server"))
        self.assertEqual("rpc", pair.classify("./ggml-rpc-server"))
        self.assertIsNone(pair.classify("/usr/bin/bash"))
        # a shell whose command line mentions the server is not the server
        self.assertIsNone(pair.classify("bash"))

    def test_the_walk_reads_argv0_only(self):
        with tempfile.TemporaryDirectory() as root:
            for pid, argv in ((11, ["./ggml-rpc-server", "-H", "127.0.0.1"]),
                              (12, ["bash", "-c", "pgrep ggml-rpc-server"]),
                              (13, ["/usr/local/lib/ollama/llama-server", "--model", "x"]),
                              (14, ["./llama-server", "--rpc", "127.0.0.1:50052"])):
                os.makedirs(f"{root}/{pid}")
                Path(f"{root}/{pid}/cmdline").write_bytes("\0".join(argv).encode() + b"\0")
            os.makedirs(f"{root}/self")
            self.assertEqual({"rpc": [11], "pool": [14], "runner": [13]},
                             pair.processes(root))


class TestSockets(unittest.TestCase):
    TCP = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
        "   uid  timeout inode\n"
        "   0: 0100007F:C3C4 00000000:0000 0A 00000000:00000000 00:00000000 00000000"
        "  1000        0 1 1 0000000000000000 100 0 0 10 0\n"
        "   1: 0100007F:C3C4 0100007F:A1B2 01 00000000:00000000 00:00000000 00000000"
        "  1000        0 2 1 0000000000000000 20 4 30 10 -1\n"
        "   2: 0100007F:A1B2 0100007F:C3C4 01 00000000:00000000 00:00000000 00000000"
        "  1000        0 3 1 0000000000000000 20 4 30 10 -1\n"
    )

    def test_listening_and_accepted_connections_on_the_rpc_port(self):
        # 0xC3C4 is 50116 — a port the default is not — and the pool's own end
        # of a connection (local A1B2, remote C3C4) is not an accepted one.
        self.assertEqual((True, 1), pair.parse_tcp(self.TCP, 0xC3C4))
        self.assertEqual((False, 0), pair.parse_tcp(self.TCP, pair.DEFAULT_RPC_PORT))


class TestLink(unittest.TestCase):
    ROUTE = ("Iface\tDestination\tGateway\tFlags\n"
             "tailscale0\t0040640A\t00000000\t0001\n"
             "enp7s0\t00000000\t0101A8C0\t0003\n")
    DEV = ("Inter-|   Receive |  Transmit\n"
           " face |bytes    packets errs drop fifo frame compressed multicast|bytes\n"
           "    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
           "enp7s0: 5000000 10 0 0 0 0 0 0 9000000 20 0 0 0 0 0 0\n")

    def setUp(self):
        pair._LINK_PREV.clear()
        self.addCleanup(pair._LINK_PREV.clear)

    def test_the_default_route_names_the_interface(self):
        self.assertEqual("enp7s0", pair.default_iface(self.ROUTE))
        self.assertIsNone(pair.default_iface("Iface\tDestination\n"))

    def test_counters_are_read_by_name(self):
        self.assertEqual((5000000, 9000000), pair.iface_bytes("enp7s0", self.DEV))
        self.assertIsNone(pair.iface_bytes("thunderbolt0", self.DEV))

    def test_rates_are_a_difference_and_a_single_shot_settles_once(self):
        readings = iter([(1_000_000, 2_000_000), (3_000_000, 6_000_000),
                         (4_000_000, 6_000_000)])
        clock = iter([10.0, 12.0, 14.0])
        slept = []
        read = lambda _iface: next(readings)  # noqa: E731
        now = lambda: next(clock)  # noqa: E731
        # a single shot with nothing to difference against takes a second reading
        rx, tx = pair.link_rates("enp7s0", settle=True, now=now, read=read, sleep=slept.append)
        self.assertEqual([pair.LINK_SETTLE_S], slept)
        self.assertEqual((1.0, 2.0), (rx, tx))
        # a poll differences against the last call, and does not sleep
        rx, tx = pair.link_rates("enp7s0", settle=True, now=now, read=read, sleep=slept.append)
        self.assertEqual((0.5, 0.0), (rx, tx))
        self.assertEqual(1, len(slept))

    def test_a_first_poll_without_settling_has_no_rate(self):
        self.assertEqual((None, None), pair.link_rates(
            "enp7s0", settle=False, now=lambda: 1.0, read=lambda _i: (1, 1)))


class TestPool(unittest.TestCase):
    def setUp(self):
        pair._SLOT_PREV.clear()
        pair._TAG_CACHE.clear()
        self.addCleanup(pair._SLOT_PREV.clear)
        self.addCleanup(pair._TAG_CACHE.clear)

    def test_metrics_drop_the_prefix_and_the_comments(self):
        values = pair.parse_metrics(METRICS)
        self.assertEqual(1001.0, values["tokens_predicted_total"])
        self.assertNotIn("# HELP llamacpp:prompt_tokens_total", values)

    def test_an_unreachable_pool_is_down_and_asks_nothing_else(self):
        block = pair.pool_block(DEAD_URL, pids=[], apps=[])
        self.assertEqual("down", block["state"])
        fake = FakePool(health=None)
        fake.health = 502
        self.assertEqual("down", pair.pool_block("http://pool", pids=[], apps=[],
                                                 get=fake)["state"])
        self.assertEqual(["/health"], fake.asked)

    def test_a_loading_pool_says_loading(self):
        block = pair.pool_block("http://pool", pids=[5], apps=[], get=FakePool(health=503))
        self.assertEqual("loading", block["state"])
        self.assertEqual(5, block["pid"])

    def test_an_idle_pool_reports_what_it_serves_and_has_served(self):
        apps = [{"pid": 5, "name": "llama-server", "role": "pool", "used_mb": 9856.0}]
        block = pair.pool_block("http://pool", pids=[5], apps=apps, get=FakePool())
        self.assertEqual("idle", block["state"])
        self.assertEqual("b11173", block["build"])
        self.assertEqual(16384, block["n_ctx"])
        self.assertEqual(9856.0, block["vram_mb"])
        self.assertEqual(round(1001 / 4.66, 1), block["decode_tok_s"])
        self.assertEqual(round(4984 / 1.862, 1), block["prefill_tok_s"])
        self.assertEqual((4984, 1001), (block["tokens_in"], block["tokens_out"]))
        self.assertIsNone(block["task"])

    def test_decoding_gives_a_live_rate_from_two_reads_of_one_task(self):
        clock = iter([100.0, 102.0, 104.0])
        first = pair.pool_block("http://pool", pids=[5], apps=[],
                                get=FakePool(decoding=True, decoded=100),
                                now=lambda: next(clock))
        self.assertEqual("decoding", first["state"])
        self.assertIsNone(first["live_decode_tok_s"])
        second = pair.pool_block("http://pool", pids=[5], apps=[],
                                 get=FakePool(decoding=True, decoded=530),
                                 now=lambda: next(clock))
        self.assertEqual(215.0, second["live_decode_tok_s"])
        self.assertEqual(530, second["n_decoded"])
        # a new task starts its own count, never a rate across two requests
        third = pair.pool_block("http://pool", pids=[5], apps=[],
                                get=FakePool(decoding=True, task=8, decoded=12),
                                now=lambda: next(clock))
        self.assertIsNone(third["live_decode_tok_s"])

    def test_the_blob_is_named_by_the_tag_that_points_at_it(self):
        with tempfile.TemporaryDirectory() as root:
            digest = "1194192cf2a187eb"
            for rel, target in (
                ("registry.ollama.ai/library/qwen3-coder/30b", digest),
                ("hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Q5_K_S", "a1a93585"),
            ):
                path = Path(root, "manifests", rel)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"layers": [
                    {"mediaType": "application/vnd.ollama.image.model",
                     "digest": f"sha256:{target}"}]}))
            Path(root, "blobs").mkdir()
            self.assertEqual("qwen3-coder:30b",
                             pair.blob_tag(f"{root}/blobs/sha256-{digest}"))
            self.assertEqual("hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q5_K_S",
                             pair.blob_tag(f"{root}/blobs/sha256-a1a93585"))
            self.assertIsNone(pair.blob_tag(f"{root}/blobs/sha256-ffff"))
            self.assertIsNone(pair.blob_tag("/models/qwen.gguf"))


class TestPartner(unittest.TestCase):
    def test_the_view_reads_the_partner_s_own_document(self):
        view = pair.partner_view(partner_document())
        self.assertEqual("NVIDIA GeForce RTX 5070 Ti", view["gpu"]["name"])
        self.assertEqual(10355.0, view["gpu"]["vram_mb"]["used"])
        # an older partner has no pair block: the rpc server is found in its top
        self.assertEqual(711969, view["rpc"]["pid"])
        self.assertEqual(31.5, view["rpc"]["cpu_pct"])
        self.assertIsNone(view["rpc"]["vram_mb"])
        self.assertEqual(("paused", "lent: d12-node-flux"), (view["control"], view["reason"]))
        self.assertEqual([], view["resident"])

    def test_a_partner_on_this_build_reports_its_lent_card_itself(self):
        document = partner_document()
        document["pair"] = pair.skeleton()
        document["pair"]["lent"] = {"rpc_pid": 42, "cpu_pct": 12.0, "vram_mb": 10355.0,
                                    "serving": True}
        view = pair.partner_view(document)
        self.assertEqual({"pid": 42, "cpu_pct": 12.0, "vram_mb": 10355.0, "serving": True},
                         view["rpc"])

    def test_no_document_is_the_skeleton(self):
        self.assertEqual(pair.skeleton()["partner"], pair.partner_view(None))

    def test_a_single_shot_reads_inline_and_a_poll_reads_off_thread(self):
        calls = []

        def fetch(host, command):
            calls.append((host, command))
            return partner_document()

        clock = [0.0]
        cache = pair.PartnerCache()
        spawned = []
        document, age, error = cache.snapshot("pair-partner", "cmd", sync=False,
                                              fetch=fetch, now=lambda: clock[0],
                                              spawn=spawned.append)
        # the poll did not wait: nothing read yet, one refresh queued
        self.assertIsNone(document)
        self.assertEqual([], calls)
        self.assertEqual(1, len(spawned))
        # a second poll while that refresh runs does not queue another
        cache.snapshot("pair-partner", "cmd", sync=False, fetch=fetch,
                       now=lambda: clock[0], spawn=spawned.append)
        self.assertEqual(1, len(spawned))
        spawned[0]()
        clock[0] = 2.0
        document, age, error = cache.snapshot("pair-partner", "cmd", sync=False,
                                              fetch=fetch, now=lambda: clock[0],
                                              spawn=spawned.append)
        self.assertIsNotNone(document)
        self.assertEqual(2.0, age)
        self.assertEqual(1, len(spawned))  # still inside the TTL
        # a single shot past the TTL reads now
        clock[0] = 2.0 + pair.PARTNER_TTL_S
        cache.snapshot("pair-partner", "cmd", sync=True, fetch=fetch, now=lambda: clock[0])
        self.assertEqual(2, len(calls))

    def test_a_failed_read_keeps_the_last_document_and_says_why(self):
        cache = pair.PartnerCache()
        clock = [0.0]
        cache.snapshot("p", "c", sync=True, fetch=lambda h, c: partner_document(),
                       now=lambda: clock[0])

        def refuse(host, command):
            raise OSError("ssh: connect to host 192.168.1.133 port 22: No route to host")

        clock[0] = 10.0
        document, age, error = cache.snapshot("p", "c", sync=True, fetch=refuse,
                                              now=lambda: clock[0])
        self.assertIsNotNone(document)
        self.assertEqual(10.0, age)
        self.assertIn("No route to host", error)

    def test_a_poll_really_reads_off_thread(self):
        started = threading.Event()

        def fetch(host, command):
            started.set()
            return partner_document()

        cache = pair.PartnerCache()
        cache.snapshot("p", "c", sync=False, fetch=fetch)
        self.assertTrue(started.wait(5))


class TestBlock(unittest.TestCase):
    NO_PROCS = {"rpc": [], "pool": [], "runner": []}

    def test_a_node_not_in_a_pair_is_the_skeleton(self):
        self.assertEqual(pair.skeleton(), pair.block(procs=self.NO_PROCS, env={}))

    def test_the_fixture_holds_the_skeleton(self):
        sample = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(pair.skeleton(), sample["pair"])

    def test_the_pool_url_makes_a_head_and_the_partner_is_read(self):
        cache = pair.PartnerCache()
        env = {pair.POOL_URL_ENV: DEAD_URL, pair.PARTNER_ENV: "pair-partner",
               pair.LINK_ENV: "lo"}
        block = pair.block(procs=self.NO_PROCS, env=env, sync=True, cache=cache,
                           fetch=lambda host, command: partner_document())
        self.assertEqual("head", block["role"])
        self.assertEqual("down", block["pool"]["state"])
        self.assertEqual("pair-partner", block["partner"]["host"])
        self.assertTrue(block["partner"]["reachable"])
        self.assertEqual("NVIDIA GeForce RTX 5070 Ti", block["partner"]["gpu"]["name"])
        self.assertEqual("lo", block["link"]["iface"])
        # the shape never changes with the role
        self.assertEqual(json.dumps(sorted(pair.skeleton())), json.dumps(sorted(block)))

    def test_an_rpc_server_here_makes_a_partner_without_any_setting(self):
        apps = [{"pid": 77, "name": "ggml-rpc-server", "role": "rpc", "used_mb": 10355.0}]
        block = pair.block(procs={"rpc": [77], "pool": [], "runner": []}, apps=apps,
                           proc_cpu=lambda pid: 31.5 if pid == 77 else None, env={})
        self.assertEqual("partner", block["role"])
        self.assertEqual({"rpc_pid": 77, "cpu_pct": 31.5, "vram_mb": 10355.0,
                          "serving": block["lent"]["serving"]}, block["lent"])
        self.assertIsNone(block["pool"]["state"])


class TestSlot(unittest.TestCase):
    """console._slot learns the pair (pooled-pair.md §1)."""

    def test_a_partner_s_slot_is_lent(self):
        block = pair.skeleton()
        block["role"] = "partner"
        block["lent"].update(rpc_pid=77, serving=True)
        slot = console._slot([], [], None, block, {"rpc": [77], "pool": [], "runner": []})
        self.assertEqual("lent", slot["state"])
        self.assertIn("serving the pool", slot["holder"])

    def test_the_pool_decoding_without_a_job_is_said_as_such(self):
        block = pair.skeleton()
        block["role"] = "head"
        block["pool"].update(state="decoding", pid=5)
        slot = console._slot([], [], None, block, {"rpc": [], "pool": [5], "runner": []})
        self.assertEqual("busy", slot["state"])
        self.assertIn("not a job", slot["holder"])

    def test_ollama_s_own_runner_is_not_a_stranger(self):
        resident = [{"name": "gemma4:e4b"}]
        top = [{"pid": 900, "cpu_pct": 96.0, "cmd": "llama-server --model /x"}]
        procs = {"rpc": [], "pool": [], "runner": [900]}
        self.assertEqual("free", console._slot(resident, top, None, pair.skeleton(),
                                               procs)["state"])
        # and without knowing it is Ollama's, the same row is a stranger, as before
        self.assertEqual("busy", console._slot(resident, top, None)["state"])


def head_document():
    """The fixture as flux's Console would have read it mid-replay, 2026-09-25."""
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["node"]["kind"] = "local"
    doc["node"]["gpu"] = {
        "name": "NVIDIA GeForce RTX 5080", "vram_mb": {"total": 16303.0, "used": 13019.0},
        "util_pct": 62.0,
        "apps": [
            {"pid": 5, "name": "llama-server", "role": "pool", "used_mb": 9856.0},
            {"pid": 9, "name": "llama-server", "role": "runner", "used_mb": 3163.0},
        ],
    }
    block = pair.skeleton()
    block["role"] = "head"
    block["pool"].update(url="http://127.0.0.1:8090", state="decoding", build="b11173",
                         model="qwen3-coder:30b", n_ctx=16384, pid=5, vram_mb=9856.0,
                         task=468, n_decoded=412, live_decode_tok_s=201.3,
                         decode_tok_s=214.8, prefill_tok_s=2677.8,
                         tokens_in=4984, tokens_out=1001)
    block["partner"] = pair.partner_view(partner_document())
    block["partner"].update(host="pair-partner", reachable=True, age_s=3.2)
    block["link"].update(iface="enp7s0", speed_mbps=1000, rx_mb_s=4.68, tx_mb_s=9.53,
                         rpc_port_open=True, rpc_connections=1)
    doc["pair"] = block
    return doc


def partner_role_document(control="paused"):
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["worker"]["control"] = control
    block = pair.skeleton()
    block["role"] = "partner"
    block["lent"].update(rpc_pid=77, cpu_pct=31.5, vram_mb=10355.0, serving=True)
    block["link"].update(iface="enp7s0", speed_mbps=1000, rx_mb_s=9.5, tx_mb_s=4.7,
                         rpc_port_open=True, rpc_connections=1)
    doc["pair"] = block
    return doc


class TestPanel(unittest.TestCase):
    def test_a_node_not_in_a_pair_has_no_panel(self):
        sample = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual("", web.pair_panel(sample))
        self.assertNotIn('id="pair"', web.console_page(sample))

    def test_the_head_shows_both_cards_the_pool_and_the_link_live(self):
        doc = head_document()
        page = web.console_page(doc)
        self.assertIn('id="pair"', page)
        for path in (
            'data-k="pair.pool.live_decode_tok_s"',
            'data-k="pair.pool.n_decoded"',
            'data-k="pair.pool.decode_tok_s"',
            'data-bar-num="pair.partner.gpu.vram_mb.used"',
            'data-bar-num="node.gpu.vram_mb.used"',
            'data-k="pair.partner.rpc.cpu_pct"',
            'data-k="pair.link.rx_mb_s"',
            'data-k="node.gpu.apps.0.used_mb"',
        ):
            self.assertIn(path, page)
        self.assertIn("201.3", page)
        self.assertIn("the pool (llama-server)", page)
        self.assertIn("Ollama (planner, judge, critic)", page)
        self.assertIn("lent: d12-node-flux", page)
        self.assertNotIn("billwarn", web.pair_panel(doc))

    def test_the_head_warns_about_what_needs_a_hand(self):
        doc = head_document()
        doc["pair"]["partner"]["control"] = "running"
        doc["pair"]["partner"]["resident"] = ["gemma4:e4b"]
        doc["pair"]["pool"]["state"] = "down"
        doc["pair"]["link"]["rpc_port_open"] = False
        panel = web.pair_panel(doc)
        self.assertIn("generator is running", panel)
        self.assertIn("gemma4:e4b", panel)
        self.assertIn("pool is not answering", panel)
        self.assertIn("tunnel to the partner", panel)

    def test_the_partner_says_its_card_is_lent(self):
        panel = web.pair_panel(partner_role_document())
        self.assertIn("this card is lent", panel)
        self.assertIn('data-k="pair.lent.vram_mb"', panel)
        self.assertIn(">yes<", panel)
        self.assertNotIn("billwarn", panel)
        self.assertIn("generator is running", web.pair_panel(
            partner_role_document(control="running")))

    def test_the_text_view_carries_the_pair(self):
        text = console.render_text(head_document())
        self.assertIn("pair  head — pool decoding · qwen3-coder:30b · b11173", text)
        self.assertIn("task 468: 412 tokens at 201.3 tok/s", text)
        self.assertIn("partner  pair-partner: NVIDIA GeForce RTX 5070 Ti", text)
        self.assertIn("rpc port open, 1 connection(s)", text)
        self.assertIn("pair  partner — this card is lent",
                      console.render_text(partner_role_document()))
        sample = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertNotIn("pair ", console.render_text(sample))

    def test_yes_and_no_format_the_same_on_both_sides(self):
        self.assertEqual("yes", web._fmt(True, "yn"))
        self.assertEqual("no", web._fmt(False, "yn"))
        self.assertEqual("—", web._fmt(None, "yn"))
        self.assertIn('if (kind === "yn")', web.CONSOLE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
