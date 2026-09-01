#!/usr/bin/env python3
"""Tests for /connectgdrive: an explicit, Telegram-driven way to authorise a
Google Drive account, replacing "someone ran a script on the host at some
point, nobody's sure when."

Asked directly why gdrive already had a connected account without ever going
through a Telegram consent flow the way Gemini/Claude's sign-in does. Checking
found the account WAS genuinely working (a real upload, link fetch, and
cleanup all succeeded live) -- but nothing about how it got connected was
visible or auditable from Telegram at all.

Replicating Gemini/Claude's exact "one link, paste a short code" UX turned out
not to be possible: verified live by running `rclone authorize` on the server,
which prints a URL pointing at 127.0.0.1 on the machine running IT --

    please go to the following link: http://127.0.0.1:53682/auth?state=...
    NOTICE: Waiting for code...

-- unlike agy/claude, Google's OAuth for rclone's Drive backend waits for a
network REDIRECT back to a local listener, not a code a human can carry
between devices. So the operator still runs `rclone authorize` once, on a
machine they control with a browser -- but now pastes the resulting JSON
token into Telegram, where the bot does everything that used to be a risky
manual step: picks a collision-free name, never hand-edits rclone.conf's TOML
(uses `rclone config create`, verified live not to disturb existing remotes),
checks for -- and avoids -- a duplicate root folder if the same account gets
connected twice, verifies the connection with a real listing before calling
it done, and rolls back cleanly on any failure so nothing is left
half-configured. All of it logged with who did it and when.
"""
import asyncio, importlib.util, json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="isla_gdriveconnect_")
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

def rc(stdout="", stderr="", code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)

FAKE_TOKEN = json.dumps({"access_token": "a", "refresh_token": "b",
                        "expiry": "2030-01-01T00:00:00Z", "token_type": "Bearer"})

OWNER = 111
def upd(chat_id=OWNER, text="hi"):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock(), delete=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=OWNER),
                           effective_chat=SimpleNamespace(id=chat_id, type="private", title=None))
def ctx():
    return SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock()), args=[])

# --- 1. naming helpers ------------------------------------------------------
with patch.object(mod, "_list_gdrive_accounts", return_value=[]):
    check("first account defaults to the bare name 'gdrive'",
          mod._next_gdrive_default_name() == "gdrive")
with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]):
    check("second account suggests 'gdrive_2'",
          mod._next_gdrive_default_name() == "gdrive_2")
with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive", "gdrive_2"]):
    check("third account suggests 'gdrive_3' (skips the taken one)",
          mod._next_gdrive_default_name() == "gdrive_3")

check("a label sanitizes to a safe remote name",
      mod._sanitize_gdrive_label("Client A!") == "gdrive_Client_A")
check("a label of only unsafe characters yields nothing usable",
      mod._sanitize_gdrive_label("!!!") == "")
check("an empty label yields nothing usable",
      mod._sanitize_gdrive_label("   ") == "")

# --- 2. connect_gdrive_account: input validation ----------------------------
with patch.object(mod, "_list_gdrive_accounts", return_value=[]):
    ok, detail = mod.connect_gdrive_account("gdrive", "not json at all")
check("garbage input is rejected before any rclone call", ok is False)

with patch.object(mod, "_list_gdrive_accounts", return_value=[]):
    ok, detail = mod.connect_gdrive_account("gdrive", json.dumps({"foo": "bar"}))
check("valid JSON with no access_token is rejected", ok is False)

with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]):
    ok, detail = mod.connect_gdrive_account("gdrive", FAKE_TOKEN)
check("a name that already exists is rejected without touching rclone",
      ok is False and "already exists" in detail)

# --- 3. connect_gdrive_account: the happy path, and what it actually calls -
calls = []
def happy(*args, timeout=60):
    calls.append(args)
    if args[:2] == ("config", "create"):
        return rc(code=0)
    if args[:2] == ("lsd",) or args[0] == "lsd":
        # first lsd (folder check) shows the root already there; second lsd
        # (final verification) also succeeds.
        return rc(stdout=mod.GDRIVE_ROOT + "\n", code=0)
    return rc(code=0)

