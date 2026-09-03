#!/usr/bin/env python3
"""Tests: PIN confirmation must not dead-end in a group for an action whose
own entry gate already trusts a registered group's admin to reach the PIN
prompt in the first place -- the exact bug reported live: /update let a
group admin all the way to the PIN keypad, then refused the PIN itself with
"only in a private DM", for no security gained (they were already vetted
seconds earlier by a SEPARATE, drifted-out-of-sync check deciding the same
thing). update, addserver and (per an explicit follow-up decision) rmboundary
are now all group-eligible, gated the same way throughout: owner anywhere, or
a REGISTERED group's own admin, live-checked against Telegram -- plus the
PIN itself either way.

A hypothetical unreviewed action ("future_action", not really wired to
anything) stands in for "whatever gets added next without a deliberate group
decision" -- proving the strict fallback (owner AND private DM) still holds
for anything not explicitly listed, including for the owner themselves.
"""
import atexit
import shutil as _shutil
import asyncio, importlib.util, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

SRC = sys.argv[1]
scratch = Path(tempfile.mkdtemp(prefix="isla_pingroup_"))
# Tests must not litter the machine they run on: 485 stale
# isla_* directories were found on a real server after a few days
# of runs. Registered rather than done at the end, so a failing
# assertion still cleans up.
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = "-500"

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

OWNER = 111
GROUP_ADMIN = 222  # NOT the owner -- a plain registered-group admin
PLAIN_MEMBER = 333  # a group member with no admin status at all
GROUP_CHAT = -500
results = []

def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

def update_for(user_id: int, chat_id: int, chat_type: str):
    msg = SimpleNamespace(text="x\n\n🔢 6-digit PIN:\n○○○○○○", reply_text=AsyncMock())
    q = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock(), message=msg)
    return SimpleNamespace(
        message=None, effective_message=msg, callback_query=q,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title="G"),
    ), q

def ctx_for(status: str):
    member = SimpleNamespace(status=status)
    return SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock(return_value=member)), args=[])

async def press_digit(action: str, user_id: int, chat_id: int, chat_type: str,
                       admin_status: str = "administrator", payload: dict | None = None):
    token = mod._new_pin_session(action, payload or {}, chat_id)
    upd, q = update_for(user_id, chat_id, chat_type)
    upd.callback_query.data = f"pin:{token}:1"
    await mod.cmd_pin_key(upd, ctx_for(admin_status))
    alerted = any(c.kwargs.get("show_alert") for c in q.answer.call_args_list)
    alert_text = next((c.args[0] for c in q.answer.call_args_list if c.args and c.kwargs.get("show_alert")), "")
    succeeded = (not alerted) and q.edit_message_text.called
    return succeeded, alert_text

async def main():
    # ---- the three now-fixed, group-eligible actions, tried by a plain
    #      (non-owner) registered-group admin ----
    for action in ("update", "addserver", "rmboundary"):
        ok, alert = await press_digit(action, GROUP_ADMIN, GROUP_CHAT, "supergroup",
                                       payload={"rule": "x"} if action == "rmboundary" else None)
        check(f"'{action}': group admin's PIN entry succeeds (no dead-end)", ok)

    # ---- the same three, tried by a group member with NO admin status ----
    for action in ("update", "addserver", "rmboundary"):
        ok, alert = await press_digit(action, PLAIN_MEMBER, GROUP_CHAT, "supergroup",
                                       admin_status="member",
                                       payload={"rule": "x"} if action == "rmboundary" else None)
        check(f"'{action}': a non-admin member is still refused",
              not ok and alert in ("Not permitted.", "Tidak diizinkan."))

    # ---- an unreviewed action: strict fallback must still hold ----
    ok, alert = await press_digit("future_action", GROUP_ADMIN, GROUP_CHAT, "supergroup")
    check("unreviewed action: a group admin (not owner) is refused outright",
          not ok and alert in ("Not permitted.", "Tidak diizinkan."))

    ok, alert = await press_digit("future_action", OWNER, GROUP_CHAT, "supergroup")
    check("unreviewed action: even the OWNER is told to use a private DM (not just 'not permitted')",
          not ok and ("private DM" in alert or "DM pribadi" in alert))

    ok, alert = await press_digit("future_action", OWNER, OWNER, "private")
    check("unreviewed action: the owner IS allowed, from an actual private DM", ok)

    # ---- sanity: the frozenset itself has what's expected ----
    check("PIN_ACTIONS_ALLOWED_IN_GROUP has all four reviewed group-eligible actions",
          {"update", "addserver", "rmboundary", "unlock"} <= mod.PIN_ACTIONS_ALLOWED_IN_GROUP)
    check("PIN_ACTIONS_ALLOWED_IN_GROUP does not contain the made-up action",
          "future_action" not in mod.PIN_ACTIONS_ALLOWED_IN_GROUP)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
