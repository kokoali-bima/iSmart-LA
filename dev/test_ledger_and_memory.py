#!/usr/bin/env python3
"""Tests for three things an external review correctly called out as gaps:

1. The token ledger. This project's whole claim is about cost, but the cost
   only ever existed as a sentence inside a log line -- readable while
   scrolling, useless for adding up. The numbers were already in hand
   (run_combo collects a usage block from both providers); writing them down
   as data is what turns "cheaper" from an anecdote into a measurement, and
   what makes /spend cost zero model tokens.

2. A per-turn token ceiling. Without one the project's claim was not
   "bounded cost" but "unscheduled cost" -- two very different things. It is
   enforced BETWEEN tiers, the only point where stopping is still possible:
   tokens inside a single CLI call are already spent by the time it returns.

3. Per-chat memory isolation. MEMORY.md was global: every DM and every
   registered group shared one file. That directly contradicted the
   multi-tenant model this project deliberately supports -- per-group PINs,
   one deployment shared between companies -- because a fact remembered in
   one company's group was injected into another company's very next turn.
   Not a feature request; a defect.
"""
import importlib.util, json, os, sys, tempfile
from pathlib import Path
from unittest.mock import patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_ledger_"))
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

# Keep every file this test writes inside the scratch dir, never the repo.
mod.LEDGER_FILE = scratch / "spend.jsonl"
mod.MEMORY_FILE = scratch / "MEMORY.md"
mod.MEMORY_DIR = scratch / "memory"

AGY = next((t["model"] for t in mod.TIERS if t["provider"] == "agy"), None)

# --- 1. ledger write/read round-trip ---------------------------------------
mod._ledger_append({"chat": "-100", "total": 1200, "wasted": 300})
mod._ledger_append({"chat": "999", "total": 500, "wasted": 0})
rows = mod._ledger_read(1)
check("ledger writes and reads back both rows", len(rows) == 2)
check("...as real JSON objects, not text to be re-parsed by eye",
      all(isinstance(r, dict) for r in rows))
check("...with a timestamp added automatically", all(r.get("ts") for r in rows))

with mod.LEDGER_FILE.open("a", encoding="utf-8") as f:
    f.write('{"chat": "broken", "total":\n')          # a crash mid-write
check("a truncated final line is skipped, not fatal -- /spend still works "
      "after an unclean shutdown", len(mod._ledger_read(1)) == 2)

old = {"ts": "2020-01-01T00:00:00", "chat": "-100", "total": 9_999_999}
with mod.LEDGER_FILE.open("a", encoding="utf-8") as f:
    f.write(json.dumps(old) + "\n")
check("rows older than the window are excluded from the report",
      all(r.get("total") != 9_999_999 for r in mod._ledger_read(1)))

