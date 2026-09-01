#!/usr/bin/env python3
"""Tests for /graduate finding the case wherever it actually happened.

/graduate is the one feature that genuinely REDUCES future token spend: it
turns a solved case into a script that costs zero model tokens to reuse. But
it only ever looked at the primary Claude session, and said so in its own
refusal text:

    "if the last turn was answered by Gemini/'mini', /graduate can't see that
     history -- current limitation, each tier keeps its own history"

Since the default chain answers with Gemini FIRST, that meant the cheapest
path -- the normal one -- was exactly the path that could not be graduated.
The cost-saving feature was unavailable for most cases, which is backwards.

Now each turn records which tier answered (`last_model`), and /graduate reads
the history from that tier, whichever it is: agy or claude.
"""
import asyncio, importlib.util, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="isla_grad_")
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

AGY = next((t["model"] for t in mod.TIERS if t["provider"] == "agy"), None)
CLA = mod.CLAUDE_MODEL_PRIMARY

# --- 1. _graduate_target picks where the history actually is ---------------
if AGY:
    check("picks the agy tier when IT answered last (THE bug -- used to refuse)",
          mod._graduate_target({"agy": {AGY: "A1"}, "claude": {}, "last_model": AGY})
          == ("agy", AGY, "A1"))
    check("picks agy from history alone even with no last_model recorded",
          mod._graduate_target({"agy": {AGY: "A1"}, "claude": {}})
          == ("agy", AGY, "A1"))
check("picks claude when IT answered last",
      mod._graduate_target({"agy": {}, "claude": {CLA: "C1"}, "last_model": CLA})
      == ("claude", CLA, "C1"))
if AGY:
    check("prefers the tier that answered LAST when both have history",
          mod._graduate_target({"agy": {AGY: "A1"}, "claude": {CLA: "C1"},
                                "last_model": AGY}) == ("agy", AGY, "A1"))
    check("...and the other way round too",
          mod._graduate_target({"agy": {AGY: "A1"}, "claude": {CLA: "C1"},
                                "last_model": CLA}) == ("claude", CLA, "C1"))
check("falls back to claude primary when last_model is stale/unknown",
      mod._graduate_target({"agy": {}, "claude": {CLA: "C1"}, "last_model": "gone"})
      == ("claude", CLA, "C1"))
check("returns None when the session has no history at all",
      mod._graduate_target({"agy": {}, "claude": {}}) is None)
check("an empty/new session yields None rather than exploding",
      mod._graduate_target(mod._empty_session()) is None)

# --- 2. cmd_graduate end to end -------------------------------------------
def upd():
    msg = SimpleNamespace(text="/graduate x", reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=111),
                           effective_chat=SimpleNamespace(id=111, type="private", title=None))
def ctx(args):
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()), args=args)

async def main():
    if AGY:
        # Gemini-only history: this is the case that used to be refused outright.
        sessions = {"111": {"active": "default", "sessions": {"default": {
            "claude": {}, "agy": {AGY: "AGY-CONV"}, "last_model": AGY}}}}
        saved = {}
        agy_calls = []
        def fake_agy(prompt, model, conv, *a, **kw):
            agy_calls.append({"prompt": prompt, "model": model, "conv": conv})
            return {"response": "made the script", "conversation_id": "AGY-CONV-2",
                    "usage": {"input_tokens": 1, "output_tokens": 2}}
        u = upd()
        with patch.object(mod, "load_sessions", return_value=sessions), \
             patch.object(mod, "save_sessions", side_effect=lambda s: saved.update(s)), \
             patch.object(mod, "_run_agy_once", side_effect=fake_agy), \
             patch.object(mod, "run_claude", side_effect=AssertionError("must not use claude")), \
             patch.object(mod, "_reply_chunked", new=AsyncMock()) as reply:
            await mod.cmd_graduate(u, ctx(["backup-coverage"]))

        check("a Gemini-only session now actually graduates (THE fix)",
              len(agy_calls) == 1)
        check("...through the agy tier that holds the history, not Claude",
              agy_calls and agy_calls[0]["conv"] == "AGY-CONV"
              and agy_calls[0]["model"] == AGY)
        check("...with the script name passed into the instruction",
              agy_calls and "backup-coverage" in agy_calls[0]["prompt"])
        check("...and the refusal message is NOT what got sent",
              u.message.reply_text.call_args is None)
        sent = reply.call_args[0][1] if reply.call_args else ""
        check("...the reply says which tier it was graduated from",
              "graduated from" in sent)
        check("the new conversation id is stored back on the agy side",
              saved.get("111", {}).get("sessions", {}).get("default", {})
                   .get("agy", {}).get(AGY) == "AGY-CONV-2")

    # No history anywhere -> a clean refusal, not a crash
    sessions2 = {"111": {"active": "default", "sessions": {"default": mod._empty_session()}}}
    u2 = upd()
    with patch.object(mod, "load_sessions", return_value=sessions2), \
         patch.object(mod, "save_sessions"), \
         patch.object(mod, "_run_agy_once", side_effect=AssertionError("must not run")), \
         patch.object(mod, "run_claude", side_effect=AssertionError("must not run")), \
         patch.object(mod, "_reply_chunked", new=AsyncMock()):
        await mod.cmd_graduate(u2, ctx(["nothing-here"]))
    txt = u2.message.reply_text.call_args[0][0] if u2.message.reply_text.call_args else ""
    check("an empty session refuses cleanly without calling any model",
          "graduate" in txt.lower() or "belum ada" in txt.lower())

    # --- 3. a turn records which tier answered ----------------------------
    sessions3 = {}
    saved3 = {}
    def fake_combo(text, sess, active, forced_tier=None):
        return {"result": "ok", "usage": {}}, CLA, []
    u3 = upd()
    with patch.object(mod, "run_combo", side_effect=fake_combo), \
         patch.object(mod, "load_sessions", return_value=sessions3), \
         patch.object(mod, "save_sessions", side_effect=lambda s: saved3.update(s)), \
         patch.object(mod, "append_learned", return_value=[]), \
         patch.object(mod, "register_snapshot"), \
         patch.object(mod, "_maybe_notify_update", new=AsyncMock()):
        await mod._run_turn(u3, ctx([]), "hello")
    rec = saved3.get("111", {}).get("sessions", {}).get("default", {})
    check("a completed turn records which tier answered, so /graduate can find it",
          rec.get("last_model") == CLA)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
