#!/usr/bin/env python3
"""Tests the backstop against `'NoneType' object has no attribute 'reply_text'`.

Found in this deployment's OWN production log, four times on 2026-09-01:

    File "/root/lite-agent/lite_agent.py", line 4437, in cmd_usemodel
      await update.message.reply_text(_t(lang,
    AttributeError: 'NoneType' object has no attribute 'reply_text'

update.message is None for an edited message, a channel post, and anything
else that isn't a plain new message -- while 157 call sites reach straight
for update.message.reply_text(). v0.2b.46 closed the original trigger by
restricting allowed_updates to ["message", "callback_query"]; this is the
backstop in the three shared auth gates almost every handler already calls.

The second half of these tests is the important half. All TEN button handlers
pass through those same gates, and in a callback update update.message is
ALWAYS None -- the message hangs off update.callback_query instead. A guard
written as `if update.message is None: return False`, which is the obvious
way to write it and was in fact the first cut here, silently kills every
button in the bot: the PIN keypad, /update, unlock, the Drive picker. It
looks like a safety improvement right up until nothing responds.
"""
import atexit
import shutil as _shutil
import asyncio
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_noneguard_"))
# Tests must not litter the machine they run on: 485 stale
# isla_* directories were found on a real server after a few days
# of runs. Registered rather than done at the end, so a failing
# assertion still cleans up.
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ["ALLOWED_USER_IDS"] = "111"
os.environ["ALLOWED_GROUP_IDS"] = "-999"

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)
mod.LEDGER_FILE = scratch / "spend.jsonl"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def make(message=True, callback=False, uid=111, chat=111, ctype="private"):
    msg = SimpleNamespace(text="/usemodel", reply_text=AsyncMock()) if message else None
    cb = SimpleNamespace(data="pin:1", answer=AsyncMock(),
                         edit_message_text=AsyncMock(),
                         message=SimpleNamespace(text="x")) if callback else None
    return SimpleNamespace(message=msg, callback_query=cb,
                           effective_message=msg or (cb.message if cb else None),
                           effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(id=chat, type=ctype, title=None))

def ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock(),
                                               send_message=AsyncMock(),
                                               get_chat_member=AsyncMock()), args=[])


async def main():
    # --- 1. the crash case: neither a message nor a button ------------------
    edited = make(message=False, callback=False)
    check("_authorized refuses an update with no message at all "
          "(THE production crash: an edited /usemodel)",
          mod._authorized(edited) is False)
    check("_may_run_setup refuses it too", mod._may_run_setup(edited) is False)
    check("_may_authorize_group_action refuses it too",
          await mod._may_authorize_group_action(edited, ctx()) is False)

    # --- 2. THE regression this guard can cause: buttons ---------------------
    # In a callback update update.message is None. Guarding on that alone
    # kills every button in the bot.
    btn = make(message=False, callback=True)
    check("a BUTTON press still passes _authorized -- update.message is None "
          "for every callback, so a naive guard would kill all ten button "
          "handlers", mod._authorized(btn) is True)
    check("...and still passes _may_run_setup (owner)", mod._may_run_setup(btn) is True)
    check("...and still passes _may_authorize_group_action (owner)",
          await mod._may_authorize_group_action(btn, ctx()) is True)

    # A non-owner's button in a registered group must still reach its own check
    # rather than being refused by the guard.
    grp_btn = make(message=False, callback=True, uid=999, chat=-999, ctype="supergroup")
    with patch.object(mod, "_is_group_admin", new=AsyncMock(return_value=True)):
        ok = await mod._may_authorize_group_action(grp_btn, ctx())
    check("a group admin's button press is still evaluated on its merits, "
          "not blocked by the guard", ok is True)

    # --- 3. ordinary messages are untouched ---------------------------------
    plain = make(message=True)
    check("a plain message still passes _authorized", mod._authorized(plain) is True)
    check("...and _may_run_setup", mod._may_run_setup(plain) is True)
    check("...and _may_authorize_group_action",
          await mod._may_authorize_group_action(plain, ctx()) is True)

    # --- 4. end to end: the command that actually crashed --------------------
    u = make(message=False, callback=False)
    try:
        await mod.cmd_usemodel(u, ctx())
        crashed = False
    except AttributeError:
        crashed = True
    check("cmd_usemodel on a message-less update returns quietly instead of "
          "raising AttributeError (the exact production traceback)", not crashed)

    # --- 5. the guard is in all three gates, not just the one that crashed --
    src = Path(SRC).read_text(encoding="utf-8")
    check("the guard is present in all three shared gates, so the fix is not "
          "specific to the one command that happened to be reported",
          src.count("update.message is None and update.callback_query is None") == 3)
    check("...and no gate guards on update.message alone, which would kill "
          "the buttons",
          re.search(r"if update\.message is None:\s*\n\s*return False", src) is None)

    # --- 6. allowed_updates, the primary defence, is still in place ---------
    # Still a restricted list, just a longer one: edited_message was added so
    # that adding a forgotten @mention by editing works, and the guard above is
    # what keeps that safe for commands. Channel posts and the rest stay out.
    check("allowed_updates is still an explicit allowlist, not everything",
          "allowed_updates=[" in src and '"channel_post"' not in src)
    check("...and carries exactly the three types this bot handles",
          all(f'"{t}"' in src for t in ("message", "edited_message", "callback_query")))

    # --- editing a message to ADD the mention you forgot -------------------
    # Reported: "kirim teks, lupa tag bot, lalu edit untuk menambahkan tag --
    # tidak dapat respons apa pun". Edited updates were blocked outright
    # because update.message is None for one and a hundred-odd call sites
    # reach straight for update.message.reply_text. Commands still ignore
    # them -- an edited /usemodel crashed this bot for real -- but the plain
    # message path now opts in and reads effective_message instead.
    edited_msg = SimpleNamespace(text="@bot lihat ini", caption=None,
                                 caption_entities=None, entities=[],
                                 photo=None, document=None,
                                 reply_to_message=None, reply_text=AsyncMock())
    edited = SimpleNamespace(message=None, edited_message=edited_msg,
                             callback_query=None, effective_message=edited_msg,
                             effective_user=SimpleNamespace(id=111),
                             effective_chat=SimpleNamespace(id=111, type="private",
                                                            title=None))
    check("an edited message is still refused by default, so commands cannot "
          "crash on it", mod._authorized(edited) is False)
    check("...but the plain-message path can opt in",
          mod._authorized(edited, allow_edited=True) is True)

    got = {}
    async def cap(update, context, text, **kw):
        got["text"] = text
    with patch.object(mod, "_run_turn", side_effect=cap),          patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)),          patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(edited, ctx())
    check("editing a message to add the tag now gets an answer",
          got.get("text") == "@bot lihat ini")

    src2 = Path(SRC).read_text(encoding="utf-8")
    check("edited updates are actually delivered by the poller",
          '"edited_message"' in src2)
    check("...and routed to handle_message, but never to commands",
          "UpdateType.EDITED_MESSAGE" in src2 and "~filters.COMMAND, handle_message" in src2)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
