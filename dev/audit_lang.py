#!/usr/bin/env python3
"""Audit sweep: find every reply_text/edit_message_text/answer call in
lite_agent.py and report whether its FIRST argument is a `_t(lang, ...)`
call (a translated string) or something else (a raw literal, an f-string
built from raw literals, a variable, etc). This is a heuristic scan meant to
surface candidates a human should look at -- not everything flagged is
actually a bug (log lines, prompts sent TO the model, HELP_TEXT/
_HELP_CREDITS, and functions like `_wizard_text(lang)` / `_srv_prompt(step,
lang)` that already take `lang` as an argument are legitimately exempt), but
everything that's a REAL bug -- a literal string with no `_t()` at all --
will show up here.

Usage: python3 dev/audit_lang.py lite_agent.py
Grew out of the v0.2b.6-9 /lang migration series; became v0.2b.10 when a
requested re-check run against it found six real gaps the per-command
migration passes had walked past. Safe to re-run any time /lang coverage
needs re-verifying after future changes -- it's read-only, makes no edits.
"""
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").split("\n")

CALL_RE = re.compile(r'\.(reply_text|edit_message_text|answer)\(')

flagged = []
current_func = None
FUNC_RE = re.compile(r'^(async def|def) (\w+)')

for i, line in enumerate(lines, 1):
    m = FUNC_RE.match(line)
    if m:
        current_func = m.group(2)

    if not CALL_RE.search(line):
        continue
    # Strip the method call prefix to see what immediately follows the paren.
    call_start = CALL_RE.search(line)
    after = line[call_start.end():].lstrip()
    if not after:
        after = lines[i].strip() if i < len(lines) else ""

    # Skip calls with no string argument at all (e.g. answer() with nothing,
    # or a kwarg-only call continuing from a prior line's variable).
    if after.startswith(")"):
        continue
    # Translated via _t(...)
    if after.startswith("_t("):
        continue
    # A pre-built variable (msg, text, header, prompt, etc) -- likely already
    # constructed with _t() earlier; flag separately as "check manually".
    if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*[,)]', after):
        flagged.append((i, current_func, "variable", after[:60]))
        continue
    # A raw literal string or f-string -- the real candidates.
    if after.startswith('"') or after.startswith("f\"") or after.startswith("'") or after.startswith("f'"):
        flagged.append((i, current_func, "LITERAL", after[:70]))
        continue
    flagged.append((i, current_func, "other", after[:60]))

print(f"{len(flagged)} calls flagged for review in {path.name}\n")
for lineno, func, kind, snippet in flagged:
    print(f"  L{lineno:5d}  [{kind:8s}]  {func or '?':28s}  {snippet}")
