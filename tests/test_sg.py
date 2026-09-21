"""bin/sg: the laptop-side wrapper that runs one sketchgen command on the node.

The thing it exists for is quoting. Before it, the recipe was a shell function
that pasted "$*" into an ssh command line, and a prompt had to be quoted twice.
Here every argument is quoted once by printf %q and arrives on the node as the
same argument. The test replaces ssh with a script that prints what it was
handed and echoes stdin, so nothing leaves this machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SG = REPO_ROOT / "bin" / "sg"

FAKE_SSH = """#!/usr/bin/env bash
# what ssh was handed, one argument per line, then stdin verbatim
for a in "$@"; do printf '%s\\n' "$a"; done
printf -- '--stdin--\\n'
cat
exit 3
"""


@unittest.skipUnless(shutil.which("bash"), "bin/sg is a bash script")
class SgTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-sg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ssh = self.tmp / "ssh"
        self.ssh.write_text(FAKE_SSH, encoding="utf-8")
        self.ssh.chmod(0o755)

    def sg(self, *args, stdin="", env=None):
        environment = dict(os.environ, SKETCHGEN_SSH=str(self.ssh))
        environment.pop("SKETCHGEN_NODE", None)
        environment.pop("SKETCHGEN_REMOTE", None)
        environment.update(env or {})
        return subprocess.run(
            [str(SG), *args], capture_output=True, text=True, input=stdin,
            env=environment, check=False,
        )

    def remote_command(self, result):
        lines = result.stdout.split("--stdin--\n", 1)[0].splitlines()
        return lines

    def test_a_prompt_with_spaces_and_quotes_arrives_as_one_argument(self):
        prompt = "a tide of 'slow' lines, $5 each \"quoted\""
        result = self.sg("paid", "start", "--as", "claude-sonnet-5", "--by",
                         "octocat", "--prompt", prompt)
        lines = self.remote_command(result)
        self.assertEqual(lines[:3], ["-o", "BatchMode=yes", "sld-cloud"])
        remote = lines[3]
        # the remote line, evaluated by the node's bash, yields the same argv
        argv = subprocess.run(
            ["bash", "-c", 'printf "%s\\n" ' + remote.split("bin/sketchgen", 1)[1]],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertEqual(argv[argv.index("--prompt") + 1], prompt)
        self.assertEqual(argv[-2:], ["--db", os.path.expandvars("$HOME/sketchgen/sketchgen.db")])
        self.assertIn("$HOME/sketchgen/.venv/bin/python3 $HOME/sketchgen/app/bin/sketchgen", remote)

    def test_stdin_and_the_exit_status_pass_through(self):
        result = self.sg("paid", "import", "-", stdin='{"packet": "x"}\n')
        self.assertTrue(result.stdout.endswith('--stdin--\n{"packet": "x"}\n'))
        self.assertEqual(result.returncode, 3)

    def test_the_node_and_checkout_are_settings(self):
        result = self.sg("paid", "status", env={"SKETCHGEN_NODE": "other-node",
                                                "SKETCHGEN_REMOTE": "/srv/sg"})
        lines = self.remote_command(result)
        self.assertEqual(lines[2], "other-node")
        self.assertTrue(lines[3].startswith("/srv/sg/.venv/bin/python3 /srv/sg/app/bin/sketchgen paid status"))

    def test_no_arguments_is_refused(self):
        result = self.sg()
        self.assertEqual(result.returncode, 3)
        self.assertIn("usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
