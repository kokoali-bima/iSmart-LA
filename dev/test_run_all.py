#!/usr/bin/env python3
"""Tests for dev/run_all.py's own correctness -- specifically a blind spot
found while wiring it into CI, proven live before being fixed:

A module that fails to even PARSE makes every suite exit non-zero with no
"N/M passed" tally -- the exact same shape run_all.py already treats as a
harmless SKIP ("a missing optional dependency, say"). With a genuinely broken
lite_agent.py in place, 19 of 20 real suites SKIPped with "SyntaxError", the
one suite that never imports the module (test_cli_login.py, which only
touches tools/cli_login.py) reported a clean 15/15, and run_all.py printed
"TOTAL 15/15" at exit code 0 -- green, while the product could not run at
all. That is precisely the failure mode a Python-version CI matrix exists to
catch (this project shipped a real f-string fix after an external review
found lite_agent.py failed to import on 3.10/3.11), so the one case that
matters most could not be allowed to hide behind "SKIP".

Fixed with a compile check up front: `python3 -m py_compile <module>`, run
once before any suite, treated as a hard failure distinct from a per-suite
skip.

Every case here runs run_all.py as a real subprocess against an ISOLATED
scratch dev/ directory containing only a copy of run_all.py plus small fake
test_*.py suites this file controls -- never the real dev/ directory. Running
it against the real one would make the inner run_all.py discover and execute
THIS file again, which would do the same thing again: unbounded recursive
process spawning.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER_SRC = HERE / "run_all.py"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def isolated_project(module_text: str, fake_suites: dict[str, str]) -> Path:
    """<tmp>/proj/lite_agent.py + <tmp>/proj/dev/{run_all.py, fake suites}.
    Returns the project root. fake_suites maps filename -> full script text."""
    root = Path(tempfile.mkdtemp(prefix="isla_runall_proj_"))
    (root / "lite_agent.py").write_text(module_text, encoding="utf-8")
    dev = root / "dev"
    dev.mkdir()
    shutil.copy(RUNNER_SRC, dev / "run_all.py")
    for name, text in fake_suites.items():
        (dev / name).write_text(text, encoding="utf-8")
    return root


def run_all(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / "dev" / "run_all.py"), str(root / "lite_agent.py")],
        capture_output=True, text=True, cwd=str(root),
    )


FAKE_OK = '''import sys
print("PASS - a trivial check")
print("1/1 passed")
'''
BROKEN_MODULE = "def main(: -> None:\n    pass\n"          # a real SyntaxError
VALID_EMPTY_MODULE = "# syntactically valid, nothing useful in it\n"

# --- 1. a module that cannot even parse: hard failure, before any suite ----
root1 = isolated_project(BROKEN_MODULE, {"test_fake.py": FAKE_OK})
proc1 = run_all(root1)
out1 = proc1.stdout + proc1.stderr
shutil.rmtree(root1, ignore_errors=True)

check("a module that fails to compile makes run_all.py exit non-zero "
      "(THE bug: this used to exit 0)", proc1.returncode != 0)
check("...and it says so BEFORE running any suite, not after silently "
      "skipping all of them", "does not even compile" in out1)
check("...and shows the real SyntaxError, not a generic message",
      "SyntaxError" in out1)
check("...the fake suite never actually ran",
      "1/1 passed" not in out1 and "a trivial check" not in out1)
check("...and it does NOT print a misleadingly clean tally the way it did "
      "before the fix (TOTAL 15/15 at exit 0)",
      "TOTAL" not in out1)

# --- 2. a syntactically valid module passes the gate and suites DO run ----
root2 = isolated_project(VALID_EMPTY_MODULE, {"test_fake.py": FAKE_OK})
proc2 = run_all(root2)
out2 = proc2.stdout + proc2.stderr
shutil.rmtree(root2, ignore_errors=True)

check("a syntactically VALID (if useless) module passes the compile gate "
      "-- it checks syntax only, not whether suites find what they need",
      "does not even compile" not in out2)
check("...and the runner proceeds to actually run the suite",
      "1/1" in out2 and "TOTAL" in out2)
check("...reporting success when the (fake) suite passes", proc2.returncode == 0)

# --- 3. a suite that fails still fails the run, same as always ------------
FAKE_FAIL = 'print("FAIL - something real broke")\nprint("0/1 passed")\n'
root3 = isolated_project(VALID_EMPTY_MODULE, {"test_fake.py": FAKE_FAIL})
proc3 = run_all(root3)
shutil.rmtree(root3, ignore_errors=True)
check("a genuinely failing suite still fails the overall run "
      "(the compile-gate fix did not loosen this)", proc3.returncode != 0)

# --- 4. the real module in this repo passes the gate ----------------------
real_src = HERE.parent / "lite_agent.py"
if real_src.exists():
    root4 = isolated_project(real_src.read_text(encoding="utf-8"),
                             {"test_fake.py": FAKE_OK})
    proc4 = run_all(root4)
    out4 = proc4.stdout + proc4.stderr
    shutil.rmtree(root4, ignore_errors=True)
    check("the real lite_agent.py in this repo passes the compile gate",
          "does not even compile" not in out4)
else:
    print("SKIP - real lite_agent.py not found next to dev/ (unexpected layout)")

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
