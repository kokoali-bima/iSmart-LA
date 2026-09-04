#!/usr/bin/env python3
"""Tests for disconnecting a Google Drive account from /gdrive, and for /help
listing every command in alphabetical order.

The disconnect exists because an operator found an account connected that they
did not remember adding, and had no way to remove it from Telegram -- the only
route was editing rclone.conf on the host by hand.

The care in here is about one distinction. "Remove the account" reads just as
naturally as "delete my files", and only one of those readings is
recoverable. So this revokes the agent's ACCESS (at Google, and locally) and
touches nothing in Drive itself, the confirmation card says so in as many
words, and these tests assert both the behaviour and that the wording is
actually present.
"""
import asyncio
import atexit
import importlib.util
import json
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
scratch = Path(tempfile.mkdtemp(prefix="isla_gddisc_"))
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
mod.GDRIVE_ROOM_ACCOUNTS_FILE = scratch / "gdrive_room_accounts.json"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def rclone_stub(dumped=None, delete_rc=0):
    """Stand in for the rclone binary, recording what it was asked to do."""
    calls = []
    def run(*args, **kw):
        calls.append(list(args))
        if args[:2] == ("config", "dump"):
            return SimpleNamespace(returncode=0, stdout=json.dumps(dumped or {}), stderr="")
        if args[:2] == ("config", "delete"):
            return SimpleNamespace(returncode=delete_rc, stdout="",
                                   stderr="" if delete_rc == 0 else "boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run, calls


# --- 1. the disconnect itself ----------------------------------------------
DUMP = {"gdrive": {"type": "drive",
                   "token": json.dumps({"access_token": "a", "refresh_token": "R123"})}}

run, calls = rclone_stub(DUMP)
mod._write_gdrive_room_accounts({"-100999": "gdrive", "-100777": "other"})
revoked = []
with patch.object(mod, "_rclone_run", side_effect=run), \
     patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive", "other"]), \
     patch.object(mod, "_post_form", side_effect=lambda url, f: revoked.append(f) or (200, {})):
    ok, detail = mod.disconnect_gdrive_account("gdrive")

check("disconnecting a connected account succeeds", ok)
check("the rclone remote is deleted through rclone's own config API, never by "
      "hand-editing rclone.conf", ["config", "delete", "gdrive"] in calls)
check("the grant is REVOKED at Google, not merely forgotten locally -- "
      "otherwise a leaked config file still holds live access",
      revoked and revoked[0].get("token") == "R123")
check("...and the reply says so, in both languages",
      detail.startswith("drive_revoked_and_removed")
      and "revoked" in mod._detail("en", detail)
      and "dicabut" in mod._detail("id", detail))
check("...with the room count rendered in both, not glued on as English prose "
      "that would stop the whole thing being a translatable key",
      "1 room unset" in mod._detail("en", detail)
      and "1 room dilepas" in mod._detail("id", detail))

rooms = mod._read_gdrive_room_accounts()
check("a room pointing at the removed account is unset, so uploads cannot "
      "quietly land in whichever account happens to be first",
      "-100999" not in rooms)
check("...while rooms pointing elsewhere are left alone", rooms.get("-100777") == "other")

# --- 2. nothing in Drive is touched ----------------------------------------
DESTRUCTIVE = ("delete", "purge", "rmdir", "deletefile", "cleanup")
used = {c[0] for c in calls}
check("NO destructive rclone verb is ever used against the remote's contents "
      "-- files already in Drive stay exactly where they are",
      not (used & set(DESTRUCTIVE)))
check("...the only 'delete' is rclone's CONFIG delete, which is local",
      all(c[0] != "delete" for c in calls))

# --- 3. failure paths ------------------------------------------------------
with patch.object(mod, "_list_gdrive_accounts", return_value=[]):
    ok, detail = mod.disconnect_gdrive_account("ghost")
check("disconnecting something that isn't connected fails cleanly", not ok)

run2, _ = rclone_stub(DUMP, delete_rc=1)
with patch.object(mod, "_rclone_run", side_effect=run2), \
     patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]), \
     patch.object(mod, "_post_form", return_value=(200, {})):
    ok, detail = mod.disconnect_gdrive_account("gdrive")
check("an rclone failure is reported, not swallowed", not ok)

# Google unreachable must NOT block the local removal: leaving a half-removed
# account behind is worse than a grant that stays live until revoked by hand.
run3, calls3 = rclone_stub(DUMP)
with patch.object(mod, "_rclone_run", side_effect=run3), \
     patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]), \
     patch.object(mod, "_post_form", return_value=(0, {"error": "network"})):
    ok, detail = mod.disconnect_gdrive_account("gdrive")
