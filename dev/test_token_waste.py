#!/usr/bin/env python3
"""Tests for two token costs found by reading a production deployment's own
log, not by reasoning about the code.

**1. The agy timeout was cutting off work that would have finished.**
Across 91 real successful agy turns: median 21s, p90 171s, max 538s. The
timeout was 280s. Cutting a run short there is not a cheap failure -- agy does
the whole job and only then reports "timeout waiting for response", so
everything it burned is discarded: 261k, 392k and 411k tokens in individual
cases, 859,218 across eight failures. The turn then falls through to a pricier
tier that starts COLD, since each tier keeps its own history, so the
replacement answer costs more AND knows less than the one thrown away.

**2. A conversation that has grown expensive says nothing about it.**
In that same log one session's input went from 10 tokens on its first turn to
506,250 on its 95th, with no /new in between -- while the median prompt the
person actually typed was 352 characters. The cost is history, so it is
invisible from where they sit, and it recurs on every further turn.
"""
import asyncio
import atexit
import importlib.util
import os
import re
import shutil as _shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_waste_"))
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ["ALLOWED_USER_IDS"] = "111"
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)
mod.LEDGER_FILE = scratch / "spend.jsonl"
mod.MEMORY_DIR = scratch / "memory"
mod.MEMORY_FILE = scratch / "MEMORY.md"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


# --- 1. the timeout must clear the observed tail --------------------------
check("the agy timeout clears the longest SUCCESSFUL turn seen in production "
      "(538s), so work that would finish is no longer discarded",
      mod.AGY_TIMEOUT >= 600)
check("...and the print-timeout sits just under it, so agy reports its own "
      "clean timeout JSON instead of being hard-killed with no usage data",
      int(re.sub(r"\D", "", mod.AGY_PRINT_TIMEOUT)) < mod.AGY_TIMEOUT)
check("...both still overridable per deployment",
      "AGY_TIMEOUT_SECONDS" in Path(SRC).read_text(encoding="utf-8"))

# --- 2. a discarded run must still be counted -----------------------------
# The waste is only visible if it is recorded; otherwise /spend reports a
# clean bill for a turn that burned 400k tokens before failing.
src = Path(SRC).read_text(encoding="utf-8")
check("tokens burned by a failed tier are recorded, not silently dropped",
      "wasted_tokens=" in src and '"wasted"' in src)

# --- 3. the expensive-conversation hint ------------------------------------
check("there is a threshold for warning about an expensive conversation",
      mod.TURN_COST_HINT_TOKENS > 0)
check("...set above an ordinary turn but below the 506k a real session "
      "reached", 50_000 < mod.TURN_COST_HINT_TOKENS < 506_000)


def upd(text="hi"):
    msg = SimpleNamespace(text=text, caption=None, photo=None, document=None,
                          reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=111),
                           effective_chat=SimpleNamespace(id=111, type="private", title=None))

def ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock(),
                                               send_message=AsyncMock()), args=[])


async def run_turn(usage):
    """One turn with a given usage, returning the tag the reply carried."""
    seen = {}
    async def fake_chunked(update, text, tag_html=""):
        seen["tag"] = tag_html
    def fake_combo(text, sess, active, **kw):
        return {"result": "ok", "usage": usage}, mod.CLAUDE_MODEL_PRIMARY, []
    with patch.object(mod, "run_combo", side_effect=fake_combo), \
         patch.object(mod, "_reply_chunked", side_effect=fake_chunked), \
         patch.object(mod, "append_learned", return_value=[]), \
         patch.object(mod, "register_snapshot"), \
         patch.object(mod, "_maybe_notify_update", new=AsyncMock()):
        await mod._run_turn(upd(), ctx(), "hello")
    return seen.get("tag", "")


async def main():
    mod.save_sessions({})

    tag = await run_turn({"input_tokens": 500, "cache_read_tokens": 800})
    check("an ordinary turn says nothing about cost -- the hint must not "
          "become background noise", "token" not in tag.lower())

    tag = await run_turn({"input_tokens": 220_000, "cache_read_tokens": 300_000})
    check("a turn whose input has grown past the threshold says so",
          "/new" in tag)
    check("...naming the actual size, so it is a fact rather than a nag",
          re.search(r"\d+k|\d+rb", tag) is not None)

    tag2 = await run_turn({"input_tokens": 220_000, "cache_read_tokens": 300_000})
    check("...and says it ONCE per session -- it stays true until /new, so "
          "repeating it every turn would nag instead of inform",
          "/new" not in tag2)

    # cache_read is where the size actually lives, and it arrives under two
    # different key names depending on which CLI answered.
    mod.save_sessions({})
    tag = await run_turn({"input_tokens": 2, "cache_read_input_tokens": 400_000})
    check("cache_read counts toward the size under EITHER CLI's key name -- "
          "counting only input_tokens would miss almost all of it",
          "/new" in tag)

    # /new must clear the flag, or the next expensive conversation is silent.
    mod.save_sessions({})
    tag = await run_turn({"input_tokens": 250_000})
    check("a fresh session warns again when it too grows expensive", "/new" in tag)

    # --- sessions.json survives a write that dies partway ------------------
    # Not a race: all six load->modify->save sequences in the file are
    # await-free, so no coroutine interleaves them (checked, rather than
    # assumed -- the first instinct here was to add a lock for a race that
    # does not exist, and a lock that protects nothing is worse than none
    # because it implies protection). The real exposure is a torn write: a
    # plain write_text() interrupted by a crash or a full disk leaves
    # truncated JSON, and load_sessions() then quietly "starts fresh" -- not
    # one lost chat but EVERY chat's history, each re-sending its whole brief
    # on the next message, with nothing said about it.
    mod.save_sessions({"111": {"active": "default", "sessions": {}}})
    check("sessions.json is written atomically, so a reader never sees a "
          "half-written file", "os.replace" in src)
    check("...and the temp file it writes through is cleaned up on failure",
          "unlink(missing_ok=True)" in src)
    check("...with the round trip intact",
          mod.load_sessions().get("111", {}).get("active") == "default")

    tmp = mod.SESSIONS_FILE.with_suffix(".json.tmp")
    check("no temp file is left behind after a normal save", not tmp.exists())

    # A file that IS corrupt still must not take the bot down.
    mod.SESSIONS_FILE.write_text("{ truncated", encoding="utf-8")
    check("a corrupt sessions.json degrades to empty instead of raising",
          mod.load_sessions() == {})

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
