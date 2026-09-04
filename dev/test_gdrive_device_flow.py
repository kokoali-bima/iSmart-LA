#!/usr/bin/env python3
"""Tests for connecting Google Drive through Google's OAuth device flow --
open a URL, type a code -- instead of running `rclone authorize` in a terminal
on your own PC and pasting the token back.

Feasibility was checked against Google's own documentation before any of this
was built, because two facts decide whether it can work at all:

  * the device flow supports only a LIMITED scope list, and of the Drive
    scopes only drive.appdata and drive.file are on it (full "drive" is not).
    drive.file is what connect_gdrive_account() already asked for, so nothing
    about the bot's reach changes -- only how the token is obtained.
  * drive.file is classed NON-SENSITIVE, so the operator's own OAuth client
    needs no Google review to be published. That matters: an unpublished
    client sits in "Testing", where Google expires the refresh token after
    7 days -- and no amount of keep-alive can defeat that, since it is a
    revocation, and refreshing requires a live refresh token.

Live-tested against the real endpoint too: rclone's own built-in client id
comes back "invalid_client / Invalid client type", which is exactly the
mistake this setup invites and reads like a wrong id rather than a wrong
TYPE. That translation is asserted here.
"""
import atexit
import shutil as _shutil
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_gdrive_"))
# Tests must not litter the machine they run on: 485 stale
# isla_* directories were found on a real server after a few days
# of runs. Registered rather than done at the end, so a failing
# assertion still cleans up.
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

mod.GDRIVE_CLIENT_FILE = scratch / "gdrive_oauth_client.json"
mod.LEDGER_FILE = scratch / "spend.jsonl"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


# --- 1. the client credential ----------------------------------------------
check("no client configured reads as empty, not a crash", mod.read_gdrive_client() == {})
mod.write_gdrive_client("123.apps.googleusercontent.com", "secret")
check("a stored client reads back", mod.read_gdrive_client()["client_id"].startswith("123."))
if os.name != "nt":
    check("...and is owner-only on disk -- it is a credential, like the bot token",
          oct(mod.GDRIVE_CLIENT_FILE.stat().st_mode)[-3:] == "600")
else:
    print("SKIP - POSIX file modes are not meaningful on Windows")

mod.GDRIVE_CLIENT_FILE.write_text("{ not json")
check("a corrupt client file degrades to 'not set up' instead of raising",
      mod.read_gdrive_client() == {})
mod.write_gdrive_client("123.apps.googleusercontent.com", "secret")

# --- 2. the scope actually requested ----------------------------------------
check("the scope requested is drive.file -- the only Drive scope the device "
      "flow supports, and already what rclone was configured with",
      mod.GDRIVE_DEVICE_SCOPE.endswith("/auth/drive.file"))
src = Path(SRC).read_text(encoding="utf-8")
check("...and the rclone remote is still created with scope=drive.file, so "
      "the device flow changed how the token is obtained, not what it can reach",
      "scope=drive.file" in src)

# --- 3. the wrong-client-TYPE message, the mistake this setup invites -------
with patch.object(mod, "_post_form", return_value=(401, {"error": "invalid_client",
                                                         "error_description": "Invalid client type."})):
    ok, body = mod.gdrive_device_start()
check("a Desktop/Web client is refused", not ok)
check("...with a message naming the real problem (wrong TYPE), not Google's "
      "'Invalid client type' which reads like a wrong id",
      "TV and Limited Input" in body["error_description"])

# --- 4. poll states map to the right decisions -----------------------------
CASES = {
    "authorization_pending": "pending",
    "slow_down": "slow_down",
    "access_denied": "denied",
    "expired_token": "expired",
    "something_else": "error",
}
for err, want in CASES.items():
    with patch.object(mod, "_post_form", return_value=(400, {"error": err})):
        got, _ = mod.gdrive_device_poll_once("dc")
    check(f"poll: '{err}' -> {want}", got == want)

with patch.object(mod, "_post_form",
                  return_value=(200, {"access_token": "ya29.x", "refresh_token": "1//r",
                                      "expires_in": 3599, "token_type": "Bearer"})):
    got, payload = mod.gdrive_device_poll_once("dc")
check("poll: an approved sign-in -> ok, with the token", got == "ok" and payload["access_token"])

# --- 5. THE conversion bug this would otherwise ship with -------------------
# Google returns a RELATIVE lifetime; rclone stores an ABSOLUTE expiry. Handing
# rclone the raw reply leaves it with no expiry, so it keeps presenting a dead
# access token instead of refreshing: works at first, fails an hour later.
raw = mod.gdrive_token_to_rclone({"access_token": "ya29.x", "refresh_token": "1//r",
                                  "expires_in": 3599, "token_type": "Bearer"})
