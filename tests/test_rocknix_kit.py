"""The ROCKNIX kit holds together without a device: the scripts parse, every unit
reads only variables node.env.example defines, and the units point at the
layout install.sh builds. The device itself is tested by running the kit."""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT = REPO_ROOT / "rocknix"


class RocknixKitTests(unittest.TestCase):
    def test_the_scripts_parse(self):
        for script in ("install.sh", "publish-local.sh"):
            done = subprocess.run(["bash", "-n", str(KIT / script)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_every_unit_variable_is_in_the_env_template(self):
        defined = set(re.findall(r"^([A-Z_]+)=", (KIT / "node.env.example").read_text(), re.M))
        for unit in sorted((KIT / "units").glob("*.service")):
            text = unit.read_text()
            used = set(re.findall(r"\$\{?([A-Z_]+)\}?", text))
            self.assertTrue(used <= defined, f"{unit.name} reads {used - defined}")
            self.assertIn("EnvironmentFile=/storage/sketchgen/node.env", text) if used else None
            self.assertIn("[Install]", text)

    def test_install_writes_every_variable_the_units_read(self):
        script = (KIT / "install.sh").read_text()
        for unit in (KIT / "units").glob("*.service"):
            for name in set(re.findall(r"\$\{?([A-Z_]+)\}?", unit.read_text())):
                self.assertIn(name + "=", script, f"install.sh never writes {name}")

    def test_the_shim_is_served_under_the_planner_name(self):
        shim = (KIT / "units" / "sketchgen-shim.service").read_text()
        self.assertIn("--name ${SKETCHGEN_PLANNER_MODEL}", shim)
        self.assertIn("--port 11434", shim)
        worker = (KIT / "units" / "sketchgen-worker.service").read_text()
        self.assertIn("OLLAMA_HOST_URL=http://127.0.0.1:11434", worker)


if __name__ == "__main__":
    unittest.main()
