#!/usr/bin/env python3
"""Tests for connecting Google Drive with no Google Cloud project at all.

The device flow shipped in v0.2b.54 removed the terminal, but not the hard
part. Setting up your own OAuth client turned out to mean: create a Cloud
project, pick client type "TV and Limited Input devices", enable the Drive
API, fill a Branding page, supply a homepage AND a privacy policy AND a terms
of service URL, then press Publish -- and skipping Publish gets you
"Error 403: access_denied" from your own account. A real operator was stopped
at each of those in turn. For an ordinary user it is not a setup step, it is
a wall.

rclone ships a Google OAuth client of its own, already published and verified.
Using it means **no Cloud project, no branding, no publishing, and no 7-day
test-user token expiry** -- and it is what `rclone authorize` has always done.

The mechanism, verified end to end on a live host before this was written:

  * `rclone authorize drive --auth-no-open-browser` prints a LOCAL link, not a
    Google one, and listens on 127.0.0.1:53682.
  * Fetching that local /auth path returns a 307 whose Location IS the real
    Google consent URL -- confirmed to carry rclone's client_id
    (202264815644...) and the drive.file scope. That URL works on a phone.
  * Google then redirects the phone to 127.0.0.1, which the phone cannot
    reach -- but the address bar holds the code.
  * Replaying that same path against THIS host's listener hands the code to
    the waiting rclone: it answers "Success! All done." and proceeds to the
    token exchange (a deliberately fake code produced exactly the expected
    "invalid_grant").

So the whole thing becomes what signing in to Gemini or Claude already is
here: open a link, paste something back.
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
scratch = Path(tempfile.mkdtemp(prefix="isla_rcauth_t_"))
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
mod.GDRIVE_CLIENT_FILE = scratch / "gdrive_oauth_client.json"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


# --- 1. the code, from whatever shape the person actually pasted -----------
# They are copying out of a phone address bar showing a failed page, so
# accepting only one shape would fail them at the very last step having done
# everything right.
CASES = {
    "http://127.0.0.1:53682/?state=xy&code=4/0AbCdEf&scope=x": "4/0AbCdEf",
    "?state=xy&code=4/0AbCdEf": "4/0AbCdEf",
    "4/0AbCdEf": "4/0AbCdEf",
    "  4/0AbCdEf  ": "4/0AbCdEf",
    "http://127.0.0.1:53682/?code=4%2F0Enc": "4/0Enc",
    "hello what do you want": "",
    "": "",
}
for pasted, want in CASES.items():
    got = mod._extract_oauth_code(pasted)
    check(f"code extracted from {pasted[:38]!r} -> {want or '(refused)'}", got == want)


# --- 2. a host without rclone provisions it rather than refusing ----------
# Reported live on a deployment with no rclone: "Tidak bisa memulai sign-in:
# rclone isn't installed on this host. Install it with: curl ... | sudo bash".
# Telling the operator to SSH in and pipe a script into sudo is exactly the
# friction this was asked to remove -- "user biasa akan kesulitan". rclone is
# one static binary, so the bot fetches its own copy into BASE_DIR/bin: no
# sudo, nothing outside the install, and deleting bin/ undoes it.
SRC_TEXT = Path(SRC).read_text(encoding="utf-8")
check("provisioning exists, rather than only an instruction to go and install",
      hasattr(mod, "ensure_rclone"))
check("...into the install's own directory, not a system path",
      "RCLONE_LOCAL_BIN = BASE_DIR" in SRC_TEXT)

with patch.object(mod, "_rclone_path", return_value="/usr/bin/rclone"):
    ok, where = mod.ensure_rclone()
check("a host that already has rclone keeps using it, with no download",
      ok and where == "/usr/bin/rclone")

with patch.object(mod, "_rclone_path", return_value=None), \
     patch.object(mod.sys, "platform", "darwin"):
    ok, msg = mod.ensure_rclone()
check("a platform it cannot fetch for says so AND still gives the install "
      "command", not ok and "rclone.org/install.sh" in msg)

with patch.object(mod, "ensure_rclone", return_value=(False, "no rclone here")):
    ok, msg, handle = mod.gdrive_rclone_start()
check("the sign-in reports a provisioning failure instead of crashing",
      not ok and "no rclone" in msg)

check("every rclone call uses the RESOLVED path, so a fetched copy that is "
      "not on PATH still works for uploads and /gdrivestatus",
      "_rclone_path() or RCLONE_BIN" in SRC_TEXT)


# --- 3. failure paths in start -------------------------------------------
class _Proc:
    def __init__(self, rc=None):
        self.pid = 424242
        self._rc = rc
    def poll(self):
        return self._rc
    def kill(self):
        pass

with patch.object(mod, "ensure_rclone", return_value=(True, "/usr/bin/rclone")), \
     patch.object(mod.subprocess, "run"), \
     patch.object(mod.subprocess, "Popen", return_value=_Proc(rc=1)), \
     patch.object(mod.time, "sleep"):
    ok, msg, _ = mod.gdrive_rclone_start()
check("rclone exiting immediately is reported, not waited out", not ok)

with patch.object(mod, "ensure_rclone", return_value=(True, "/usr/bin/rclone")), \
     patch.object(mod.subprocess, "run"), \
     patch.object(mod.subprocess, "Popen", return_value=_Proc()), \
     patch.object(mod.time, "sleep"), \
     patch.object(mod.time, "time", side_effect=[0, 0, 999, 999, 999]):
    ok, msg, _ = mod.gdrive_rclone_start()
check("no sign-in link in time is reported rather than hanging", not ok)


# --- 4. a stale authorize is cleared before starting ----------------------
# It still owns port 53682, and the next attempt would fail with nothing on
# screen explaining why.
killed = []
with patch.object(mod, "ensure_rclone", return_value=(True, "/usr/bin/rclone")), \
     patch.object(mod.subprocess, "run", side_effect=lambda *a, **k: killed.append(a)), \
     patch.object(mod.subprocess, "Popen", return_value=_Proc(rc=1)), \
     patch.object(mod.time, "sleep"):
    mod.gdrive_rclone_start()
check("a leftover 'rclone authorize' is killed first, so the port is free",
      any("pkill" in str(a) for a in killed))


# --- 5. cleanup removes a file holding a LIVE refresh token ---------------
log_dir = Path(tempfile.mkdtemp(prefix="isla_rc_log_"))
log = log_dir / "authorize.log"
log.write_text('{"access_token":"x","refresh_token":"LIVE"}')
mod.gdrive_rclone_cleanup({"pid": None, "log": str(log)})
check("the authorize log is deleted -- it holds a working refresh token until "
      "it is gone", not log.exists())
mod.gdrive_rclone_cleanup({"pid": None, "log": ""})
check("cleanup with nothing to clean does not raise", True)


# --- 6. the flow, from Telegram's side ------------------------------------
def upd(text=""):
    msg = SimpleNamespace(text=text, caption=None, photo=None, document=None,
                          reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=111),
                           effective_chat=SimpleNamespace(id=111, type="private", title=None))

def ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock(),
                                               send_message=AsyncMock()), args=[])

def sent(u):
    return u.message.reply_text.call_args[0][0] if u.message.reply_text.call_args else ""

GOOGLE_URL = ("https://accounts.google.com/o/oauth2/auth?client_id="
              "202264815644.apps.googleusercontent.com&scope=drive.file&state=S1")


async def main():
    mod._gdrive_wizard.clear()

    # With no OAuth client of their own, /connectgdrive takes the rclone path.
    with patch.object(mod, "_may_authorize_group_action", new=AsyncMock(return_value=True)), \
         patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
         patch.object(mod, "gdrive_rclone_start",
                      return_value=(True, GOOGLE_URL, {"pid": 1, "state": "S1", "log": "/x"})):
        u = upd()
        await mod.cmd_connectgdrive(u, ctx())
    card = sent(u)
    check("with no Cloud project set up, /connectgdrive uses rclone's own "
          "verified sign-in instead of demanding one be created",
          "accounts.google.com" in card)
    check("...and says no Google Cloud project is needed, since that is the "
          "wall this removes", "Google Cloud" in card)
    check("...warning that the page will FAIL to load, which is the step that "
          "otherwise looks like something went wrong",
          "fails to load" in card or "gagal dimuat" in card)
    check("...and telling them to copy the address bar",
          "address bar" in card or "address bar" in card.lower() or "alamat" in card)
    check("the wizard is waiting for the pasted code",
          mod._gdrive_wizard.get(111, {}).get("step") == "await_rclone_code")

    # Pasting the redirect completes it, through the SAME verified path a
    # pasted token already took.
    with patch.object(mod, "gdrive_rclone_finish",
                      return_value=(True, '{"access_token":"a","refresh_token":"r"}')), \
         patch.object(mod, "connect_gdrive_account",
                      return_value=(True, "verified")) as conn, \
         patch.object(mod, "gdrive_rclone_cleanup") as clean:
        u2 = upd("http://127.0.0.1:53682/?state=S1&code=4/0Real")
        await mod._handle_gdrive_wizard_input(u2, ctx())
    check("pasting the failed-page address finishes the connection",
          conn.call_count == 1)
    check("...through connect_gdrive_account(), so verification, the "
          "duplicate-folder guard and rollback all still apply",
          conn.call_args[0][0] == "gdrive")
    check("...and the token log is cleaned up afterwards", clean.call_count == 1)
    check("...and the wizard is finished", 111 not in mod._gdrive_wizard)

    # A bad paste keeps the wizard alive: the link is still good, and sending
    # them back to the start over a typo would be its own small cruelty.
    mod._gdrive_wizard[111] = {"step": "await_rclone_code", "name": "gdrive",
                               "handle": {"state": "S1", "log": "/x"},
                               "expires": mod._dt.datetime.now().timestamp() + 900}
    with patch.object(mod, "gdrive_rclone_finish",
                      return_value=(False, "I couldn't find a code in that.")), \
         patch.object(mod, "connect_gdrive_account",
                      side_effect=AssertionError("must not connect on a bad paste")):
        u3 = upd("oops wrong thing")
        await mod._handle_gdrive_wizard_input(u3, ctx())
    check("a bad paste is explained and the same link stays usable",
          111 in mod._gdrive_wizard and "code" in sent(u3).lower())

    # Someone who HAS set up their own client keeps the device flow.
    mod._gdrive_wizard.clear()
    mod.write_gdrive_client("9.apps.googleusercontent.com", "s")
    with patch.object(mod, "_may_authorize_group_action", new=AsyncMock(return_value=True)), \
         patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
         patch.object(mod, "_gdrive_begin_device", new=AsyncMock()) as dev, \
         patch.object(mod, "_gdrive_begin_rclone", new=AsyncMock()) as rc:
        await mod.cmd_connectgdrive(upd(), ctx())
    check("an operator who already set up their own OAuth client still gets "
          "the device flow -- this adds a path, it does not remove one",
          dev.await_count == 1 and rc.await_count == 0)
    mod.GDRIVE_CLIENT_FILE.unlink(missing_ok=True)

    # --- a saved client Google says is gone must not be a one-way trap ----
    # Its mere presence routes every /connectgdrive down the device path, and
    # nothing in Telegram could remove it. Reported live: "Google menolak
    # memulai sign-in: The OAuth client was deleted" -- on repeat, with the
    # rclone route (which needs no Cloud project at all) unreachable behind it.
    for body in ({"error": "deleted_client"},
                 {"error_description": "The OAuth client was deleted."},
                 {"error": "invalid_client"},
                 {"error": "unauthorized_client"}):
        check(f"recognised as a dead client: {body}", mod._client_is_dead(body))
    for body in ({"error": "slow_down"}, {"error": "network"}, {}):
        check(f"NOT a dead client: {body} -- a blip must not throw away a "
              f"working client", not mod._client_is_dead(body))

    mod.write_gdrive_client("dead.apps.googleusercontent.com", "s")
    mod._gdrive_wizard[111] = {"step": "x", "name": "gdrive",
                               "expires": mod._dt.datetime.now().timestamp() + 900}
    with patch.object(mod, "gdrive_device_start",
                      return_value=(False, {"error": "deleted_client",
                                            "error_description": "The OAuth client was deleted."})), \
         patch.object(mod, "_gdrive_begin_rclone", new=AsyncMock()) as rc:
        u = upd()
        await mod._gdrive_begin_device(u, ctx(), "en", "gdrive")
    check("a deleted client is cleared instead of being retried forever",
          not mod.GDRIVE_CLIENT_FILE.exists())
    check("...and the sign-in continues on the route that needs no Cloud "
          "project, rather than dead-ending", rc.await_count == 1)
    check("...saying what happened", "removed it" in sent(u))

    # A transient failure must NOT discard a client that is probably fine.
    mod.write_gdrive_client("live.apps.googleusercontent.com", "s")
    mod._gdrive_wizard[111] = {"step": "x", "name": "gdrive",
                               "expires": mod._dt.datetime.now().timestamp() + 900}
    with patch.object(mod, "gdrive_device_start",
                      return_value=(False, {"error_description": "network"})), \
         patch.object(mod, "_gdrive_begin_rclone",
                      side_effect=AssertionError("must not switch route on a blip")):
        await mod._gdrive_begin_device(upd(), ctx(), "en", "gdrive")
    check("a transient failure keeps the stored client",
          mod.GDRIVE_CLIENT_FILE.exists())
    mod.GDRIVE_CLIENT_FILE.unlink(missing_ok=True)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
