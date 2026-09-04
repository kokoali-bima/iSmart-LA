#!/usr/bin/env python3
"""Tests for surviving the retirement of rclone's shared OAuth client (2026).

Google has begun charging for API requests made through rclone's built-in
shared client_id, and usage is far over the free quota, so rclone is retiring
it during 2026. Every user then needs their own client_id/client_secret.

The exposure in this project was not obvious, because nothing about it is
visible while it works. `connect_gdrive_account()` created the remote with only
`scope` and `token`:

    rclone config create <name> drive scope=drive.file token=<blob>

An access token lives about an hour. Everything after that is the REFRESH, and
rclone refreshes using the client_id stored ON THE REMOTE -- with none stored,
it uses its own built-in one. So the account whose token came from the
operator's OWN Google Cloud client (the device flow, v0.2b.54) was still
refreshing through rclone's shared client, and would have died with it. The
failure mode is the nastiest kind: fine for an hour, then Drive stops, on a
deployment nobody touched, with nothing pointing at the cause.

THE TRAP THIS DELIBERATELY AVOIDS, and most of what is asserted below: a
refresh token is bound to the client that ISSUED it. Attaching a client_id to a
token that came from a different client does not postpone the breakage -- it
causes it immediately, at the first refresh. So the client is passed in by the
caller rather than read inside, and only the one caller that actually knows
passes it:

    device flow      -> the operator's own stored client   (certain)
    rclone authorize -> rclone's shared client             (nothing to attach)
    manual paste     -> whatever the operator used         (unknown)

The last two correctly pass nothing and keep today's behaviour. They are the
paths that must be RECONNECTED before the retirement, which is why the second
half of this suite covers making that visible in /gdrivestatus rather than
leaving it to be discovered.
"""
import ast
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile

SRC = pathlib.Path(sys.argv[1]).resolve()
source = SRC.read_text(encoding="utf-8")
tree = ast.parse(source, filename=str(SRC))

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

scratch = pathlib.Path(tempfile.mkdtemp(prefix="isla_cid_"))
import atexit
atexit.register(shutil.rmtree, scratch, ignore_errors=True)

work = scratch / "install"
work.mkdir()
shutil.copy(SRC, work / SRC.name)
shutil.copytree(SRC.parent / "tools", work / "tools")
home = work / "home"
home.mkdir()
os.environ.update(HOME=str(home), USERPROFILE=str(home), TELEGRAM_BOT_TOKEN="t",
                  ALLOWED_USER_IDS="1", ALLOWED_GROUP_IDS="")

spec = importlib.util.spec_from_file_location("la_cid", work / SRC.name)
la = importlib.util.module_from_spec(spec)
sys.modules["la_cid"] = la
spec.loader.exec_module(la)

REQUIRED = ("connect_gdrive_account", "_gdrive_accounts_on_shared_client")
for _n in REQUIRED:
    check(f"the module provides {_n}", hasattr(la, _n))
if [n for n in REQUIRED if not hasattr(la, n)]:
    _f = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(_f)}/{len(results)} passed")
    print("FAILED:", _f)
    print("the client_id work is absent from this source -- the checks below "
          "it need it, so they cannot be evaluated")
    sys.exit(1)

TOKEN = json.dumps({"access_token": "a", "refresh_token": "r",
                    "token_type": "Bearer", "expiry": "2030-01-01T00:00:00Z"})
CLIENT = {"client_id": "own-client.apps.googleusercontent.com",
          "client_secret": "own-secret"}


# --- 1. what actually reaches `rclone config create` -----------------------
class _Res:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err

def run_with(oauth_client, listing_root=True):
    """Capture every rclone invocation for one connect attempt."""
    calls = []
    def fake(*args, timeout=60):
        calls.append(list(args))
        if args[:2] == ("config", "create"):
            return _Res(0)
        if args[0] == "lsd":
            return _Res(0, f"          -1 2026-01-01 00:00:00        -1 {la.GDRIVE_ROOT}\n"
                        if listing_root else "")
        return _Res(0)
    la._rclone_run = fake
    la._list_gdrive_accounts = lambda: []
    ok, detail = la.connect_gdrive_account("gdrive", TOKEN, oauth_client)
    create = next((c for c in calls if c[:2] == ["config", "create"]), [])
    return ok, detail, create

