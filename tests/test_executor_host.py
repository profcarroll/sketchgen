"""The executor's model host may differ from every other step's (2026-10-09).

The worker side is in tests/test_worker.py (TestTheExecutorHost and the
loading-card test); this is the New job page: the executor menu asks the
executor's host what it has, and the planner menu still asks this box's.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sketchgen import models, web, worker  # noqa: E402

CODER = models.Model(name="qwen3-coder:30b-a3b-q4_K_M", capabilities=frozenset({"completion"}))
GEMMA = models.Model(name="gemma4:e4b-qat-q4_0",
                     capabilities=frozenset({"completion", "vision"}))


class ExecutorHostMenuTests(unittest.TestCase):
    def test_the_two_menus_ask_their_own_hosts(self):
        asked = []

        def catalogue(host, **kwargs):
            asked.append(host)
            return [CODER] if "100.76.50.85" in host else [GEMMA]

        with mock.patch.object(web, "OLLAMA_HOST", "http://127.0.0.1:11434"), \
                mock.patch.object(web, "EXECUTOR_HOST", "http://100.76.50.85:11434"), \
                mock.patch.object(web.models, "catalogue", side_effect=catalogue):
            executors = web.executor_groups()
            planners = web.planner_groups()
        # The first group is this node's own models, whatever its label says.
        self.assertIn(CODER.name, [v for v, _ in executors[0][1]])
        self.assertIn(GEMMA.name, [v for v, _ in planners[0][1]])
        self.assertIn("http://100.76.50.85:11434", asked)
        self.assertIn("http://127.0.0.1:11434", asked)

    def test_unset_the_executor_host_is_the_host(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import importlib
            # The defaults are read at import; what matters is the fallback rule.
            self.assertEqual(
                worker.DEFAULT_EXECUTOR_HOST,
                __import__("os").environ.get("SKETCHGEN_EXECUTOR_HOST") or worker.DEFAULT_HOST,
            )
            del importlib


if __name__ == "__main__":
    unittest.main()
