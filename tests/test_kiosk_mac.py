"""kiosk-mac/ — the wall's kit, as far as a laptop can hold it (local-gallery.md §3).

The kit runs on a Mac under launchd, so most of it is proven at a Mac, not here. What the suite
can hold: every script parses, every plist is a plist whose label is its file name, get.sh
fetches every file the Mac needs, install.sh refuses a flag it does not know before it touches
anything, launch-kiosk.sh takes its URL from the file when there is one, serve-gallery.py serves
loopback only and follows the release link, and sync-local.sh — run against a fake ssh whose
hosts are directories — copies, swaps, prunes and refuses as it says.
"""

import http.client
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT = REPO_ROOT / "kiosk-mac"
SCRIPTS = ["get.sh", "install.sh", "prep.sh", "probe.sh", "system.sh", "launch-kiosk.sh",
           "kiosk-watchdog.sh", "kiosk-status", "serve-gallery.sh", "sync-local.sh"]
#: Never fetched by get.sh: the laptop's sync, and get.sh itself, which is the one line typed.
NOT_FETCHED = {"sync-local.sh", "get.sh"}


class KitTextTests(unittest.TestCase):

    def test_every_script_parses(self):
        for name in SCRIPTS:
            with self.subTest(script=name):
                subprocess.run(["bash", "-n", str(KIT / name)], check=True)

    def test_the_server_compiles(self):
        subprocess.run([sys.executable, "-m", "py_compile", str(KIT / "serve-gallery.py")],
                       check=True)

    def test_every_plist_is_named_for_its_label_and_templated(self):
        for path in sorted(KIT.glob("*.plist")):
            with self.subTest(plist=path.name):
                data = plistlib.loads(path.read_bytes())
                self.assertEqual(path.stem, data["Label"])
                self.assertTrue(all(arg.startswith("/Users/USER/") or arg == "/bin/bash"
                                    for arg in data["ProgramArguments"]))

    def test_get_fetches_every_file_the_mac_needs(self):
        text = (KIT / "get.sh").read_text()
        block = text[text.index('FILES="') + 7:]
        listed = set(block[:block.index('"')].split())
        on_disk = {p.name for p in KIT.iterdir() if p.is_file()} - NOT_FETCHED
        self.assertEqual(on_disk, listed)

    def test_install_refuses_an_unknown_flag_first(self):
        done = subprocess.run(["bash", str(KIT / "install.sh"), "--lcoal"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(3, done.returncode)
        self.assertIn("unknown option --lcoal", done.stdout)

    def test_install_checks_the_command_line_tools_before_the_gallery_agent(self):
        text = (KIT / "install.sh").read_text()
        self.assertLess(text.index("xcode-select -p"), text.index('AGENTS="$AGENTS $GALLERY_AGENT"'))

    def test_the_server_script_checks_the_tools_before_python(self):
        text = (KIT / "serve-gallery.sh").read_text()
        self.assertLess(text.index("xcode-select -p"), text.index("exec /usr/bin/python3"))


class LaunchUrlTests(unittest.TestCase):
    """The URL block of launch-kiosk.sh, run as the script runs it."""

    def url(self, home: Path) -> str:
        text = (KIT / "launch-kiosk.sh").read_text()
        block = text[text.index('URL="'):text.index("PROFILE=")]
        done = subprocess.run(["bash", "-c", block + 'printf %s "$URL"'], capture_output=True,
                              text=True, env={**os.environ, "HOME": str(home)}, check=True)
        return done.stdout

    def test_pages_by_default_and_the_file_when_there_is_one(self):
        home = Path(tempfile.mkdtemp(prefix="kiosk-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        self.assertTrue(self.url(home).startswith("https://profcarroll.github.io/"))
        (home / "Library" / "sketchgen-kiosk").mkdir(parents=True)
        (home / "Library" / "sketchgen-kiosk" / "url").write_text(
            "http://127.0.0.1:8090/kiosk.html?unattended=1&site=d12-mini \nignored\n")
        self.assertEqual("http://127.0.0.1:8090/kiosk.html?unattended=1&site=d12-mini",
                         self.url(home))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServeGalleryTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="kiosk-serve-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for release, build in (("a", "aaa"), ("b", "bbb")):
            d = self.root / "releases" / release
            d.mkdir(parents=True)
            (d / "kiosk.json").write_text('{"build": "%s"}' % build)
            (d / "kiosk.html").write_text("<!doctype html>")
        (self.root / "gallery").symlink_to("releases/a")
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(KIT / "serve-gallery.py"), "--port", str(self.port),
             "--directory", str(self.root / "gallery")],
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        if not self.proc.stderr.closed:
            self.stderr = self.proc.stderr.read()
            self.proc.stderr.close()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()

    def test_manifests_are_no_store_and_pages_are_not(self):
        status, headers, body = self.get("/kiosk.json")
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers.get("Cache-Control"))
        self.assertIn("aaa", body)
        status, headers, _ = self.get("/kiosk.html")
        self.assertEqual(200, status)
        self.assertNotIn("Cache-Control", headers)
        self.assertEqual(404, self.get("/nothing.json")[0])

    def test_a_swapped_link_is_served_on_the_next_request(self):
        self.assertIn("aaa", self.get("/kiosk.json")[2])
        tmp = self.root / "gallery.new"
        tmp.symlink_to("releases/b")
        os.replace(tmp, self.root / "gallery")
        self.assertIn("bbb", self.get("/kiosk.json")[2])

    def test_no_access_log(self):
        for _ in range(5):
            self.get("/kiosk.json")
        self.stop()
        lines = [line for line in self.stderr.splitlines() if "GET" in line]
        self.assertEqual([], lines)

    def test_it_will_not_bind_anything_but_loopback(self):
        done = subprocess.run([sys.executable, str(KIT / "serve-gallery.py"), "--bind", "0.0.0.0",
                               "--port", str(free_port())], capture_output=True, text=True,
                              timeout=10)
        self.assertEqual(3, done.returncode)
        self.assertIn("not loopback", done.stderr)


FAKE_SSH = """#!/bin/bash
# ssh for the suite: HOST is a directory under $FAKE_HOSTS, and the command runs there as HOME.
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac; done
host=$1; shift
home="$FAKE_HOSTS/$host"
[ -d "$home" ] || { echo "ssh: no host $host" >&2; exit 255; }
cd "$home" && HOME="$home" exec bash -c "$*"
"""

FAKE_DATE = """#!/bin/bash
# date for the suite: a new second at every call, so two syncs in one test are two releases.
n=$(cat "$FAKE_HOSTS/.clock" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$FAKE_HOSTS/.clock"
printf '20260928T2200%02dZ\\n' "$n"
"""


class SyncLocalTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kiosk-sync-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hosts = self.tmp / "hosts"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in (("ssh", FAKE_SSH), ("date", FAKE_DATE),
                           ("xcode-select", "#!/bin/bash\necho /Library/Developer/CommandLineTools\n")):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.node = self.hosts / "node"
        self.render = self.node / "sketchgen-local" / "sld-gpu"
        self.write_render("one")
        self.mac = self.hosts / "mac"
        self.kiosk = self.mac / "Library" / "sketchgen-kiosk"
        self.kiosk.mkdir(parents=True)
        (self.kiosk / "serve-gallery.py").write_text("# installed\n")

    def write_render(self, build):
        (self.render / "e" / "1").mkdir(parents=True, exist_ok=True)
        (self.render / ".sketchgen-local").write_text('{"origin": "sld-gpu"}\n')
        (self.render / "kiosk.json").write_text('{"build": "%s", "entries": []}\n' % build)
        (self.render / "e" / "1" / "index.html").write_text(build)

    def sync(self, *extra, source="node:sketchgen-local/sld-gpu", path=None):
        env = {**os.environ, "FAKE_HOSTS": str(self.hosts),
               "PATH": f"{self.bin}:{path or os.environ['PATH']}"}
        return subprocess.run(["bash", str(KIT / "sync-local.sh"), source, "mac", *extra],
                              capture_output=True, text=True, env=env, timeout=60)

    def releases(self):
        return sorted(p.name for p in (self.kiosk / "releases").iterdir())

    def test_a_sync_lands_a_release_and_points_the_link_at_it(self):
        done = self.sync()
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        link = self.kiosk / "gallery"
        self.assertTrue(link.is_symlink())
        self.assertEqual("releases/20260928T220001Z", os.readlink(link))
        self.assertEqual("one", (link / "e" / "1" / "index.html").read_text())
        self.assertIn('"build": "one"', done.stdout)

    def test_the_next_sync_swaps_and_the_oldest_goes(self):
        for build in ("one", "two", "three"):
            self.write_render(build)
            done = self.sync("--keep", "2")
            self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(["20260928T220002Z", "20260928T220003Z"], self.releases())
        self.assertEqual("three", (self.kiosk / "gallery" / "e" / "1" / "index.html").read_text())

    def test_a_source_that_is_not_a_local_render_is_refused(self):
        (self.render / ".sketchgen-local").unlink()
        done = self.sync()
        self.assertEqual(3, done.returncode)
        self.assertIn("not a local render", done.stdout)
        self.assertFalse((self.kiosk / "releases").exists())

    def test_a_real_gallery_directory_is_refused(self):
        (self.kiosk / "gallery").mkdir()
        done = self.sync()
        self.assertEqual(3, done.returncode)
        self.assertIn("real directory", done.stdout)
        self.assertFalse((self.kiosk / "releases").exists())

    def test_a_mac_without_the_kit_installed_for_local_is_refused(self):
        (self.kiosk / "serve-gallery.py").unlink()
        done = self.sync()
        self.assertEqual(3, done.returncode)
        self.assertIn("not installed with --local", done.stdout)

    def test_a_mac_without_the_command_line_tools_is_refused(self):
        (self.bin / "xcode-select").write_text("#!/bin/bash\nexit 2\n")
        done = self.sync()
        self.assertEqual(3, done.returncode)
        self.assertIn("no Command Line Tools", done.stdout)

    @unittest.skipIf(os.geteuid() == 0, "root reads an unreadable file")
    def test_a_copy_that_fails_leaves_the_served_release_alone(self):
        self.assertEqual(0, self.sync().returncode)
        secret = self.render / "e" / "1" / "unreadable.js"
        secret.write_text("x")
        secret.chmod(0)
        self.addCleanup(secret.chmod, 0o644)
        done = self.sync()
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("did not finish", done.stdout)
        self.assertEqual("releases/20260928T220001Z", os.readlink(self.kiosk / "gallery"))
        self.assertEqual(["20260928T220001Z"], self.releases())

    def test_usage(self):
        done = self.sync(source="no-colon-here")
        self.assertEqual(3, done.returncode)
        self.assertIn("NODE:DIR MAC", done.stdout)


if __name__ == "__main__":
    unittest.main()
