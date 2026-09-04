#!/usr/bin/env python3
"""Tests for deleting and moving files in Google Drive, PIN-gated.

Asked for directly: "bagaimana caranya agar kita bisa hapus file di gdrive?
memindahkan atau menghapus file di gdrive wajib memakai pin sebelum eksekusi."

Feasibility was checked against the real connected account before any of this
was written, because the drive.file scope only reaches files this client
created and it was not obvious that covered removing them: a probe file was
uploaded, moved with `rclone moveto`, then removed with `rclone deletefile`,
and the folder was verifiably empty afterwards.

The shape follows NEEDS_WRITE, and for the same reason. The model may only
ASK -- by writing a marker line -- and the bot is what acts, after a human
enters the PIN. An instruction telling a model not to delete things is a
request; not handing it the tool is a fact.
"""
import asyncio
import atexit
import importlib.util
import os
import shutil as _shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_gdmut_"))
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

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

ROOT = mod.GDRIVE_ROOT


# --- 1. the markers come out of the reply, and out of what the user sees ----
text = ("Sudah saya cek, ada dua file lama di luar folder Laporan.\n"
        "GDRIVE_DELETE: bscloud/lama-1.html\n"
        "GDRIVE_MOVE: bscloud/baru.html -> bscloud/Laporan/baru.html\n"
        "Silakan konfirmasi.")
clean, ops = mod.extract_gdrive_mutations(text)
check("a delete marker is picked up", {"op": "delete", "path": "bscloud/lama-1.html"} in ops)
check("a move marker is picked up, with both paths",
      {"op": "move", "path": "bscloud/baru.html",
       "to": "bscloud/Laporan/baru.html"} in ops)
check("...and neither marker is left in what the user reads",
      "GDRIVE_DELETE" not in clean and "GDRIVE_MOVE" not in clean)
check("...while the rest of the reply survives intact",
      "dua file lama" in clean and "Silakan konfirmasi" in clean)

clean2, ops2 = mod.extract_gdrive_mutations("just an ordinary answer")
check("an ordinary reply produces no operations", ops2 == [] and clean2 == "just an ordinary answer")


# --- 2. paths outside our own folder are refused ---------------------------
# drive.file already hides the account's other files -- that is Google's
# boundary. This is ours, and the two together are what make "cannot touch
# anything it did not put there" true rather than merely likely.
check("a plain relative path resolves inside the shared root",
      mod._gdrive_safe_path("bscloud/x.html") == f"{ROOT}/bscloud/x.html")
check("...and a path already written out from the root is accepted once",
      mod._gdrive_safe_path(f"{ROOT}/bscloud/x.html") == f"{ROOT}/bscloud/x.html")
for bad in ("../../etc/passwd", "bscloud/../../../secret", "~/private.txt", "", "   ", "/"):
    check(f"refused: {bad!r}", mod._gdrive_safe_path(bad) is None)


# --- 3. the operation itself -----------------------------------------------
def rclone_stub(rc=0, err=""):
    calls = []
    def run(*args, **kw):
        calls.append(list(args))
        return SimpleNamespace(returncode=rc, stdout="", stderr=err)
    return run, calls

run, calls = rclone_stub()
with patch.object(mod, "_rclone_run", side_effect=run):
    ok, detail = mod.gdrive_mutate("gdrive", {"op": "delete", "path": "bscloud/a.html"})
check("a delete succeeds", ok and detail == "gdrive_deleted")
check("...using deletefile, NOT delete -- `delete` on a path that turns out "
      "to be a directory empties the whole thing, and one word must not be "
      "what stands between removing a report and removing a folder of them",
      calls and calls[0][0] == "deletefile")
check("...aimed inside the shared root", calls and ROOT in calls[0][1])

run, calls = rclone_stub()
with patch.object(mod, "_rclone_run", side_effect=run):
    ok, detail = mod.gdrive_mutate("gdrive", {"op": "move", "path": "a.html",
                                              "to": "Laporan/a.html"})
check("a move succeeds", ok and detail == "gdrive_moved")
check("...via moveto, with both ends inside the root",
      calls and calls[0][0] == "moveto" and ROOT in calls[0][1] and ROOT in calls[0][2])

