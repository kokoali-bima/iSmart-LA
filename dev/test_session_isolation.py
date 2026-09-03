#!/usr/bin/env python3
"""Tests that a fresh session is genuinely fresh, and private to its chat.

The bug: sessions were created with `dict(EMPTY_SESSION)` off a module-level
constant `EMPTY_SESSION = {"claude": {}, "agy": {}}`. dict() is a SHALLOW
copy, so the two inner dicts were the SAME objects in every session ever
created that way -- and run_combo writes resume handles straight into them
(`sess.setdefault("agy", {})[model] = conversation_id`). So the first chat to
answer anything wrote its conversation id into the shared constant, and every
session created afterwards started life already pointing at it.

Confirmed against the real get_chat_state before the fix:

    chatB brand-new session -> {'claude': {}, 'agy': {'m': 'A-CONV', ...}}
    CROSS-CHAT LEAK?        -> True

Two consequences, and the second is the serious one:
  * /new did not actually start fresh -- it returned a session still holding
    the previous conversation, quietly defeating the biggest documented
    cost-saving habit this bot has.
  * a brand-new chat could resume a DIFFERENT chat's conversation. In a
    deployment shared between groups -- which this project supports on
    purpose, per-group PINs and all -- that is a context leak, not merely a
    billing surprise.
"""
import atexit
import shutil as _shutil
import importlib.util, os, sys, tempfile
from pathlib import Path

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
_scratch = tempfile.mkdtemp(prefix="isla_iso_")
# Tests must not litter the machine they run on -- 485 stale isla_*
# directories turned up on a real server. atexit rather than a call at
# the end, so a failing assertion still cleans up.
atexit.register(_shutil.rmtree, _scratch, ignore_errors=True)
os.environ["HOME"] = _scratch
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

# --- 1. no shared mutable default remains ----------------------------------
check("there is no module-level EMPTY_SESSION constant left to copy shallowly",
      not hasattr(mod, "EMPTY_SESSION"))

a, b = mod._empty_session(), mod._empty_session()
check("two fresh sessions do NOT share their inner 'agy' dict (THE bug)",
      a["agy"] is not b["agy"])
check("...nor their inner 'claude' dict",
      a["claude"] is not b["claude"])

a["agy"]["m"] = "A-CONV"
check("writing into one fresh session leaves the next one empty",
      mod._empty_session()["agy"] == {})

# --- 2. the real get_chat_state path, chat to chat -------------------------
st1 = mod.get_chat_state({}, "chatA")
st1["sessions"]["default"].setdefault("agy", {})["m"] = "A-CONV"
st2 = mod.get_chat_state({}, "chatB")
check("a brand-new chat does NOT inherit another chat's conversation id "
      "(the cross-chat leak)",
      st2["sessions"]["default"]["agy"] == {})
check("...and its claude side is clean too",
      st2["sessions"]["default"]["claude"] == {})

# --- 3. /new really resets --------------------------------------------------
sessions = {}
st = mod.get_chat_state(sessions, "chatC")
st["sessions"]["default"].setdefault("agy", {})["m"] = "OLD-CONV"
st["sessions"]["default"].setdefault("claude", {})["c"] = "OLD-SESSION"
# what cmd_new does
st["sessions"]["default"] = mod._empty_session()
check("/new gives a session with no leftover agy conversation",
      st["sessions"]["default"]["agy"] == {})
check("/new gives a session with no leftover claude session",
      st["sessions"]["default"]["claude"] == {})
check("...and the chat it was reset from is genuinely independent afterwards",
      mod.get_chat_state({}, "chatD")["sessions"]["default"]["agy"] == {})

# --- 4. a second named session in the SAME chat is independent -------------
st3 = mod.get_chat_state({}, "chatE")
st3["sessions"]["work"] = mod._empty_session()
st3["sessions"]["work"]["agy"]["m"] = "WORK-CONV"
st3["sessions"]["home"] = mod._empty_session()
check("two named sessions in one chat keep separate conversations",
      st3["sessions"]["home"]["agy"] == {})

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
