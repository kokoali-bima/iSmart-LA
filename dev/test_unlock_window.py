#!/usr/bin/env python3
"""The write window, and the reply target -- both from one night's real logs.

What the operator saw: enter the PIN, wait, get asked for the PIN again, and
then nothing at all. Fourteen unlocks in a day.

What the logs showed, exactly:

    23:06:50  WRITE MODE OPENED for 10 minute(s)     -> expires 23:16:50
    23:06:51  running agy
    23:21:26  running agy (failover to a second model, same turn)
    23:25:01  AttributeError: 'NoneType' has no attribute 'reply_text'

Two faults compounding. The window was ten minutes and the turn took eighteen,
because agy failed over mid-turn and the second model started from scratch. The
end-of-turn check then saw a closed window and offered the unlock again -- and
offer_unlock reached for update.message, which is ALWAYS None inside a button
callback, so the turn died after the work was already done.

Separately, at 10:03 and 10:05 the same morning, delivery failed twice and then
"even the failure notice couldn't be delivered": _msg() knew about typed
messages and button callbacks but not EDITED ones, where both are None.
"""
import atexit
import importlib.util
import os
import shutil as _shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SRC = sys.argv[1]
scratch = Path(tempfile.mkdtemp(prefix="isla_unlockwin_"))
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)
mod.LEDGER_FILE = scratch / "spend.jsonl"

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


# --- the window itself -----------------------------------------------------
check(f"the default window is 30 minutes, not 15 "
      f"({mod.WRITE_MODE_DEFAULT_MINUTES})",
      mod.WRITE_MODE_DEFAULT_MINUTES == 30)
check(f"the ceiling is 6 hours ({mod.WRITE_MODE_MAX_MINUTES} min)",
      mod.WRITE_MODE_MAX_MINUTES == 360)
check("the default comfortably covers the 18-minute turn that broke this",
      mod.WRITE_MODE_DEFAULT_MINUTES >= 18)
check("a group can be given the same ceiling, not a 10-minute one",
      mod.WRITE_MODE_GROUP_MAX_MINUTES >= mod.WRITE_MODE_DEFAULT_MINUTES)


def upd(chat_type="private"):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=1, type=chat_type))


check("a DM unlock is capped at the 6-hour ceiling",
      mod._effective_unlock_cap(upd("private")) == mod.WRITE_MODE_MAX_MINUTES)
check("a group unlock is capped by its own knob",
      mod._effective_unlock_cap(upd("supergroup")) == mod.WRITE_MODE_GROUP_MAX_MINUTES)


# --- _msg(): all three kinds of update -------------------------------------
typed = SimpleNamespace(message="M", callback_query=None, effective_message="M")
check("_msg finds a typed message", mod._msg(typed) == "M")

tapped = SimpleNamespace(message=None,
                         callback_query=SimpleNamespace(message="BTN"),
                         effective_message="BTN")
check("_msg finds the message behind a button press", mod._msg(tapped) == "BTN")

# THE bug: an edited message has neither of the first two.
edited = SimpleNamespace(message=None, callback_query=None, effective_message="ED")
check("_msg finds an EDITED message instead of returning None "
      "(the 10:03 delivery failure)", mod._msg(edited) == "ED")

nothing = SimpleNamespace(message=None, callback_query=None, effective_message=None)
check("_msg still returns None when there is genuinely nowhere to reply",
      mod._msg(nothing) is None)


# --- offer_unlock must not reach for update.message directly ---------------
src_text = Path(SRC).read_text(encoding="utf-8")
start = src_text.index("async def offer_unlock")
end = src_text.index("\nasync def ", start + 10)
body = src_text[start:end]

def code_only(text: str) -> str:
    """Strip comment lines. The first version of this check failed on the
    comment that EXPLAINS the fix, which is not a defect worth failing over."""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


check("offer_unlock never touches update.message directly -- it is reached "
      "from inside a button callback, where that is always None",
      "update.message" not in code_only(body))
check("...and replies through _msg() instead", "_msg(update)" in body)


# --- the end-of-turn re-offer ---------------------------------------------
check("the turn records whether write mode was open when it STARTED",
      "write_open_at_start = write_mode_expires_at() is not None" in src_text)
check("...and the re-offer is suppressed for a turn that began authorized",
      "and not write_open_at_start" in src_text)
check("...with the operator told the window closed, rather than re-prompted",
      "write window expired mid-turn" in src_text)


# --- offer_unlock, driven for real on a callback-only update ---------------
async def main():
    sent = []

    class Msg:
        async def reply_text(self, *a, **k):
            sent.append(a[0] if a else "")

    u = SimpleNamespace(
        message=None,
        callback_query=SimpleNamespace(message=Msg()),
        effective_message=None,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=111),
    )
    with patch.object(mod, "_chat_lang", return_value="id"), \
            patch.object(mod, "guess_vmid", return_value=None):
        try:
            await mod.offer_unlock(u, "restart a VM", "please restart it", None)
            crashed = False
        except AttributeError:
            crashed = True
    check("offer_unlock survives a callback-only update (the actual crash)",
          not crashed)
    check("...and actually sent the unlock card", len(sent) == 1)


import asyncio
asyncio.run(main())