run, calls = rclone_stub(rc=1, err="not found")
with patch.object(mod, "_rclone_run", side_effect=run):
    ok, detail = mod.gdrive_mutate("gdrive", {"op": "delete", "path": "gone.html"})
check("an rclone failure is reported, not swallowed", not ok and "not found" in detail)

with patch.object(mod, "_rclone_run",
                  side_effect=AssertionError("must not run rclone for a bad path")):
    ok, detail = mod.gdrive_mutate("gdrive", {"op": "delete", "path": "../escape"})
check("a path outside the root never reaches rclone at all",
      not ok and detail == "gdrive_path_refused")


# --- 4. nothing happens without a PIN --------------------------------------
def upd():
    msg = SimpleNamespace(text="", caption=None, photo=None, document=None,
                          reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=111),
                           effective_chat=SimpleNamespace(id=111, type="private", title=None))

def ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), args=[])

def sent(u):
    return u.message.reply_text.call_args[0][0] if u.message.reply_text.call_args else ""


async def main():
    OPS = [{"op": "delete", "path": "bscloud/a.html"}]

    with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]), \
         patch.object(mod, "pin_is_set", return_value=True), \
         patch.object(mod, "request_pin", new=AsyncMock()) as rp, \
         patch.object(mod, "gdrive_mutate",
                      side_effect=AssertionError("must not act before the PIN")):
        u = upd()
        await mod._offer_gdrive_mutations(u, ctx(), OPS)
    check("a delete goes to the PIN keypad, and nothing is touched first",
          rp.await_count == 1)
    check("...under its own action name", rp.await_args[0][1] == "gdrive_mutate")
    check("...carrying exactly what was asked for",
          rp.await_args[0][2] == {"ops": OPS})
    card = rp.await_args[0][3]
    check("...and the card names the file, so the person approves something "
          "specific rather than a category", "bscloud/a.html" in card)
    check("...and says plainly that this cannot be undone from here",
          "undone" in card.lower() or "dibatalkan" in card.lower())

    # No PIN configured: refuse outright rather than doing it unprotected.
    with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]), \
         patch.object(mod, "pin_is_set", return_value=False), \
         patch.object(mod, "request_pin",
                      side_effect=AssertionError("no PIN to ask for")), \
         patch.object(mod, "gdrive_mutate",
                      side_effect=AssertionError("must not act without a PIN")):
        u = upd()
        await mod._offer_gdrive_mutations(u, ctx(), OPS)
    check("with no PIN set, it refuses instead of deleting unprotected -- the "
          "opposite of /addserver, which only warns, because this one cannot "
          "be taken back", "/setpin" in sent(u))

    # An out-of-root path is refused BEFORE a PIN is requested: a card for
    # something that would be rejected afterwards wastes the entry.
    with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]), \
         patch.object(mod, "pin_is_set", return_value=True), \
         patch.object(mod, "request_pin", new=AsyncMock()) as rp:
        u = upd()
        await mod._offer_gdrive_mutations(u, ctx(),
                                          [{"op": "delete", "path": "../../etc/passwd"}])
    check("an out-of-root path is refused before the PIN is even asked for",
          rp.await_count == 0 and "Refused" in sent(u) or "Menolak" in sent(u))

    # Nothing connected: say so rather than failing obscurely.
    with patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
         patch.object(mod, "request_pin", new=AsyncMock()) as rp:
        u = upd()
        await mod._offer_gdrive_mutations(u, ctx(), OPS)
    check("with no Drive account connected, it says so",
          rp.await_count == 0 and "connectgdrive" in sent(u))

    # --- 5. the model is told, or it will never use any of this ------------
    for tpl in ("GEMINI.md.template", "SOUL.md.template"):
        brief = (Path(SRC).parent / tpl).read_text(encoding="utf-8")
        check(f"{tpl} tells the model the markers exist",
              "GDRIVE_DELETE:" in brief and "GDRIVE_MOVE:" in brief)
        check(f"...and that {tpl.split('.')[0]} must not claim it deleted anything itself",
              "have not" in brief or "not tell the user you have deleted" in brief)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
