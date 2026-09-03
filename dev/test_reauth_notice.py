#!/usr/bin/env python3
"""Tests for the Gemini/Antigravity re-auth notice.

_agy_attempt_needs_reauth() is tested against the ACTUAL production log line
observed on 10.10.63.11 (bscloud) when its Antigravity session died -- not a
synthetic stand-in -- plus cases that must NOT trigger it (a normal network
failover, a claude-side failure, success)."""
import asyncio, importlib.util, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

SRC = sys.argv[1]
scratch = Path(tempfile.mkdtemp(prefix="isla_reauth_"))
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

# LEDGER_FILE/MEMORY_DIR/MEMORY_FILE are BASE_DIR-relative (the module's own
# directory), NOT HOME-relative -- overriding HOME above does not sandbox
# them, and this suite drives real _run_turn() calls. See test_concurrency.py
# for the live confirmation: without this, running against a real checkout
# wrote spend.jsonl straight into the repo directory.
mod.LEDGER_FILE = scratch / "spend.jsonl"
mod.MEMORY_DIR = scratch / "memory"
mod.MEMORY_FILE = scratch / "MEMORY.md"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

# The exact tail captured in production (stderr[-500:]) when bscloud's Gemini
# session died -- a URL-encoded Google OAuth authorize URL, truncated mid-word
# ("ps%3A%2F%2F..." is the tail of "https://").
REAL_REAUTH_LINE = (
    "agy:gemini-3.7-flash-medium FAILED/failover (agy exited 1: "
    "ps%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform+"
    "https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email+"
    "https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.profile+"
    "https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcclog+"
    "https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fexperimentsandconfigs+"
    "https%3A%2F%2Fwww.googleapis.com%2Fauth%2Faicode+openid&"
    "state=Z01I5cwIwWDRMVEGwvTQyQ)"
)

check("the REAL production failure line is detected",
      mod._agy_attempt_needs_reauth([REAL_REAUTH_LINE]))
check("detected even mixed in with unrelated attempts",
      mod._agy_attempt_needs_reauth([
          "agy:gemini-3.7-flash-medium SKIPPED (cooldown)",
          REAL_REAUTH_LINE,
          "claude:claude-haiku-4-5-20251001 OK (11578 tok)",
      ]))

# --- must NOT fire for ordinary failures ---
check("an ordinary agy network failover does not trigger it",
      not mod._agy_attempt_needs_reauth([
          "agy:gemini-3.7-flash-medium FAILED/failover (agy exited 1: "
          "connection timed out after 30s)",
      ]))
check("a claude-side failure does not trigger it",
      not mod._agy_attempt_needs_reauth([
          "claude:claude-haiku-4-5-20251001 FAILED/failover (rate limited, please try again)",
      ]))
check("a clean success list does not trigger it",
      not mod._agy_attempt_needs_reauth([
          "agy:gemini-3.7-flash-medium OK (412 tok)",
      ]))
check("an empty attempts list does not trigger it",
      not mod._agy_attempt_needs_reauth([]))

# --- the notice itself: cooldown + reset behaviour, driven through _run_turn ---
def msg_update(chat_id=555):
    msg = SimpleNamespace(text="hi", reply_text=AsyncMock())
    return SimpleNamespace(
        message=msg, effective_message=msg, callback_query=None,
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=chat_id, type="private", title=None),
    )

def ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

async def run_turn_with(attempts, result_text="ok", model="claude-haiku-4-5-20251001"):
    upd = msg_update()
    def fake_run_combo(*a, **kw):  # run_combo itself is sync, not awaited by _run_turn
        return {"result": result_text, "usage": {}}, model, attempts
    import unittest.mock as um
    with um.patch.object(mod, "run_combo", side_effect=fake_run_combo):
        with um.patch.object(mod, "load_sessions", return_value={}), \
             um.patch.object(mod, "get_chat_state", return_value={"active": "default", "sessions": {"default": {}}}), \
             um.patch.object(mod, "save_sessions"), \
             um.patch.object(mod, "append_learned", return_value=[]), \
             um.patch.object(mod, "register_snapshot"), \
             um.patch.object(mod, "_maybe_notify_update", new=AsyncMock()):
            await mod._run_turn(upd, ctx(), "hi")
    return upd

async def main():
    mod._agy_reauth_last_notice = 0.0

    upd1 = await run_turn_with([REAL_REAUTH_LINE])
    texts1 = [c.args[0] for c in upd1.message.reply_text.call_args_list if c.args]
    check("first hit: the notice IS sent", any("Gemini" in t and "logout" in t for t in texts1))

    # The whole point of a live screenshot: /start kept showing a green check
    # after this exact failure, because agy_signed_in() falls back to a
    # setup_state.json flag that a real failure never used to clear.
    mod._mark_setup("agy", 111)
    check("setup: agy WAS marked done", "agy" in mod._setup_state())
    check("agy_signed_in() reports True from the stale flag alone",
          mod.agy_signed_in() is True)
    await run_turn_with([REAL_REAUTH_LINE])
    check("a live reauth failure clears the stale 'agy' setup flag",
          "agy" not in mod._setup_state())

    upd2 = await run_turn_with([REAL_REAUTH_LINE])
    texts2 = [c.args[0] for c in upd2.message.reply_text.call_args_list if c.args]
    check("second hit within the cooldown: NOT sent again (no spam)",
          not any("logout" in t for t in texts2))

    # A real agy success (inside run_combo's own tier loop, not just any turn)
    # resets the cooldown, so a genuinely NEW outage later notifies promptly
    # instead of waiting out a cooldown left over from one that already ended.
    import unittest.mock as um
    mod._agy_reauth_last_notice = mod._dt.datetime.now().timestamp()  # pretend we just notified
    with um.patch.object(mod, "_run_agy_once",
                          return_value={"status": "SUCCESS", "response": "hi",
                                        "usage": {"total_tokens": 5}, "conversation_id": None}):
        mod.run_combo("hello", {}, "default")
    check("a real agy success resets the cooldown", mod._agy_reauth_last_notice == 0.0)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