# --- 2. run_combo fills the trace -----------------------------------------
if AGY:
    def ok_agy(prompt, model, conv, *a, **kw):
        return {"response": "hi", "conversation_id": "c1",
                "usage": {"input_tokens": 100, "output_tokens": 20,
                          "thinking_tokens": 5, "cache_read_tokens": 900,
                          "total_tokens": 125}}
    trace = []
    with patch.object(mod, "_run_agy_once", side_effect=ok_agy):
        mod.run_combo("hi", {"agy": {}, "claude": {}}, "default", trace=trace)
    check("a successful tier records structured usage in the trace",
          len(trace) == 1 and trace[0]["outcome"] == "ok" and trace[0]["total"] == 125)
    check("...broken out per field, not glued into one string",
          trace[0]["in"] == 100 and trace[0]["out"] == 20 and trace[0]["think"] == 5)

    # The number nobody could add up before: tokens burned by a tier that failed.
    REAL_WASTE = ('agy exited 1: {"status":"SUCCESS","response":"",'
                  '"error":"(empty response)","usage":{"total_tokens":314646}} '
                  'wasted_tokens=314646')
    calls = {"n": 0}
    def fail_then_ok(prompt, model, conv, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(REAL_WASTE)
        return ok_agy(prompt, model, conv)
    trace2 = []
    mod._tier_cooldown.clear()
    with patch.object(mod, "_run_agy_once", side_effect=fail_then_ok):
        mod.run_combo("hi", {"agy": {}, "claude": {}}, "default", trace=trace2)
    failed_rows = [t for t in trace2 if t["outcome"] == "failed"]
    check("a FAILED tier records what it burned before failing (the 314,646 "
          "that was previously only reachable by reading log prose)",
          failed_rows and failed_rows[0]["wasted"] == 314646)

    mod._tier_cooldown.clear()
    # trace is optional: the ceiling must still work without one
    with patch.object(mod, "_run_agy_once", side_effect=ok_agy):
        r = mod.run_combo("hi", {"agy": {}, "claude": {}}, "default")
    check("run_combo still works with no trace requested (trace is optional)",
          r[1] == AGY)

# --- 3. the per-turn ceiling ----------------------------------------------
if AGY:
    def always_waste(prompt, model, conv, *a, **kw):
        raise RuntimeError("boom wasted_tokens=500000")

    mod._tier_cooldown.clear()
    old_ceiling = mod.TURN_TOKEN_CEILING
    try:
        mod.TURN_TOKEN_CEILING = 0          # off by default
        with patch.object(mod, "_run_agy_once", side_effect=always_waste), \
             patch.object(mod, "run_claude", side_effect=RuntimeError("no claude here")):
            try:
                mod.run_combo("hi", {"agy": {}, "claude": {}}, "default")
                stopped_msg = ""
            except RuntimeError as e:
                stopped_msg = str(e)
        check("with the ceiling OFF (default), the chain runs to the end as before",
              "ceiling" not in stopped_msg.lower())

        mod._tier_cooldown.clear()
        mod.TURN_TOKEN_CEILING = 100_000     # lower than one wasted attempt
        trace3 = []
        with patch.object(mod, "_run_agy_once", side_effect=always_waste), \
             patch.object(mod, "run_claude", side_effect=RuntimeError("no claude here")):
            try:
                mod.run_combo("hi", {"agy": {}, "claude": {}}, "default", trace=trace3)
                stopped_msg = ""
            except RuntimeError as e:
                stopped_msg = str(e)
        check("with a ceiling set, the chain STOPS instead of trying every "
              "remaining tier (THE gap this closes)",
              "ceiling" in stopped_msg.lower())
        check("...and says how much was burned and what to change",
              "500,000" in stopped_msg or "500000" in stopped_msg)
        check("...counting tokens burned by FAILED tiers, not just successful ones",
              any(t.get("reason") == "ceiling" for t in trace3))
    finally:
        mod.TURN_TOKEN_CEILING = old_ceiling
        mod._tier_cooldown.clear()

# --- 4. per-chat memory isolation -----------------------------------------
mod.MEMORY_FILE.write_text("- [old] a shared fact everyone still sees\n", encoding="utf-8")

check("a fact is stored for the chat that asked", mod.append_memory("group A secret", "-100") is True)
check("a second chat stores its own separately", mod.append_memory("group B secret", "-200") is True)

a_text = mod.load_memory_text("-100")
b_text = mod.load_memory_text("-200")
check("chat A sees its own fact", "group A secret" in a_text)
check("chat A does NOT see chat B's fact (THE leak this fixes)",
      "group B secret" not in a_text)
check("chat B does NOT see chat A's fact either",
      "group A secret" not in b_text)
check("both still see the shared base -- nothing existing was dropped",
      "a shared fact" in a_text and "a shared fact" in b_text)

check("a chat with no memory of its own still gets the shared base",
      "a shared fact" in mod.load_memory_text("-999")
      and "group A secret" not in mod.load_memory_text("-999"))
check("no chat id -> shared base only, never another chat's file",
      mod.load_memory_text(None).strip() == "- [old] a shared fact everyone still sees")

check("a malformed chat id is refused rather than becoming a filename",
      mod.append_memory("x", "../../etc/passwd") is False)
check("...and an empty one too", mod.append_memory("x", "") is False)
check("the refused writes created nothing on disk",
      not (mod.MEMORY_DIR / "..").joinpath("passwd").exists())

failed_names = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed_names)}/{len(results)} passed")
if failed_names:
    print("FAILED:", failed_names)
    sys.exit(1)
