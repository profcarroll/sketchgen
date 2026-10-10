"""The ROCKNIX kit holds together without a device: the scripts parse, every unit
reads only variables node.env.example defines, the units point at the layout
install.sh builds, nothing but the gallery can start at boot, and the power keeper
follows the console's switch. The device itself is tested by running the kit."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT = REPO_ROOT / "rocknix"
sys.path.insert(0, str(REPO_ROOT))

from sketchgen import db  # noqa: E402

_spec = importlib.util.spec_from_file_location("rocknix_power", KIT / "power.py")
power = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(power)


class RocknixKitTests(unittest.TestCase):
    def test_the_scripts_parse(self):
        for script in ("install.sh", "publish-local.sh"):
            done = subprocess.run(["bash", "-n", str(KIT / script)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
        done = subprocess.run(["sh", "-n", str(KIT / "ports" / "Sketchgen Console.sh")],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_every_unit_variable_is_in_the_env_template(self):
        defined = set(re.findall(r"^([A-Z_]+)=", (KIT / "node.env.example").read_text(), re.M))
        for unit in sorted((KIT / "units").glob("*.service")):
            text = unit.read_text()
            used = set(re.findall(r"\$\{?([A-Z_]+)\}?", text))
            self.assertTrue(used <= defined, f"{unit.name} reads {used - defined}")
            self.assertIn("EnvironmentFile=/storage/sketchgen/node.env", text) if used else None

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

    def test_nothing_but_the_gallery_can_start_at_boot(self):
        units = sorted((KIT / "units").iterdir())
        for unit in units:
            if unit.name != "sketchgen-gallery.service":
                self.assertNotRegex(unit.read_text(), r"(?m)^\[Install\]", unit.name)
        for name in power.MODEL_UNITS + ("sketchgen-web.service", "sketchgen-power.service"):
            self.assertIn("PartOf=sketchgen.target", (KIT / "units" / name).read_text(), name)
        target = (KIT / "units" / "sketchgen.target").read_text()
        self.assertIn("Wants=sketchgen-web.service sketchgen-power.service", target)
        self.assertNotIn("llama", target.split("[Unit]")[1].split("Wants=")[1])

    def test_install_retires_the_units_an_earlier_kit_enabled(self):
        script = (KIT / "install.sh").read_text()
        self.assertNotRegex(script, r"systemctl enable[^\n]*sketchgen-(llama|shim|web|worker)")
        self.assertIn("disable --now", script)
        self.assertIn('"$APP/rocknix/ports/Sketchgen Console.sh"', script)

    def test_the_ports_entry_starts_the_target_and_marks_the_console_open(self):
        entry = (KIT / "ports" / "Sketchgen Console.sh").read_text()
        self.assertIn("systemctl start sketchgen.target", entry)
        self.assertIn(f"FLAG={power.CONSOLE_FLAG}", entry)
        self.assertIn("launch.sh http://127.0.0.1:8081/", entry)


class PowerKeeperTests(unittest.TestCase):
    """rocknix/power.py: the units follow the console's switch, and the console."""

    ALL = set(power.MODEL_UNITS)

    def test_on_starts_the_worker_once(self):
        self.assertEqual(power.decide("running", set(), True), power.START)
        self.assertEqual(power.decide("running", set(), False), power.START)
        self.assertEqual(power.decide("running", self.ALL, False), power.HOLD)

    def test_pausing_lets_the_worker_finish_its_attempt(self):
        self.assertEqual(power.decide("pausing", self.ALL, True), power.HOLD)
        self.assertEqual(power.decide("pausing", self.ALL, False), power.HOLD)

    def test_off_with_the_console_open_stops_the_model_only(self):
        self.assertEqual(power.decide("paused", self.ALL, True), power.STOP_MODEL)
        self.assertEqual(power.decide("paused", {"sketchgen-llama.service"}, True), power.STOP_MODEL)
        self.assertEqual(power.decide("paused", set(), True), power.HOLD)
        # pausing with no worker: nobody is left to finish anything or write paused
        self.assertEqual(power.decide("pausing", set(), True), power.HOLD)

    def test_off_with_the_console_closed_stops_everything(self):
        self.assertEqual(power.decide("paused", self.ALL, False), power.STOP_ALL)
        self.assertEqual(power.decide("paused", set(), False), power.STOP_ALL)
        self.assertEqual(power.decide("pausing", set(), False), power.STOP_ALL)

    def test_the_console_is_open_while_its_pid_lives(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "console"
            self.assertFalse(power.console_open(flag))
            flag.write_text(f"{os.getpid()}\n")
            self.assertTrue(power.console_open(flag))
            gone = subprocess.Popen([sys.executable, "-c", "pass"])
            gone.wait()
            flag.write_text(f"{gone.pid}\n")
            self.assertFalse(power.console_open(flag))
            flag.write_text("")
            self.assertFalse(power.console_open(flag))

    def test_the_switch_is_read_from_the_control_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sketchgen.db"
            db.init(path)
            conn = db.connect(path)
            try:
                db.set_control(conn, "paused", "bench first")
                self.assertEqual(power.generator_state(path), "paused")
                db.set_control(conn, "running", None)
                self.assertEqual(power.generator_state(path), "running")
            finally:
                conn.close()

    def test_a_pause_with_no_worker_is_settled_as_the_worker_would(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sketchgen.db"
            db.init(path)
            conn = db.connect(path)
            try:
                db.set_control(conn, "pausing", "stop")
                self.assertTrue(power.settle_pause(path))
                control = db.get_control(conn)
                self.assertEqual((control.state, control.reason), ("paused", "stop"))
                self.assertFalse(power.settle_pause(path))
                db.set_control(conn, "running", None)
                self.assertFalse(power.settle_pause(path))
                self.assertEqual(db.get_control(conn).state, "running")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