calls.clear()
with patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
     patch.object(mod, "_rclone_run", side_effect=happy):
    ok, detail = mod.connect_gdrive_account("gdrive", FAKE_TOKEN)
check("a fully successful connect reports True", ok is True)
check("...uses 'rclone config create', never hand-writes rclone.conf",
      any(c[:2] == ("config", "create") for c in calls))
check("...the pasted token is passed straight through, unmodified",
      any(f"token={FAKE_TOKEN}" in " ".join(c) for c in calls))
check("...a real verification lsd is the LAST call (verify, don't assume)",
      calls and calls[-1][0] == "lsd")

# --- 4. the folder-collision guard: create only when truly missing --------
calls.clear()
def missing_folder(*args, timeout=60):
    calls.append(args)
    if args[:2] == ("config", "create"):
        return rc(code=0)
    if args[0] == "lsd" and len(calls) == 2:      # first lsd: folder check, empty
        return rc(stdout="SomeOtherFolder\n", code=0)
    if args[0] == "mkdir":
        return rc(code=0)
    return rc(stdout=mod.GDRIVE_ROOT + "\n", code=0)   # final verify

with patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
     patch.object(mod, "_rclone_run", side_effect=missing_folder):
    ok, detail = mod.connect_gdrive_account("gdrive", FAKE_TOKEN)
check("the data folder is created when it's not already there", ok is True)
check("...via 'rclone mkdir', not by assuming it exists",
      any(c[0] == "mkdir" for c in calls))

calls.clear()
def existing_folder(*args, timeout=60):
    calls.append(args)
    if args[:2] == ("config", "create"):
        return rc(code=0)
    return rc(stdout=mod.GDRIVE_ROOT + "\n", code=0)   # every lsd shows it present

with patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
     patch.object(mod, "_rclone_run", side_effect=existing_folder):
    ok, _ = mod.connect_gdrive_account("gdrive", FAKE_TOKEN)
check("no duplicate folder is created when this account already has one "
      "(the exact risk the README's manual check existed to avoid)",
      ok is True and not any(c[0] == "mkdir" for c in calls))

# --- 5. rollback: nothing is left half-configured on any failure ----------
def fails_at_verify(*args, timeout=60):
    if args[:2] == ("config", "create"):
        return rc(code=0)
    if args[0] == "lsd":
        return rc(code=1, stderr="directory not found")
    return rc(code=0)

deleted = []
def track_delete(*args, timeout=60):
    if args[:2] == ("config", "delete"):
        deleted.append(args[2])
        return rc(code=0)
    return fails_at_verify(*args, timeout=timeout)

with patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
     patch.object(mod, "_rclone_run", side_effect=track_delete):
    ok, detail = mod.connect_gdrive_account("gdrive", FAKE_TOKEN)
check("a failed folder check reports failure, not a false success", ok is False)
check("...and the half-configured remote is rolled back via config delete "
      "(THE property this whole design exists for)",
      deleted == ["gdrive"])

def create_fails(*args, timeout=60):
    if args[:2] == ("config", "create"):
        return rc(code=1, stderr="invalid token")
    return rc(code=0)
with patch.object(mod, "_list_gdrive_accounts", return_value=[]), \
     patch.object(mod, "_rclone_run", side_effect=create_fails):
    ok, detail = mod.connect_gdrive_account("gdrive", FAKE_TOKEN)
check("rclone itself rejecting the token is reported plainly", ok is False)

