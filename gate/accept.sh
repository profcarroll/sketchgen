#!/bin/bash
# accept.sh -- the ACCEPT harness for the sketch gate (packet 1.2).
#
# For every fixtures/<name>/ directory it runs sketch_gate.py --json, compares
# the exit code and the six fixed checks against fixtures/expected.json, then
# runs the assertions listed in expected.json["assertions_expected"] for that
# fixture and compares their pass/fail.  One line per fixture, PASS or
# MISMATCH with what differed, a summary, and a non-zero exit on any mismatch.
#
#   ./accept.sh [fixtures_dir] [artefact_dir]
#
# A fixture's expected "checks" object need not name every check: only the keys
# it lists are compared.  bad-frame-budget uses that, because the budget stops
# its run before the two checks read at the end of the idle window are read.
# An optional "ghost" object is compared the same way, key by key, against
# report.json's own: ghost-echo uses it to say that the gate played the
# sketch's own script and played all of it (auto-mouse.md section 5).
#
# An optional "assertion_detail" object names, per assertion word, a substring
# that word's detail line must contain.  The three image fixtures use it: they
# miss or pass loads(image) for three different reasons, and the reason is what
# the next attempt reads (media-assertion.md section 3.3).
#
# fixtures_dir defaults to ./fixtures next to this script; artefact_dir
# defaults to a fresh directory under $TMPDIR so the fixtures are not written
# into.  Needs the venv with playwright on PATH already:
#     . ~/sketchgen/.venv/bin/activate
#
# Timestamps printed here are UTC.

set -u

here="$(cd "$(dirname "$0")" && pwd)"
gate="$here/sketch_gate.py"
fixtures="${1:-$here/fixtures}"
outroot="${2:-$(mktemp -d "${TMPDIR:-/tmp}/sketch-gate-accept.XXXXXX")}"

if [ ! -f "$gate" ]; then
  echo "accept: refused -- no sketch_gate.py next to this script" >&2
  exit 3
fi
if [ ! -d "$fixtures" ]; then
  echo "accept: refused -- no fixtures directory at $fixtures" >&2
  exit 3
fi
if [ ! -f "$fixtures/expected.json" ]; then
  echo "accept: refused -- no expected.json in $fixtures" >&2
  exit 3
fi

echo "accept: gate      $gate"
echo "accept: fixtures  $fixtures"
echo "accept: artefacts $outroot"
echo "accept: started   $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC)"
echo

GATE="$gate" FIXTURES="$fixtures" OUTROOT="$outroot" python3 - <<'PY'
import json, os, pathlib, subprocess, sys, time

gate = os.environ["GATE"]
fixtures = pathlib.Path(os.environ["FIXTURES"])
outroot = pathlib.Path(os.environ["OUTROOT"])
expected = json.loads((fixtures / "expected.json").read_text())
asserts_expected = expected.get("assertions_expected", {})

CHECKS = ["console_clean", "is_looping", "frame_advancing",
          "sound_lib_ok", "audio_context_running", "frame_budget"]

names = sorted(p.name for p in fixtures.iterdir()
               if p.is_dir() and (p / "index.html").is_file())
if not names:
    print("accept: refused -- no fixture directories in %s" % fixtures)
    sys.exit(3)

