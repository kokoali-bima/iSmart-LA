#!/usr/bin/env python3
"""Guards the minimum Python version this project claims to support.

An external review flagged a real one: `lite_agent.py` contained

    f"{'\\u2705 ' if a == effective else ''}{a}"

a backslash escape INSIDE an f-string expression. That only became legal in
Python 3.12 (PEP 701). On 3.10 and 3.11 it is a SyntaxError at import time --
so the module would not load at all, on exactly the versions `install.sh`
advertises ("python3 (3.10+)") and exactly what Debian 12 (3.11) and Ubuntu
22.04 (3.10) ship. Not a degraded feature: nothing runs, and /update's own
compile-check would have refused the deploy too.

Why this test exists in this shape: the bug is invisible from any machine new
enough to run the code. Every interpreter reachable from this project when the
fix was made was 3.12 or newer (dev box 3.14, bot host 3.12.3, Proxmox node
3.13.5), so it could not be reproduced by simply running it -- and
`ast.parse(..., feature_version=(3, 11))` does NOT help, because feature_version
does not emulate the older f-string tokenizer. It happily accepts the broken
line.

So instead of asking an interpreter, this walks the AST and inspects the source
text of each f-string expression directly. That works identically on every
version and needs no matrix, no containers, and no CI to be useful today.
"""
import ast
import pathlib
import re
import sys

SRC = pathlib.Path(sys.argv[1]).resolve()
ROOT = SRC.parent

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def backslash_in_fstring_exprs(source: str, filename: str) -> list[str]:
    """-> ["file:line: <expr>", ...] for every f-string expression whose own
    source text contains a backslash. Uses the parsed AST rather than a regex
    over the whole line, so a backslash sitting harmlessly in the LITERAL part
    of the same f-string is correctly ignored."""
    hits = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            seg = ast.get_source_segment(source, part.value)
            if seg and "\\" in seg:
                hits.append(f"{filename}:{part.value.lineno}: {seg.strip()}")
    return hits


# --- 1. the detector itself must actually detect ---------------------------
BAD = '''x = f"{'\\u2705 ' if a == b else ''}{a}"'''
GOOD = '''tick = "\\u2705 "\nx = f"{tick if a == b else ''}{a}"'''
LITERAL_ONLY = '''x = f"line\\n{a}"'''   # backslash in the LITERAL half: fine

check("detector flags a backslash inside an f-string expression (THE bug)",
      len(backslash_in_fstring_exprs(BAD, "<bad>")) == 1)
check("detector passes the fixed form (literal bound to a name first)",
      backslash_in_fstring_exprs(GOOD, "<good>") == [])
check("detector does NOT false-positive on a backslash in the literal part "
      "(only the expression half is restricted before 3.12)",
      backslash_in_fstring_exprs(LITERAL_ONLY, "<lit>") == [])

# --- 2. the real sources must be clean -------------------------------------
targets = [SRC] + sorted((ROOT / "tools").glob("*.py"))
all_hits = []
for f in targets:
    if not f.exists():
        continue
    all_hits += backslash_in_fstring_exprs(f.read_text(encoding="utf-8"), f.name)

check(f"no backslash-in-f-string-expression anywhere in {len(targets)} source file(s) "
      f"-- would break every Python below 3.12",
      not all_hits)
if all_hits:
    for h in all_hits:
        print("      ->", h)

# --- 3. what install.sh advertises must match what the code needs ----------
installer = ROOT / "install.sh"
if installer.exists():
    text = installer.read_text(encoding="utf-8")
    m = re.search(r"python3 \((\d+)\.(\d+)\+\)", text)
    check("install.sh states a minimum Python version explicitly", m is not None)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        # Nothing in this project needs 3.12: the only 3.12-only construct was
        # the f-string above. Everything else in use (str.removeprefix, builtin
        # generics, `from __future__ import annotations`) is 3.9-or-older.
        check(f"the advertised minimum ({major}.{minor}+) is one this code can "
              f"actually meet now that the f-string is fixed",
              (major, minor) <= (3, 12) and not all_hits)
        check("...and it is not silently newer than the README/installer promise",
              (major, minor) >= (3, 9))
else:
    print("SKIP - install.sh not found next to the module")

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