ok, detail, create = run_with(CLIENT)
check("a connect with a known client still succeeds", ok)
check("the client_id is written onto the remote, so refresh no longer depends "
      "on rclone's shared client",
      any(a == f"client_id={CLIENT['client_id']}" for a in create))
check("...and the secret with it, since refresh needs both",
      any(a == f"client_secret={CLIENT['client_secret']}" for a in create))
check("the scope is unchanged -- this changes WHO refreshes, not what the bot "
      "can reach",
      "scope=drive.file" in create)
check("the token is still passed as before", any(a.startswith("token=") for a in create))

# The trap. A token from rclone's own client refreshed with someone else's
# client_id fails at the FIRST refresh -- sooner than doing nothing.
ok2, _, create2 = run_with(None)
check("a connect with NO known client succeeds too (unchanged behaviour)", ok2)
check("...and writes NO client_id, rather than guessing one and breaking "
      "refresh immediately",
      not any(a.startswith("client_id=") for a in create2))
check("...and no client_secret either", not any(a.startswith("client_secret=") for a in create2))

for empty in ({}, {"client_id": ""}, {"client_secret": "x"}):
    _, _, c = run_with(empty)
    check(f"an unusable client dict ({empty}) is treated as none, not written blank",
          not any(a.startswith("client_id=") for a in c))


# --- 2. which callers may pass a client ------------------------------------
def func_src(name, code_only=True):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            body = n.body
            if code_only and body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return "\n".join(ast.unparse(s) for s in body)
    return ""

# Every connect_gdrive_account call in the module, with its arguments.
connect_calls = [ast.unparse(n) for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and "connect_gdrive_account" in ast.unparse(n)
                 and "disconnect" not in ast.unparse(n)]
with_client = [c for c in connect_calls if "read_gdrive_client" in c]
check("exactly one call site passes a client -- the device flow, the only one "
      "that knows which client issued the token",
      len(with_client) == 1)
check("...and it is the one handling the device-flow token",
      bool(with_client) and "gdrive_token_to_rclone" in with_client[0])
check("connect_gdrive_account does not read the client itself, which would "
      "attach it to tokens from other clients too",
      "read_gdrive_client" not in func_src("connect_gdrive_account"))


# --- 3. the accounts that cannot be saved must at least be visible ---------
DUMP = json.dumps({
    "gdrive":       {"type": "drive", "scope": "drive.file"},
    "gdrive_own":   {"type": "drive", "scope": "drive.file", "client_id": "mine"},
    "gdrive_blank": {"type": "drive", "client_id": "   "},
    "notdrive":     {"type": "s3", "provider": "AWS"},
})
la._rclone_run = lambda *a, timeout=60: _Res(0, DUMP) if a[:2] == ("config", "dump") else _Res(0)
flagged = la._gdrive_accounts_on_shared_client()
check("a Drive remote with no client_id is flagged", "gdrive" in flagged)
check("a whitespace-only client_id is flagged too, not mistaken for one set",
      "gdrive_blank" in flagged)
check("a remote with its own client_id is NOT flagged", "gdrive_own" not in flagged)
check("a non-Drive remote is left alone entirely", "notdrive" not in flagged)

la._rclone_run = lambda *a, timeout=60: _Res(1, "", "boom")
check("an unreadable rclone config reports nothing rather than crying wolf "
      "about every account", la._gdrive_accounts_on_shared_client() == [])

status = func_src("cmd_gdrivestatus")
check("/gdrivestatus surfaces the flagged accounts", "_gdrive_accounts_on_shared_client" in status)
check("...in both languages", status.count("2026") >= 2)
check("...saying reconnection is required, not merely that a client is needed "
      "-- an existing refresh token cannot be re-pointed at a new client",
      "/connectgdrive" in status
      and ("issued it" in status or "menerbitkannya" in status))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
