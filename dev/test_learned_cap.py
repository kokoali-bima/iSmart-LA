#!/usr/bin/env python3
"""Tests for LEARNED_MAX_FACTS becoming tunable, and visible.

Why it became tunable at all: /setchatscope let one deployment answer as
genuinely different things per room, and every one of those rooms draws on this
single 60-fact budget. Past the cap the OLDEST fact is dropped with nothing
said, so an infrastructure-heavy week can quietly evict a research room's facts.

Deliberately NOT solved by making the zone per-room -- `LEARN:` lines are
already refused from any group (_is_trusted_origin), so the useful shape is
"the operator teaches it once in a DM and every room benefits", and splitting
the zone would break exactly that. What was wrong was that the cap could not be
changed and could not be seen. So: tunable, and /learned reports the count
against it.

Two properties get most of the attention here, because both are the kind that
look fine until the day they matter:

1. **A bad value is refused, not obeyed.** 0 is NOT "off" the way
   TURN_TOKEN_CEILING's 0 is: this cap exists to stop unbounded growth, so
   treating 0 as "no limit" would make the setting do the opposite of what it
   reads like.

2. **The refusal can actually be logged.** The first cut of this parsed the
   value beside the constant, ~20 lines ABOVE where `logger` is created -- so
   the only path that calls logger.warning() would have died on a NameError, in
   precisely the case the guard exists for. Valid values imported fine, which
   is what makes it the dangerous kind of bug: it would have shipped green and
   broken only for the operator who typo'd their .env. The ordering is asserted
   below so it cannot come back.
"""
import ast
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile

SRC = pathlib.Path(sys.argv[1]).resolve()
source = SRC.read_text(encoding="utf-8")
tree = ast.parse(source, filename=str(SRC))

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


# --- 1. ordering: the guard must be able to report itself ------------------
def _assign_line(name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node.lineno
    return None

logger_line = _assign_line("logger")
check("logger is created at module level", logger_line is not None)

# Every module-level statement that mentions LEARNED_MAX_FACTS *and* logging.
cap_warn_lines = []
for node in tree.body:
    src = ast.unparse(node)
    if "LEARNED_MAX_FACTS" in src and "logger." in src:
        cap_warn_lines.append(node.lineno)
check("the cap's validation logs a rejection at all", bool(cap_warn_lines))
check("...and every such line comes AFTER logger exists "
      "(otherwise the guard NameErrors in exactly the case it is for)",
      bool(cap_warn_lines) and logger_line is not None
      and min(cap_warn_lines) > logger_line)


# --- 2. behaviour: load the module with different env values ---------------
scratch = pathlib.Path(tempfile.mkdtemp(prefix="isla_cap_"))
import atexit
atexit.register(shutil.rmtree, scratch, ignore_errors=True)

def load(value, tag):
    work = scratch / tag
    work.mkdir()
    shutil.copy(SRC, work / SRC.name)
    shutil.copytree(SRC.parent / "tools", work / "tools")
    home = work / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["ALLOWED_USER_IDS"] = "1"
    os.environ["ALLOWED_GROUP_IDS"] = ""
    if value is None:
        os.environ.pop("LEARNED_MAX_FACTS", None)
    else:
        os.environ["LEARNED_MAX_FACTS"] = value
    spec = importlib.util.spec_from_file_location(tag, work / SRC.name)
    m = importlib.util.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m

DEFAULT = 60
check("unset keeps the documented default", load(None, "unset").LEARNED_MAX_FACTS == DEFAULT)
check("an empty value keeps the default too (a commented-out line that got uncommented)",
      load("", "empty").LEARNED_MAX_FACTS == DEFAULT)
check("a real value is honoured", load("120", "raised").LEARNED_MAX_FACTS == 120)
check("surrounding whitespace does not defeat it", load("  25  ", "spaced").LEARNED_MAX_FACTS == 25)

# The ones that must NOT be obeyed.
check("0 is refused rather than treated as unlimited -- this cap is the thing "
      "standing between the briefs and unbounded growth",
      load("0", "zero").LEARNED_MAX_FACTS == DEFAULT)
check("a negative is refused", load("-5", "negative").LEARNED_MAX_FACTS == DEFAULT)
check("a non-number is refused rather than crashing the import",
      load("lots", "garbage").LEARNED_MAX_FACTS == DEFAULT)
check("a float is refused rather than silently truncated",
      load("12.5", "float").LEARNED_MAX_FACTS == DEFAULT)

# --- 3. the cap is actually enforced at the value given --------------------
m = load("3", "enforced")
brief = pathlib.Path(m.SYSTEM_PROMPT_FILE)
brief.write_text("You are an assistant for ACME.\n", encoding="utf-8")
pathlib.Path(m.GEMINI_PROMPT_FILE).write_text("You are an assistant for ACME.\n",
                                              encoding="utf-8")
m.append_learned([f"fact number {i} about this environment" for i in range(1, 6)])
kept = m._learned_facts()
check("the zone is trimmed to the configured cap, not the built-in 60",
      len(kept) == 3)
check("...and it is the OLDEST that were dropped, as documented",
      "fact number 5" in kept[-1] and not any("fact number 1" in k for k in kept))

# --- 3b. the token estimate ------------------------------------------------
# The count is a proxy; this is the thing actually paid, and paid again every
# time a chat opens a conversation. A fact may be 10 or 400 characters, so the
# two genuinely come apart -- which is the whole reason for showing both.
#
# Guarded the same way as the block at the top, and for the same reason: this
# section calls something the pre-change source does not have, and without the
# guard the suite dies here instead of reporting -- in the one run where being
# readable is the entire point. That mistake has now been made three times in
# this branch, which is why the check is inline rather than remembered.
_probe = load("60", "probe_tokens")
if not hasattr(_probe, "_learned_zone_tokens"):
    check("the module provides _learned_zone_tokens", False)
    _failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(_failed)}/{len(results)} passed")
    print("FAILED:", _failed)
    print("the token estimate is absent from this source -- the checks below "
          "it need it, so they cannot be evaluated")
    sys.exit(1)
