#!/usr/bin/env python3
"""Tests for /setownerscope: extra scope that applies ONLY when the bot owner
is speaking in their own private chat -- never a group, even one the owner is
speaking in, and never anyone else even inside the owner's own resumed
conversation.

Asked directly: the team wants the bot to help with general things too (a
joke, say), but wanted ONE shared broadened scope for every group ("semua
chat di group scope luasnya barengan"), plus something EXTRA the owner alone
gets, and confirmed explicitly that the extra part must be DM-only, never in
a group even when the owner is the one typing there.

That is a different axis from /setscope entirely: /setscope changes what the
agent IS FOR, identically for every chat. This changes what it's
ADDITIONALLY willing to do, gated on WHO is asking AND WHERE, checked fresh
on every single turn (like MEMORY.md and write_mode_notice() already are) --
never baked into a conversation's history, so a non-owner continuing the
owner's own resumed DM can't inherit it, and the owner speaking in a group
can't leak it there either.
"""
import asyncio, importlib.util, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_ownerscope_"))
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = "-999"

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

OWNER = 111
OTHER = 222
GROUP = -999

def upd(user_id=OWNER, chat_id=OWNER, chat_type="private", text="hi"):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=user_id),
                           effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title="g"))
def ctx(args=None):
    return SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock()), args=args or [])

# --- 1. storage round-trip --------------------------------------------------
check("nothing set yet", mod.owner_scope_text() == "")
mod.set_owner_scope("also help with jokes and general chat")
check("set/read round-trips", mod.owner_scope_text() == "also help with jokes and general chat")
mod.set_owner_scope("  padded and multi   word  \n")
check("stored text is stripped of surrounding whitespace",
      mod.owner_scope_text() == "padded and multi   word")
check("clear_owner_scope reports True when something WAS set",
      mod.clear_owner_scope() is True)
check("...and reading afterward is empty again", mod.owner_scope_text() == "")
check("clear_owner_scope reports False when nothing was set",
      mod.clear_owner_scope() is False)

# --- 2. _build_agy_prompt: owner_dm gates the extra clause ------------------
mod.set_owner_scope("EXTRA-OWNER-CLAUSE")
p1 = mod._build_agy_prompt("hi", owner_dm=True)
check("owner_dm=True includes the extra clause", "EXTRA-OWNER-CLAUSE" in p1)
p2 = mod._build_agy_prompt("hi", owner_dm=False)
check("owner_dm=False never includes it, even with text set", "EXTRA-OWNER-CLAUSE" not in p2)
mod.clear_owner_scope()
p3 = mod._build_agy_prompt("hi", owner_dm=True)
check("owner_dm=True with nothing SET adds nothing extra (no crash, no stray section)",
      "EXTRA-OWNER-CLAUSE" not in p3)

# --- 3. Claude side: folded into the SAME --append-system-prompt call ------
mod.set_owner_scope("EXTRA-OWNER-CLAUSE")
seen_cmds = []
def fake_run(cmd, cwd=None, env=None, capture_output=None, text=None, timeout=None):
    seen_cmds.append(cmd)
    return SimpleNamespace(returncode=0, stdout='{"result":"ok","session_id":"s1","usage":{}}', stderr="")

with patch.object(mod.subprocess, "run", side_effect=fake_run):
    mod.run_claude("hi", None, "default", mod.CLAUDE_MODEL_PRIMARY, owner_dm=True)
cmd = seen_cmds[-1]
check("Claude call includes the extra clause when owner_dm=True",
      "--append-system-prompt" in cmd and
      any("EXTRA-OWNER-CLAUSE" in a for a in cmd))
check("...as ONE combined flag, not a second --append-system-prompt "
      "(never verified Claude Code CLI accumulates repeats)",
      cmd.count("--append-system-prompt") <= 1)

seen_cmds.clear()
with patch.object(mod.subprocess, "run", side_effect=fake_run):
    mod.run_claude("hi", None, "default", mod.CLAUDE_MODEL_PRIMARY, owner_dm=False)
cmd2 = seen_cmds[-1]
check("Claude call does NOT include it when owner_dm=False",
      not any("EXTRA-OWNER-CLAUSE" in a for a in cmd2))
mod.clear_owner_scope()