check("if Google can't be reached, the local removal still happens", ok)
check("...and the reply admits the grant was NOT revoked, rather than "
      "claiming a logout that did not happen",
      detail == "drive_removed_only"
      and "could not reach Google" in mod._detail("en", detail)
      and "tidak terjangkau" in mod._detail("id", detail))

# A token rclone won't give up must not stop the removal either.
run4, _ = rclone_stub({})
with patch.object(mod, "_rclone_run", side_effect=run4), \
     patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]), \
     patch.object(mod, "_post_form", side_effect=AssertionError("must not revoke nothing")):
    ok, _ = mod.disconnect_gdrive_account("gdrive")
check("an unreadable stored token skips the revoke instead of crashing", ok)


# --- 4. the buttons --------------------------------------------------------
def query(data):
    return SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                           message=SimpleNamespace(text="x"))

def upd(data):
    q = query(data)
    return SimpleNamespace(message=None, callback_query=q, effective_message=q.message,
                           effective_user=SimpleNamespace(id=111),
                           effective_chat=SimpleNamespace(id=111, type="private", title=None)), q

def ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), args=[])


async def main():
    with patch.object(mod, "_may_authorize_group_action", new=AsyncMock(return_value=True)), \
         patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]):

        # Tapping disconnect must CONFIRM, never act straight away.
        u, q = upd("gdrv:rm:gdrive")
        with patch.object(mod, "disconnect_gdrive_account",
                          side_effect=AssertionError("must not remove without confirming")):
            await mod.cmd_gdrive_button(u, ctx())
        card = q.edit_message_text.call_args[0][0]
        check("tapping disconnect asks for confirmation first", "?" in card)
        check("...and states plainly that NOTHING in Google Drive is deleted -- "
              "the reading that would be unrecoverable if it were wrong",
              "Nothing in Google Drive is deleted" in card
              or "Tidak ada yang dihapus di Google Drive" in card)
        check("...and that files already uploaded stay put",
              "stay exactly where they are" in card or "tetap di tempatnya" in card)

        # Confirming does the work.
        u, q = upd("gdrv:rmyes:gdrive")
        with patch.object(mod, "disconnect_gdrive_account",
                          return_value=(True, "access revoked at Google and removed locally")) as d:
            await mod.cmd_gdrive_button(u, ctx())
        check("confirming actually disconnects", d.call_count == 1)
        check("...naming the account it removed",
              "gdrive" in q.edit_message_text.call_args[0][0])

        # Cancelling does not.
        u, q = upd("gdrv:cancel:-")
        with patch.object(mod, "disconnect_gdrive_account",
                          side_effect=AssertionError("must not remove on cancel")):
            await mod.cmd_gdrive_button(u, ctx())
        check("cancelling removes nothing",
              "ancel" in q.edit_message_text.call_args[0][0]
              or "atal" in q.edit_message_text.call_args[0][0])

        # A picker card left open from before this feature shipped still works.
        u, q = upd("gdrv:gdrive")
        await mod.cmd_gdrive_button(u, ctx())
        check("an old-format 'gdrv:<name>' button still selects, so a card "
              "sitting in a chat from before the update does not break",
              "gdrive" in q.edit_message_text.call_args[0][0])

        # And the picker offers a disconnect for every account.
        msg = SimpleNamespace(text="", reply_text=AsyncMock())
        u2 = SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                             effective_user=SimpleNamespace(id=111),
                             effective_chat=SimpleNamespace(id=111, type="private", title=None))
        await mod.cmd_gdrive(u2, ctx())
        markup = u2.message.reply_text.call_args[1]["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        check("/gdrive offers a disconnect button per account", "gdrv:rm:gdrive" in datas)
        check("...and still offers selection", "gdrv:use:gdrive" in datas)

    # --- 5. /help is alphabetical and complete -----------------------------
    src = Path(SRC).read_text(encoding="utf-8")
    registered = set()
    for m in re.finditer(r'CommandHandler\(\s*(\[[^\]]*\]|"[\w]+")\s*,\s*cmd_\w+', src):
        registered |= set(re.findall(r'"(\w+)"', m.group(1)))

    for tag in ("EN", "ID"):
        block = src[src.index(f"HELP_TEXT_{tag} = "):]
        block = block[:block.index('"""', 20)]
        listed = [l.split(" ")[0] for l in block.split("\n") if re.match(r"^/\w+", l)]
        check(f"/help ({tag}) lists commands in alphabetical order, so a "
              f"half-remembered name can be found without reading all 48",
              listed == sorted(listed, key=str.lower))
        missing = registered - {c[1:] for c in listed} - {"start", "language"}
        check(f"/help ({tag}) is not missing any registered command", not missing)

    check("the stale claim that /remember is global across chats is gone -- "
          "per-chat memory shipped in v0.2b.49 and this line still said "
          "otherwise, wrongly, about privacy",
          "GLOBAL* across every chat" not in src and "GLOBAL* untuk semua chat" not in src)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
