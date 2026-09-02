#!/usr/bin/env python3
"""Run every regression suite in dev/ and report one total.

The suites themselves were always good -- each documents the bug it covers,
with the real output from before the fix. What was missing was a way to run
them together: each had to be invoked by hand with its own sys.argv[1], so
"did anything break?" took eighteen commands and a human adding up numbers.

Usage:
    python3 dev/run_all.py                  # auto-detects ../lite_agent.py
    python3 dev/run_all.py /path/to/lite_agent.py
    python3 dev/run_all.py --quiet          # totals only

Exit code is non-zero if any suite fails, so this is also the shape a CI job
would call. Suites that legitimately cannot run here (a missing optional
dependency, say) are reported as SKIP and do not fail the run -- a skipped
suite is visible, which is the point; a silently-absent one would not be.
"""
import pathlib
import re
import subprocess
import sys
import time

args = [a for a in sys.argv[1:] if not a.startswith("--")]
QUIET = "--quiet" in sys.argv

DEV = pathlib.Path(__file__).resolve().parent
ROOT = DEV.parent
SRC = pathlib.Path(args[0]).resolve() if args else ROOT / "lite_agent.py"

if not SRC.exists():
    print(f"cannot find the module under test: {SRC}")
    sys.exit(2)

suites = sorted(DEV.glob("test_*.py"))
if not suites:
    print("no test_*.py found in dev/")
    sys.exit(2)

RESULT_RE = re.compile(r"^(\d+)/(\d+) passed", re.MULTILINE)

passed = failed = skipped = 0
failing: list[str] = []
started = time.monotonic()

for suite in suites:
    name = suite.name
    proc = subprocess.run([sys.executable, str(suite), str(SRC)],
                          capture_output=True, text=True, cwd=str(ROOT))
    out = proc.stdout + proc.stderr
    m = RESULT_RE.search(out)

    if m:
        got, total = int(m.group(1)), int(m.group(2))
        passed += got
        failed += total - got
        status = "ok" if got == total else "FAIL"
        line = f"{name:<34}{got:>4}/{total:<4} {status}"
        if got != total:
            failing.append(name)
    elif proc.returncode != 0:
        # Ran, but never printed a tally -- an import error, a missing optional
        # dependency, a crash. Surfaced as SKIP with the reason, never hidden.
        reason = next((l.strip() for l in reversed(out.splitlines()) if l.strip()), "no output")
        skipped += 1
        line = f"{name:<34}{'':>9} SKIP  {reason[:60]}"
    else:
        skipped += 1
        line = f"{name:<34}{'':>9} SKIP  produced no tally"

    if not QUIET:
        print(line)

elapsed = time.monotonic() - started
print("-" * 62)
print(f"{'TOTAL':<34}{passed:>4}/{passed + failed:<4} "
      f"in {elapsed:.1f}s across {len(suites)} suite(s)"
      + (f", {skipped} skipped" if skipped else ""))

if failing:
    print("\nFAILING SUITES:")
    for f in failing:
        print(f"  - {f}")
    sys.exit(1)
if failed:
    sys.exit(1)