# --- 4. run_combo: owner_dm reaches whichever tier actually answers --------
AGY = next((t["model"] for t in mod.TIERS if t["provider"] == "agy"), None)
mod.set_owner_scope("EXTRA-OWNER-CLAUSE")
if AGY:
    seen_prompts = []
    def fake_agy(prompt, model, conv_id, *a, **kw):
        seen_prompts.append(prompt)
        return {"response": "ok", "conversation_id": "c1", "usage": {"total_tokens": 1}}
    with patch.object(mod, "_run_agy_once", side_effect=fake_agy):
        mod.run_combo("hi", {"agy": {}, "claude": {}}, "default", owner_dm=True)
    check("run_combo(owner_dm=True) reaches the agy tier's prompt",
          seen_prompts and "EXTRA-OWNER-CLAUSE" in seen_prompts[0])

    seen_prompts.clear()
    with patch.object(mod, "_run_agy_once", side_effect=fake_agy):
        mod.run_combo("hi", {"agy": {}, "claude": {}}, "default", owner_dm=False)
    check("run_combo(owner_dm=False) does not, even with text set",
          seen_prompts and "EXTRA-OWNER-CLAUSE" not in seen_prompts[0])
mod.clear_owner_scope()

# --- 5. the exact gate the user asked for: owner + private DM, nothing else
def owner_dm_for(user_id, chat_type):
    u = upd(user_id=user_id, chat_type=chat_type)
    return mod._is_owner(u) and u.effective_chat.type == "private"

check("owner in their own private DM -> extra scope applies",
      owner_dm_for(OWNER, "private") is True)
check("owner in a GROUP -> does NOT apply, even though it's the owner speaking "
      "(the exact requirement: 'bukan di group')",
      owner_dm_for(OWNER, "group") is False)
check("a non-owner in a private DM -> does not apply",
      owner_dm_for(OTHER, "private") is False)
check("a non-owner in a group -> does not apply",
      owner_dm_for(OTHER, "group") is False)

# --- 6. cmd_setownerscope: permission and location gating ------------------
async def main():
    u1 = upd(user_id=OTHER, chat_type="private")
    await mod.cmd_setownerscope(u1, ctx(["some", "text"]))
    txt1 = u1.message.reply_text.call_args[0][0].lower()
    check("a non-owner is refused outright",
          "owner" in txt1 or "pemilik" in txt1)
    check("...and nothing was written", mod.owner_scope_text() == "")

    u2 = upd(user_id=OWNER, chat_type="group", chat_id=GROUP)
    await mod.cmd_setownerscope(u2, ctx(["some", "text"]))
    txt2 = u2.message.reply_text.call_args[0][0].lower()
    check("the OWNER in a GROUP is refused too -- must be set from their own DM",
          "dm" in txt2 or "private" in txt2 or "pribadi" in txt2)
    check("...and nothing was written from the group attempt either",
          mod.owner_scope_text() == "")

    u3 = upd(user_id=OWNER, chat_type="private")
    await mod.cmd_setownerscope(u3, ctx([]))
    txt3 = u3.message.reply_text.call_args[0][0]
    check("bare command with nothing set shows the intro/usage text",
          "/setownerscope" in txt3)

    u4 = upd(user_id=OWNER, chat_type="private")
    await mod.cmd_setownerscope(u4, ctx(["also", "help", "with", "jokes"]))
    check("the owner, in their own DM, CAN set it",
          mod.owner_scope_text() == "also help with jokes")
    txt4 = u4.message.reply_text.call_args[0][0]
    check("...and gets a clear confirmation back",
          "also help with jokes" in txt4)

    u5 = upd(user_id=OWNER, chat_type="private")
    await mod.cmd_setownerscope(u5, ctx([]))
    txt5 = u5.message.reply_text.call_args[0][0]
    check("bare command with something set shows the CURRENT value",
          "also help with jokes" in txt5)

    u6 = upd(user_id=OWNER, chat_type="private")
    await mod.cmd_setownerscope(u6, ctx(["clear"]))
    check("'clear' removes it", mod.owner_scope_text() == "")

    u7 = upd(user_id=OWNER, chat_type="private")
    await mod.cmd_setownerscope(u7, ctx(["clear"]))
    txt7 = u7.message.reply_text.call_args[0][0].lower()
    check("clearing again (nothing to clear) says so rather than pretending it worked",
          "nothing" in txt7 or "belum ada" in txt7)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