# --- 6. cmd_connectgdrive: first account vs. a labelled second one --------
async def main():
    mod._gdrive_wizard.clear()
    with patch.object(mod, "_list_gdrive_accounts", return_value=[]):
        u = upd()
        await mod.cmd_connectgdrive(u, ctx())
    state = mod._gdrive_wizard.get(OWNER)
    check("first-ever account skips the label step and goes straight to token",
          state is not None and state["step"] == "await_gdrive_token"
          and state["name"] == "gdrive")
    txt = u.message.reply_text.call_args[0][0]
    check("...and the instructions mention rclone authorize",
          "rclone authorize" in txt)
    mod._gdrive_wizard.clear()

    with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]):
        u2 = upd()
        await mod.cmd_connectgdrive(u2, ctx())
    state2 = mod._gdrive_wizard.get(OWNER)
    check("a second account asks for a label first",
          state2 is not None and state2["step"] == "await_gdrive_label"
          and state2["name"] is None)

    with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]):
        u3 = upd()
        await mod.cmd_connectgdrive(u3, ctx())
    refusal = u3.message.reply_text.call_args[0][0].lower()
    check("starting a second connect while one is already pending is refused",
          "already" in refusal or "sedang" in refusal)
    mod._gdrive_wizard.clear()

    # --- 7. the label step, end to end -------------------------------------
    with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]):
        u4 = upd(); await mod.cmd_connectgdrive(u4, ctx())
    u5 = upd(text="ClientA")
    with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]):
        consumed = await mod._handle_wizard_input(u5, ctx())
    state5 = mod._gdrive_wizard.get(OWNER)
    check("a valid label consumes the message and advances to the token step",
          consumed is True and state5 is not None
          and state5["step"] == "await_gdrive_token")
    check("...with the sanitised name recorded", state5["name"] == "gdrive_ClientA")
    mod._gdrive_wizard.clear()

    with patch.object(mod, "_list_gdrive_accounts", return_value=["gdrive"]):
        u6 = upd(); await mod.cmd_connectgdrive(u6, ctx())
    u7 = upd(text="/cancel")
    consumed = await mod._handle_wizard_input(u7, ctx())
    check("/cancel during the label step clears the pending connect",
          consumed is True and OWNER not in mod._gdrive_wizard)

    # --- 8. the token step, end to end -------------------------------------
    mod._gdrive_wizard[OWNER] = {"step": "await_gdrive_token", "name": "gdrive",
                                 "expires": mod._dt.datetime.now().timestamp() + 900}
    u8 = upd(text=FAKE_TOKEN)
    with patch.object(mod, "connect_gdrive_account", return_value=(True, "connected and verified")):
        consumed = await mod._handle_wizard_input(u8, ctx())
    check("a pasted token consumes the message", consumed is True)
    check("...and the pasted credential is deleted from the chat",
          u8.message.delete.await_count == 1)
    check("...the wizard state is cleared either way",
          OWNER not in mod._gdrive_wizard)
    ok_texts = [c.args[0] for c in u8.message.reply_text.call_args_list if c.args]
    check("...success is reported clearly",
          any("connected" in t.lower() or "terhubung" in t.lower() for t in ok_texts))

    mod._gdrive_wizard[OWNER] = {"step": "await_gdrive_token", "name": "gdrive",
                                 "expires": mod._dt.datetime.now().timestamp() + 900}
    u9 = upd(text="garbage")
    with patch.object(mod, "connect_gdrive_account", return_value=(False, "no access_token in that JSON")):
        await mod._handle_wizard_input(u9, ctx())
    fail_texts = [c.args[0] for c in u9.message.reply_text.call_args_list if c.args]
    check("a failed connect is reported, not silently swallowed",
          any("access_token" in t or "gagal" in t.lower() or "couldn't" in t.lower()
              for t in fail_texts))

    # --- 9. expiry -----------------------------------------------------------
    mod._gdrive_wizard[OWNER] = {"step": "await_gdrive_token", "name": "gdrive",
                                 "expires": mod._dt.datetime.now().timestamp() - 1}
    u10 = upd(text=FAKE_TOKEN)
    await mod._handle_wizard_input(u10, ctx())
    check("an expired wizard is cleared rather than silently accepting a stale paste",
          OWNER not in mod._gdrive_wizard)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