bad = 0
t_all = time.time()
for name in names:
    want = expected.get(name)
    problems = []
    if want is None:
        print("MISMATCH  %-24s no entry in expected.json" % name)
        bad += 1
        continue

    def run(extra, out):
        t0 = time.time()
        p = subprocess.run([sys.executable, gate, str(fixtures / name), "--json",
                            "--out", str(out)] + extra,
                           capture_output=True, text=True)
        try:
            report = json.loads(p.stdout)
        except ValueError:
            report = None
        return p, report, time.time() - t0

    # Run 1: fixed checks only.  expected.json's "exit" is the verdict of a
    # plain run, so the assertions must not be in it.
    p, report, took = run([], outroot / name)
    if p.returncode == 3 or report is None:
        print("MISMATCH  %-24s gate refused or printed no JSON (exit %d): %s"
              % (name, p.returncode, p.stderr.strip()[:200]))
        bad += 1
        continue

    if p.returncode != want.get("exit"):
        problems.append("exit %s, expected %s" % (p.returncode, want.get("exit")))
    got_checks = report.get("checks", {})
    for key in CHECKS:
        if key not in want.get("checks", {}):
            continue
        if got_checks.get(key) != want["checks"][key]:
            problems.append("%s %r, expected %r"
                            % (key, got_checks.get(key), want["checks"][key]))

    # The ghost window, for the one fixture that is about it. Only the keys
    # the expectation lists are compared, as for the checks: "ms" is the
    # script's own last millisecond and nobody should have to restate it here.
    want_ghost = want.get("ghost")
    if want_ghost:
        got_ghost = report.get("ghost")
        if not isinstance(got_ghost, dict):
            problems.append("ghost %r, expected an object" % (got_ghost,))
        else:
            for key in sorted(want_ghost):
                if got_ghost.get(key) != want_ghost[key]:
                    problems.append("ghost.%s %r, expected %r"
                                    % (key, got_ghost.get(key), want_ghost[key]))

    # Run 2: the assertions the planner would have chosen for this fixture.
    want_asserts = asserts_expected.get(name, {})
    if want_asserts:
        extra = []
        for word in sorted(want_asserts):
            extra += ["--assert", word]
        p2, report2, took2 = run(extra, outroot / (name + "-assert"))
        took += took2
        if report2 is None:
            problems.append("assertion run printed no JSON (exit %d): %s"
                            % (p2.returncode, p2.stderr.strip()[:120]))
        else:
            got_asserts = report2.get("assertions", {})
            for word, ok in sorted(want_asserts.items()):
                got = got_asserts.get(word)
                if got is None:
                    problems.append("assertion %s was not evaluated" % word)
                elif bool(got.get("pass")) != bool(ok):
                    problems.append("%s %s, expected %s (%s)"
                                    % (word, got.get("pass"), ok, got.get("detail")))
            # And, for the fixtures that are about WHICH way an assertion went,
            # a substring its detail has to carry (media-assertion.md §3.3,
            # 2026-09-22). The three image fixtures miss or pass loads(image)
            # for three different reasons, and a run that gave the right
            # verdict with the wrong sentence would hand the next attempt a
            # network fault to chase that is not there.
            for word, fragment in sorted((want.get("assertion_detail") or {}).items()):
                detail = ((got_asserts.get(word) or {}).get("detail") or "")
                if fragment not in detail:
                    problems.append("%s detail does not mention %r: %s"
                                    % (word, fragment, detail or "(not evaluated)"))
    elif want.get("assertion_detail"):
        problems.append("assertion_detail is set but no assertions_expected "
                        "entry runs the assertions it is about")

    if problems:
        bad += 1
        print("MISMATCH  %-24s %.1fs  %s" % (name, took, "; ".join(problems)))
    else:
        rate = (report.get("timings") or {}).get("ms_per_frame")
        # The ghost window is on the PASS line because it is the one thing in
        # the report nothing else here would ever mention: it fails no fixture,
        # so without this a run that quietly stopped playing scripts at all
        # would look exactly like a run that played every one of them.
        ghost = report.get("ghost") or {}
        print("PASS      %-24s %.1fs  exit %d%s%s%s"
              % (name, took, p.returncode,
                 "" if rate is None else ", %g ms/frame" % rate,
                 ", %d assertion(s)" % len(want_asserts) if want_asserts else "",
                 ", ghost %s %s/%s" % (ghost.get("script") or ghost.get("source"),
                                       ghost.get("played"), ghost.get("events"))
                 if ghost else ", no ghost"))

print()
print("accept: %d fixture(s), %d mismatch(es), %.1fs total"
      % (len(names), bad, time.time() - t_all))
sys.exit(1 if bad else 0)
PY
rc=$?

echo "accept: finished  $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC)"
exit $rc