# --- extending without a second PIN, but not without a bound ---------------
# The operator's point: infra work runs for hours, and re-entering the PIN
# every half hour does not make it safer, it makes the prompt something people
# clear without reading. So extension needs no PIN -- but the ceiling has to be
# measured from the ORIGINAL unlock, or a chain of extensions is an unlimited
# unlock wearing a bound's clothing.
mod.WRITE_STATE_FILE = scratch / "write_mode.json"
mod.SSH_RW_KEY = scratch / "rw"
mod.SSH_RW_KEY.write_text("k", encoding="utf-8")
mod.SSH_ACTIVE_KEY = scratch / "active"
with patch.object(mod, "_point_active_key_at", lambda *_: None):
    t0 = mod.unlock_write_mode(30, max_minutes=360)
    check("a fresh unlock records when the session began",
          "opened_at" in mod.WRITE_STATE_FILE.read_text())
    check("...and opens for the minutes asked", 29 * 60 < t0 - mod._dt.datetime.now().timestamp() <= 30 * 60)

    room = mod.write_mode_session_left(360)
    check(f"the session reports its remaining ceiling ({room} min)",
          355 <= room <= 360)

    t1 = mod.unlock_write_mode(30, max_minutes=360, extend=True)
    check("extending pushes the window out", t1 > t0)
    check("...without moving the session start",
          abs(mod.write_mode_session_left(360) - room) <= 1)

    # Now pretend the session began 5h58m ago: an extension may only reach the
    # ceiling, never past it.
    import json as _json
    state = _json.loads(mod.WRITE_STATE_FILE.read_text())
    state["opened_at"] = mod._dt.datetime.now().timestamp() - (358 * 60)
    mod.WRITE_STATE_FILE.write_text(_json.dumps(state))
    t2 = mod.unlock_write_mode(30, max_minutes=360, extend=True)
    left2 = (t2 - mod._dt.datetime.now().timestamp()) / 60
    check(f"an extension cannot chain past the 6-hour ceiling "
          f"({left2:.0f} min granted, not 30)", left2 <= 3)
    check("...and the session then reports no room left",
          mod.write_mode_session_left(360) <= 2)

src_text2 = Path(SRC).read_text(encoding="utf-8")
check("a second /unlock while open is answered with the time left, not a "
      "second PIN keypad", "Write mode is already open" in src_text2)
check("...and offers an extend button instead",
      "extend_write:" in src_text2)
check("the extend handler exists and needs no PIN",
      "async def cmd_extend_write_button" in src_text2
      and "request_pin" not in src_text2.split(
          "async def cmd_extend_write_button")[1].split("\nasync def ")[0])

# --- the progress heartbeat ------------------------------------------------
# Grounded in 178 measured turns: median 29s, p75 84s, p90 284s.
check(f"the first note waits long enough that most turns never produce one "
      f"({mod.HEARTBEAT_FIRST_SECONDS}s vs 29s median)",
      mod.HEARTBEAT_FIRST_SECONDS >= 84)
check(f"...and then repeats on a cadence that keeps a 2-hour job to one "
      f"edited message ({mod.HEARTBEAT_EVERY_SECONDS}s)",
      120 <= mod.HEARTBEAT_EVERY_SECONDS <= 600)
check("the window warning fires before the window ends",
      1 <= mod.WRITE_WARN_MINUTES < mod.WRITE_MODE_DEFAULT_MINUTES)


async def beat_test():
    sent, edited, deleted = [], [], []

    class Bot:
        async def send_message(self, chat_id, text, reply_markup=None):
            sent.append(text)
            return SimpleNamespace(message_id=7)

        async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            edited.append(text)

        async def delete_message(self, chat_id, message_id):
            deleted.append(message_id)

    ctx = SimpleNamespace(bot=Bot())
    with patch.object(mod, "HEARTBEAT_FIRST_SECONDS", 0), \
            patch.object(mod, "HEARTBEAT_EVERY_SECONDS", 0), \
            patch.object(mod, "write_mode_expires_at", lambda: None):
        task = asyncio.create_task(mod._progress_heartbeat(
            ctx, 1, "id", mod._dt.datetime.now(), 360))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    check("the heartbeat sends ONE message and then edits it, "
          f"never a stream of new ones ({len(sent)} sent, {len(edited)} edits)",
          len(sent) == 1 and len(edited) >= 1)
    check("...and removes it once the turn ends", deleted == [7])

    # A heartbeat that breaks the turn it reports on would be worse than none.
    class BrokenBot:
        async def send_message(self, *a, **k):
            raise RuntimeError("telegram down")

    with patch.object(mod, "HEARTBEAT_FIRST_SECONDS", 0), \
            patch.object(mod, "HEARTBEAT_EVERY_SECONDS", 0), \
            patch.object(mod, "write_mode_expires_at", lambda: None):
        t = asyncio.create_task(mod._progress_heartbeat(
            SimpleNamespace(bot=BrokenBot()), 1, "id", mod._dt.datetime.now(), 360))
        await asyncio.sleep(0.05)
        blew_up = t.done() and t.exception() is not None
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    check("a Telegram failure inside the heartbeat never escapes it", not blew_up)


asyncio.run(beat_test())

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