check("the module provides _learned_zone_tokens", True)

empty = load("60", "empty_zone")
pathlib.Path(empty.SYSTEM_PROMPT_FILE).write_text("You are an assistant.\n", encoding="utf-8")
pathlib.Path(empty.GEMINI_PROMPT_FILE).write_text("You are an assistant.\n", encoding="utf-8")
check("an empty learned zone estimates zero, not a stray header cost",
      empty._learned_zone_tokens() == 0)

sized = load("60", "sized")
for p in (pathlib.Path(sized.SYSTEM_PROMPT_FILE), pathlib.Path(sized.GEMINI_PROMPT_FILE)):
    p.write_text("You are an assistant.\n", encoding="utf-8")
sized.append_learned(["x" * 400])
one_long = sized._learned_zone_tokens()
sized.append_learned([("y" * 400)])
two_long = sized._learned_zone_tokens()
check("the estimate is non-zero once there is content", one_long > 0)
check("...and grows with the CONTENT, not merely the number of entries "
      "(400 chars is ~100 tokens, so two of them differ by about that)",
      90 <= (two_long - one_long) <= 110)

short = load("60", "short")
for p in (pathlib.Path(short.SYSTEM_PROMPT_FILE), pathlib.Path(short.GEMINI_PROMPT_FILE)):
    p.write_text("You are an assistant.\n", encoding="utf-8")
short.append_learned(["a short fact here"])
check("one short fact costs far less than one long one, though the COUNT is "
      "identical -- which is why the count alone was not enough",
      short._learned_zone_tokens() < one_long)

# --- 4. /learned reports the count against the cap -------------------------
def cmd_src(name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.unparse(n)
    return ""

learned_cmd = cmd_src("cmd_learned")
check("/learned shows the cap alongside the count, in both languages",
      learned_cmd.count("LEARNED_MAX_FACTS") >= 2
      and "of {LEARNED_MAX_FACTS}" in learned_cmd
      and "dari {LEARNED_MAX_FACTS}" in learned_cmd)
check("/learned shows the token estimate too, in both languages -- the count is "
      "a proxy, this is what is actually paid",
      "_learned_zone_tokens" in learned_cmd
      and learned_cmd.count("~{est}") >= 2)
check("...marked as an estimate rather than presented as a measurement",
      "~{est}" in learned_cmd)
# A full zone lands around 1-2k, and _fmt_tok renders every value in that band
# as "1k" -- the one range where this number has to be readable to be useful.
check("the estimate is not rounded through _fmt_tok, which would collapse "
      "1,100 and 1,900 into the same '1k'",
      "_fmt_tok(_learned_zone_tokens())" not in learned_cmd
      and "_learned_zone_tokens():," in learned_cmd)
check("...and says WHEN it is paid, since on a deployment that uses /new freely "
      "it is a floor cost rather than a one-off",
      "starts a new conversation" in learned_cmd
      and "memulai percakapan baru" in learned_cmd)
check("/learned warns when approaching the cap, rather than only after facts "
      "have already been silently dropped",
      "Close to the cap" in learned_cmd and "dekat batas" in learned_cmd)
check("...and the warning names both ways out (/forget, or raise the setting)",
      "/forget" in learned_cmd and "LEARNED_MAX_FACTS in .env" in learned_cmd)

# Missing rather than crashing: this suite is pointed at a bare scratch copy of
# the module when it is being checked in the failing direction, and a detector
# that dies there instead of reporting is no use in the one run that matters.
_env_example = SRC.parent / ".env.example"
check("the setting is documented in .env.example",
      _env_example.is_file()
      and "LEARNED_MAX_FACTS" in _env_example.read_text(encoding="utf-8"))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