tok = json.loads(raw)
check("the token handed to rclone carries an ABSOLUTE expiry, not Google's "
      "relative expires_in", "expiry" in tok and "expires_in" not in tok)
check("...that parses as a real timestamp", len(tok["expiry"]) >= 19 and "T" in tok["expiry"])
check("...and keeps the refresh token, without which it dies in an hour",
      tok["refresh_token"] == "1//r")
check("...and is exactly the shape connect_gdrive_account() already accepts "
      "(access_token present), so both paths share one verified code path",
      "access_token" in json.loads(raw))


# --- 6. the wizard ----------------------------------------------------------
def upd(uid=111, chat=111):
    msg = SimpleNamespace(text="", reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(id=chat, type="private", title=None))

def ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock(),
                                               send_message=AsyncMock()), args=[])

def sent(u):
    return u.message.reply_text.call_args[0][0] if u.message.reply_text.call_args else ""


async def main():
    # No client yet -> the setup card, naming the exact client type and the
    # Publish step (skipping it is what causes the 7-day expiry).
    mod.GDRIVE_CLIENT_FILE.unlink(missing_ok=True)
    # The DEFAULT with no client configured is now rclone's own verified
    # sign-in, which needs no Google Cloud project at all -- creating one
    # turned out to mean a branding page plus three published URLs, which
    # stopped a real operator dead. The own-client setup card below is still
    # available, it is just no longer the thing everyone has to walk into.
    with patch.object(mod, "_may_authorize_group_action", new=AsyncMock(return_value=True)), \
         patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
         patch.object(mod, "gdrive_rclone_start",
                      return_value=(True, "https://accounts.google.com/o/oauth2/auth?x=1",
                                    {"pid": 1, "state": "S", "log": "/x"})):
        u = upd()
        await mod.cmd_connectgdrive(u, ctx())
    check("with no OAuth client, /connectgdrive uses rclone's own sign-in "
          "rather than demanding a Cloud project be created",
          "accounts.google.com" in sent(u))
    mod._gdrive_wizard.pop(111, None)

    txt = mod._gdrive_client_setup_instructions("en")
    check("the own-client setup card is still available for anyone who wants "
          "their own OAuth app", "OAuth client" in txt or "client" in txt.lower())
    check("...naming the exact client type Google requires",
          "TV and Limited Input devices" in txt)
    check("...and the Publish step, whose omission causes the 7-day expiry",
          "Publish" in txt and "7" in txt)

    # A client that isn't a Google client id is refused before being stored.
    mod._gdrive_wizard[111] = {"step": "await_gdrive_client", "name": "gdrive",
                               "expires": mod._dt.datetime.now().timestamp() + 900}
    u = upd(); u.message.text = "not-a-client-id alsonot"
    with patch.object(mod, "_gdrive_begin_device", new=AsyncMock()) as begin:
        await mod._handle_gdrive_wizard_input(u, ctx())
    check("a malformed client id is refused rather than stored",
          begin.await_count == 0 and not mod.GDRIVE_CLIENT_FILE.exists())

    # A well-formed one is stored and goes straight to the device card.
    u = upd(); u.message.text = "9876.apps.googleusercontent.com topsecret"
    with patch.object(mod, "_gdrive_begin_device", new=AsyncMock()) as begin:
        await mod._handle_gdrive_wizard_input(u, ctx())
    check("a valid client id is stored and the device sign-in starts at once",
          begin.await_count == 1 and mod.read_gdrive_client()["client_secret"] == "topsecret")
    mod._gdrive_wizard.pop(111, None)

    # The card itself: a URL and a code, and no instruction to open a terminal.
    with patch.object(mod, "gdrive_device_start",
                      return_value=(True, {"device_code": "dc", "user_code": "ABCD-EFGH",
                                           "verification_url": "https://google.com/device",
                                           "expires_in": 900, "interval": 5})), \
         patch.object(mod, "_gdrive_device_wait", new=AsyncMock()):
        mod._gdrive_wizard[111] = {"step": "x", "name": "gdrive",
                                   "expires": mod._dt.datetime.now().timestamp() + 900}
        u = upd()
        await mod._gdrive_begin_device(u, ctx(), "en", "gdrive")
    card = sent(u)
    check("the sign-in card shows the code to type", "ABCD-EFGH" in card)
    check("...and the URL to open", "google.com/device" in card)
    check("...and says no terminal is needed -- the whole point of this change",
          "terminal" in card.lower())
    check("...and nothing has to be pasted back", "paste back" in card.lower())

    # A token with no refresh_token must be refused, not stored: it would work
    # for an hour and then quietly stop.
    mod._gdrive_wizard[111] = {"step": "device_pending", "name": "gdrive",
                               "expires": mod._dt.datetime.now().timestamp() + 900}
    c = ctx()
    with patch.object(mod, "gdrive_device_poll_once",
                      return_value=("ok", {"access_token": "ya29.x", "expires_in": 3599})), \
         patch.object(mod, "connect_gdrive_account") as connect, \
         patch.object(mod.asyncio, "sleep", new=AsyncMock()):
        await mod._gdrive_device_wait(c, 111, "gdrive", "en",
                                      {"device_code": "dc", "interval": 0, "expires_in": 900})
    said = c.bot.send_message.call_args[0][1] if c.bot.send_message.call_args else ""
    check("a token with NO refresh token is refused, not stored -- it would "
          "work for an hour then quietly stop", connect.call_count == 0)
    check("...and says how to fix it (revoke, then retry)", "revoke" in said.lower())

    # A refusal in the browser ends the wait cleanly.
    mod._gdrive_wizard[111] = {"step": "device_pending", "name": "gdrive",
                               "expires": mod._dt.datetime.now().timestamp() + 900}
    c = ctx()
    with patch.object(mod, "gdrive_device_poll_once", return_value=("denied", {})), \
         patch.object(mod.asyncio, "sleep", new=AsyncMock()):
        await mod._gdrive_device_wait(c, 111, "gdrive", "en",
                                      {"device_code": "dc", "interval": 0, "expires_in": 900})
    check("declining in the browser ends the wait and clears the wizard",
          111 not in mod._gdrive_wizard)

    # Cancelling stops the poll rather than leaving it running.
    mod._gdrive_wizard.pop(111, None)
    c = ctx()
    with patch.object(mod, "gdrive_device_poll_once",
                      side_effect=AssertionError("must not poll after cancel")), \
         patch.object(mod.asyncio, "sleep", new=AsyncMock()):
        await mod._gdrive_device_wait(c, 111, "gdrive", "en",
                                      {"device_code": "dc", "interval": 0, "expires_in": 900})
    check("cancelling stops the poll instead of leaving it running", True)

    # --- 7. the health check -----------------------------------------------
    with patch.object(mod, "_rclone_run",
                      return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
        ok, detail = mod.check_gdrive_account("gdrive")
    check("a working remote reports healthy", ok and detail == "ok")

    with patch.object(mod, "_rclone_run",
                      return_value=SimpleNamespace(returncode=1, stdout="",
                                                   stderr="couldn't fetch token: invalid_grant")):
        ok, detail = mod.check_gdrive_account("gdrive")
    check("a dead sign-in is reported as such, with what to do about it",
          not ok and "connectgdrive" in detail)

    # --- the three ways this got someone stuck in a real session ----------
    # Reported as "ga bisa2 nambah gdrive": /gdrive told them to ask an
    # operator to do it on the host, /connectgdrive answered "already
    # connecting one" forever, and the client-id paste kept being rejected.
    mod._gdrive_wizard.clear()
    mod.GDRIVE_CLIENT_FILE.unlink(missing_ok=True)

    with patch.object(mod, "_may_authorize_group_action", new=AsyncMock(return_value=True)), \
         patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
         patch.object(mod, "_gdrive_begin_rclone", new=AsyncMock()):
        u = upd(); await mod.cmd_gdrive(u, ctx())
    empty = sent(u)
    check("/gdrive with no account points at /connectgdrive, instead of telling "
          "the operator to go and do it on the host (stale since v0.2b.54)",
          "/connectgdrive" in empty)
    check("...and does not still claim it cannot be done through Telegram",
          "not through Telegram" not in empty and "bukan lewat Telegram" not in empty)

    # An abandoned wizard must not lock the command out.
    with patch.object(mod, "_may_authorize_group_action", new=AsyncMock(return_value=True)), \
         patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
         patch.object(mod, "_gdrive_begin_rclone", new=AsyncMock()):
        mod._gdrive_wizard[111] = {"step": "await_gdrive_client", "name": "gdrive",
                                   "expires": mod._dt.datetime.now().timestamp() + 900}
        u = upd(); await mod.cmd_connectgdrive(u, ctx())
    check("re-running /connectgdrive after abandoning it RESTARTS instead of "
          "refusing -- the refusal left people stuck for 15 minutes with no "
          "way forward", "already" not in sent(u).lower() and "sedang" not in sent(u).lower())

    # ...but a sign-in already waiting on Google is worth protecting.
    with patch.object(mod, "_may_authorize_group_action", new=AsyncMock(return_value=True)), \
         patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
         patch.object(mod, "_gdrive_begin_rclone", new=AsyncMock()):
        mod._gdrive_wizard[111] = {"step": "device_pending", "name": "gdrive",
                                   "expires": mod._dt.datetime.now().timestamp() + 900}
        u = upd(); await mod.cmd_connectgdrive(u, ctx())
    check("...while a sign-in already awaiting approval is NOT thrown away",
          "waiting" in sent(u).lower() or "menunggu" in sent(u).lower())
    mod._gdrive_wizard.clear()

    # The client paste, in every shape someone actually copies it.
    CID = "1234567890-abcdef.apps.googleusercontent.com"
    for label, msg in (
        ("id then secret, space separated", f"{CID} GOCSPX-secret"),
        ("secret FIRST, reversed", f"GOCSPX-secret {CID}"),
        ("on two lines", CID + "\n" + "GOCSPX-secret"),
        ("with the console's labels", f"Client ID: {CID} Client secret: GOCSPX-secret"),
    ):
        mod.GDRIVE_CLIENT_FILE.unlink(missing_ok=True)
        mod._gdrive_wizard[111] = {"step": "await_gdrive_client", "name": "gdrive",
                                   "expires": mod._dt.datetime.now().timestamp() + 900}
        u = upd(); u.message.text = msg
        with patch.object(mod, "_gdrive_begin_device", new=AsyncMock()) as begin:
            await mod._handle_gdrive_wizard_input(u, ctx())
        stored = mod.read_gdrive_client()
        check(f"client paste accepted: {label}",
              begin.await_count == 1 and stored.get("client_id") == CID
              and stored.get("client_secret") == "GOCSPX-secret")

    # And across two messages, keeping the id rather than discarding it.
    mod.GDRIVE_CLIENT_FILE.unlink(missing_ok=True)
    mod._gdrive_wizard[111] = {"step": "await_gdrive_client", "name": "gdrive",
                               "expires": mod._dt.datetime.now().timestamp() + 900}
    u = upd(); u.message.text = CID
    with patch.object(mod, "_gdrive_begin_device", new=AsyncMock()) as begin:
        await mod._handle_gdrive_wizard_input(u, ctx())
    check("sending only the ID asks for the secret next, instead of rejecting "
          "and throwing the ID away",
          begin.await_count == 0 and "secret" in sent(u).lower())
    u2 = upd(); u2.message.text = "GOCSPX-secret"
    with patch.object(mod, "_gdrive_begin_device", new=AsyncMock()) as begin:
        await mod._handle_gdrive_wizard_input(u2, ctx())
    check("...and the secret in the NEXT message completes it",
          begin.await_count == 1
          and mod.read_gdrive_client().get("client_id") == CID)

    # Something that is not a client id at all still gets a useful answer.
    mod.GDRIVE_CLIENT_FILE.unlink(missing_ok=True)
    mod._gdrive_wizard[111] = {"step": "await_gdrive_client", "name": "gdrive",
                               "expires": mod._dt.datetime.now().timestamp() + 900}
    u = upd(); u.message.text = "hello what do you want"
    with patch.object(mod, "_gdrive_begin_device", new=AsyncMock()) as begin:
        await mod._handle_gdrive_wizard_input(u, ctx())
    check("junk input is refused with the suffix to look for, not stored",
          begin.await_count == 0 and "apps.googleusercontent.com" in sent(u))

    # --- access_denied is ambiguous, and the ambiguity misdirects ---------
    # Google returns access_denied both when someone declines AND when the app
    # is still in "Testing", where it blocks every account not on the
    # test-user list -- the developer's own included. Reported live as
    # "Akses diblokir ... Error 403: access_denied" from an operator who had
    # declined nothing. A message saying only "you declined it" sends them
    # looking in exactly the wrong place.
    mod._gdrive_wizard[111] = {"step": "device_pending", "name": "gdrive",
                               "expires": mod._dt.datetime.now().timestamp() + 900}
    c = ctx()
    with patch.object(mod, "gdrive_device_poll_once", return_value=("denied", {})),          patch.object(mod.asyncio, "sleep", new=AsyncMock()):
        await mod._gdrive_device_wait(c, 111, "gdrive", "en",
                                      {"device_code": "dc", "interval": 0, "expires_in": 900})
    said = c.bot.send_message.call_args[0][1] if c.bot.send_message.call_args else ""
    check("access_denied names the Testing-status cause, not only 'you declined'",
          "Testing" in said)
    check("...and gives the exact fix, in the console's CURRENT menu names",
          "Publish app" in said and "Audience" in said)

    setup = mod._gdrive_client_setup_instructions("en")
    check("the setup card also uses the current console path, not the "
          "renamed-away 'OAuth consent screen'",
          "Audience" in setup and "OAuth consent screen" not in setup)
    check("...and warns that skipping Publish causes access_denied, so the "
          "error is recognisable when it happens", "access_denied" in setup)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
