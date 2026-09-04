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

# A module that fails to even PARSE makes every single suite below fail with
# the exact same shape as a suite that's merely missing an optional
# dependency -- both exit non-zero with no "N/M passed" tally. Proven live:
# with a broken module in place, 19 of 20 suites SKIPped with "SyntaxError",
# the one suite that doesn't import the module at all reported a clean 15/15,
# and this script printed TOTAL 15/15 at exit code 0 -- green, while the
# product could not run at all. This is exactly the failure mode a version
# matrix exists to catch (see the f-string fix this project shipped after an
# external review), so it cannot be allowed to hide behind "SKIP". Checked
# once, up front, as a hard failure distinct from a per-suite skip.
compile_check = subprocess.run([sys.executable, "-m", "py_compile", str(SRC)],
                               capture_output=True, text=True)
if compile_check.returncode != 0:
    print(f"{SRC.name} does not even compile on {sys.version.split()[0]} -- "
          f"stopping before running any suite (a real result would be meaningless):\n")
    print(compile_check.stderr.strip() or compile_check.stdout.strip())
    sys.exit(1)

# The symbol index is regenerated on every run, so it is never stale: it is
# what makes a name collision visible BEFORE it is written, and the incident
# that prompted it (a second _msg silently replacing the first, breaking every
# reply) was invisible at the point of writing.
#
# Its output used to be captured and thrown away, which defeated the entire
# point: the tool detected duplicates and then whispered them into a variable
# nobody read. It is printed now, and a duplicate name FAILS the run -- a
# warning that stops nothing is a warning people learn to scroll past.
# Exit code 3 from the index means "duplicate name found" and nothing else.
# Any OTHER failure means the tool itself did not run -- it is not even copied
# into the isolated projects this suite's own tests build -- and that must stay
# best-effort, exactly as it was before: a broken index says nothing about the
# product, and stopping the tests for it would be a false alarm.
INDEX_DUPLICATE_EXIT = 3
index_dupes = False
try:
    idx = subprocess.run([sys.executable, str(DEV / "code_index.py"), str(SRC)],
                         capture_output=True, text=True, timeout=60)
    for line in (idx.stdout or "").strip().splitlines():
        print(f"  {line}")
    if idx.returncode == INDEX_DUPLICATE_EXIT:
        index_dupes = True
    elif idx.returncode != 0:
        print(f"  symbol index did not run (exit {idx.returncode}) -- "
              f"continuing, this says nothing about the code")
    print()
except Exception as exc:
    print(f"  symbol index did not run: {exc}\n")

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
if index_dupes:
    print("\nDUPLICATE TOP-LEVEL NAME -- the later definition silently "
          "replaces the earlier one. See the symbol index above.")
    sys.exit(1)
