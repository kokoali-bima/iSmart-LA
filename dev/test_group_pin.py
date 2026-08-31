#!/usr/bin/env python3
"""Tests for per-group PINs: each registered group can have its own PIN,
separate from the owner's; the owner's own PIN is a master credential that
still works everywhere; a group with none of its own falls back to the
owner's, exactly like before this feature existed.

Covers: storage (including reading an old pre-feature flat pin.json
transparently), the full /setgrouppin and /rmgrouppin flows end to end
through the real keypad handlers, cross-group isolation (group A's PIN must
NOT work in group B), the owner override, and permission gating (a plain
group member can't set/remove a group's PIN, someone from an unregistered
group can't set one at all).
"""
import asyncio, importlib.util, json, os, shutil, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

SRC = sys.argv[1]

OWNER = 111
GROUP_A = -100
GROUP_A_ADMIN = 222
GROUP_B = -200
GROUP_B_ADMIN = 333
PLAIN_MEMBER = 444

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def fresh_module(extra_env=None):
    # BASE_DIR (and so PIN_FILE) is derived from the MODULE'S OWN path, not
    # from $HOME -- copying it into the fresh scratch dir is what actually
    # isolates pin.json between fresh_module() calls. Loading straight from a
    # shared SRC path would have every "fresh" module secretly sharing one
    # real pin.json underneath, each call's writes bleeding into the next.
    scratch = Path(tempfile.mkdtemp(prefix="isla_gpin_"))
    mod_path = scratch / "lite_agent.py"
    shutil.copy(SRC, mod_path)
    os.environ["HOME"] = str(scratch)
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["ALLOWED_USER_IDS"] = str(OWNER)
    os.environ["ALLOWED_GROUP_IDS"] = f"{GROUP_A},{GROUP_B}"
    for k, v in (extra_env or {}).items():
        os.environ[k] = v
    modname = f"la_{os.urandom(4).hex()}"
    spec = importlib.util.spec_from_file_location(modname, mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod, scratch


def upd(user_id, chat_id, chat_type="supergroup"):
    msg = SimpleNamespace(text="x", reply_text=AsyncMock())
    return SimpleNamespace(
        message=msg, effective_message=msg, callback_query=None,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title=f"Chat{chat_id}"),
    )


def qupd(user_id, chat_id, data, chat_type="supergroup"):
    msg = SimpleNamespace(text="x\n\n🔢 6-digit PIN:\n○○○○○○", reply_text=AsyncMock())
    q = SimpleNamespace(data=data, message=msg, answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(
        message=None, effective_message=msg, callback_query=q,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title=f"Chat{chat_id}"),
    ), q


def ctx(status="administrator"):
    member = SimpleNamespace(status=status)
    return SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock(return_value=member)), args=[])


async def type_pin(mod, user_id, chat_id, pin: str, admin_status="administrator", chat_type="supergroup"):
    """Drive the keypad ONE TIME for whatever session is currently open for
    chat_id -- use for confirming an EXISTING pin (one entry) or one leg of a
    new-pin capture/confirm pair (call it twice with the same digits for a
    fresh PIN: once to capture, once to confirm)."""
    token = next(t for t, s in mod._pin_sessions.items() if s["chat_id"] == chat_id)
    last_q = None
    for digit in pin:
        u, q = qupd(user_id, chat_id, f"pin:{token}:{digit}", chat_type)
        await mod.cmd_pin_key(u, ctx(admin_status))
        last_q = q
    return last_q


async def set_new_pin(mod, user_id, chat_id, pin: str, admin_status="administrator"):
    """A fresh PIN needs two full entries (capture, then confirm) before it
    is actually stored -- this drives both."""
    await type_pin(mod, user_id, chat_id, pin, admin_status)
    return await type_pin(mod, user_id, chat_id, pin, admin_status)


async def main():
    mod, scratch = fresh_module()

    # ---- 1. no PIN exists anywhere yet ----
    check("nothing set: pin_is_set() False for owner scope", mod.pin_is_set() is False)
    check("nothing set: pin_is_set(GROUP_A) False", mod.pin_is_set(GROUP_A) is False)

    # ---- 2. owner sets their PIN (DM) ----
    mod.set_pin("111111")
    check("owner PIN now set (no chat_id)", mod.pin_is_set() is True)
    check("owner PIN verifies in a DM (chat_id=None)", mod.verify_pin("111111", None))
    check("owner PIN verifies inside group A too (master credential)",
          mod.verify_pin("111111", GROUP_A))
    check("group A has no PIN of its own yet", mod.group_pin_is_set(GROUP_A) is False)

    # ---- 3. /setgrouppin end to end, by group A's own admin ----
    u = upd(GROUP_A_ADMIN, GROUP_A)
    await mod.cmd_setgrouppin(u, ctx())
    check("group admin CAN start /setgrouppin", mod._pin_sessions,
          )  # sanity: a session now exists
    q_last = await set_new_pin(mod, GROUP_A_ADMIN, GROUP_A, "222222")
    check("group A PIN set: confirmation message shown",
          "set for" in q_last.edit_message_text.call_args[0][0]
          or "tersimpan untuk" in q_last.edit_message_text.call_args[0][0])
    check("group_pin_is_set(GROUP_A) now True", mod.group_pin_is_set(GROUP_A) is True)

    # ---- 4. isolation: group A's PIN must not work in group B or DM ----
    check("group A's PIN does NOT verify in group B", not mod.verify_pin("222222", GROUP_B))
    check("group A's PIN does NOT verify in a DM", not mod.verify_pin("222222", None))
    check("group A's PIN DOES verify in group A", mod.verify_pin("222222", GROUP_A))
    check("owner's PIN STILL verifies in group A (override, unaffected)",
          mod.verify_pin("111111", GROUP_A))

    # ---- 5. group B has none of its own -> falls back to the owner's ----
    check("group B has no PIN of its own", mod.group_pin_is_set(GROUP_B) is False)
    check("owner's PIN verifies in group B (fallback)", mod.verify_pin("111111", GROUP_B))
    check("a random guess does not verify in group B", not mod.verify_pin("999999", GROUP_B))

    # ---- 6. changing group A's PIN requires the CURRENT one first ----
    u2 = upd(GROUP_A_ADMIN, GROUP_A)
    await mod.cmd_setgrouppin(u2, ctx())
    txt = u2.message.reply_text.call_args[0][0]
    check("changing an existing group PIN asks to confirm the CURRENT one first",
          "CURRENT" in txt or "SEKARANG" in txt)
    # confirm with the group's own current PIN, then supply the new one
    q_confirm = await type_pin(mod, GROUP_A_ADMIN, GROUP_A, "222222")
    q_new2 = await set_new_pin(mod, GROUP_A_ADMIN, GROUP_A, "333333")
    check("group A PIN changed successfully",
          "set for" in q_new2.edit_message_text.call_args[0][0]
          or "tersimpan untuk" in q_new2.edit_message_text.call_args[0][0])
    check("old group A PIN (222222) no longer verifies", not mod.verify_pin("222222", GROUP_A))
    check("new group A PIN (333333) verifies", mod.verify_pin("333333", GROUP_A))

    # the owner can ALSO confirm a group-PIN change with their own PIN instead
    # of the group's -- exercise that path explicitly.
    u3 = upd(OWNER, GROUP_A, chat_type="supergroup")
    await mod.cmd_setgrouppin(u3, ctx())
    await type_pin(mod, OWNER, GROUP_A, "111111")  # owner's PIN, not the group's
    q_owner_new2 = await set_new_pin(mod, OWNER, GROUP_A, "444444")
    check("owner can confirm a group-PIN change with THEIR OWN pin",
          "set for" in q_owner_new2.edit_message_text.call_args[0][0]
          or "tersimpan untuk" in q_owner_new2.edit_message_text.call_args[0][0])
    check("group A's PIN is now 444444", mod.verify_pin("444444", GROUP_A))

    # ---- 7. permission gating ----
    u4 = upd(PLAIN_MEMBER, GROUP_A)
    await mod.cmd_setgrouppin(u4, ctx(status="member"))
    _msg4 = u4.message.reply_text.call_args[0][0] if u4.message.reply_text.call_args else "<no call>"
    print("DEBUG plain-member message:", repr(_msg4))
    check("a plain (non-admin) member cannot start /setgrouppin",
          "owner" in _msg4.lower() or "admin" in _msg4.lower())

    u5 = upd(GROUP_A_ADMIN, GROUP_A)  # not registered as an unregistered group
    unreg_ctx = SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock(
        return_value=SimpleNamespace(status="administrator"))), args=[])
    u5b = upd(999, -999, chat_type="supergroup")  # -999 is NOT in ALLOWED_GROUP_IDS
    await mod.cmd_setgrouppin(u5b, unreg_ctx)
    check("an unregistered group cannot set its own PIN yet",
          "registergroup" in u5b.message.reply_text.call_args[0][0].lower())

    u6 = upd(OWNER, OWNER, chat_type="private")
    await mod.cmd_setgrouppin(u6, ctx())
    _msg6 = u6.message.reply_text.call_args[0][0] if u6.message.reply_text.call_args else "<no call>"
    print("DEBUG DM-refusal message:", repr(_msg6))
    check("/setgrouppin refuses in a DM (nothing to scope it to)",
          "group" in _msg6.lower() or "grup" in _msg6.lower())

    # ---- 8. /rmgrouppin ----
    u7 = upd(PLAIN_MEMBER, GROUP_A)
    await mod.cmd_rmgrouppin(u7, ctx(status="member"))
    check("a plain member cannot remove group A's PIN",
          mod.group_pin_is_set(GROUP_A) is True)  # unchanged

    u8 = upd(GROUP_A_ADMIN, GROUP_A)
    await mod.cmd_rmgrouppin(u8, ctx())
    check("group admin CAN remove group A's own PIN", mod.group_pin_is_set(GROUP_A) is False)
    check("group A now falls back to the owner's PIN", mod.verify_pin("111111", GROUP_A))
    check("the old group PIN (444444) no longer verifies anywhere",
          not mod.verify_pin("444444", GROUP_A))

    u9 = upd(GROUP_A_ADMIN, GROUP_A)
    await mod.cmd_rmgrouppin(u9, ctx())
    _msg9 = u9.message.reply_text.call_args[0][0] if u9.message.reply_text.call_args else "<no call>"
    print("DEBUG rm-again message:", repr(_msg9))
    check("removing again (nothing to remove) says so",
          "doesn't have" in _msg9 or "belum punya" in _msg9)

    # ---- 9. backward compatibility: an OLD flat pin.json is read correctly ----
    mod2, scratch2 = fresh_module()
    old_flat = {"salt": "aa" * 16, "hash": "bb" * 32}
    mod2.PIN_FILE.write_text(json.dumps(old_flat))
    check("old flat pin.json: pin_is_set() still True", mod2.pin_is_set() is True)
    store = mod2._pin_store()
    check("old flat pin.json: normalized into the owner slot",
          store["owner"] == old_flat and store["groups"] == {})
    # setting a group PIN on top of an old-format file must not corrupt it
    mod2.set_group_pin(GROUP_A, "555555", OWNER)
    check("group PIN can be added on top of a migrated old-format file",
          mod2.verify_pin("555555", GROUP_A))
    check("the pre-existing owner hash survives untouched",
          mod2._pin_store()["owner"] == old_flat)

    # ---- 10. cmd_pin_key's may_touch/location gate still holds for new actions ----
    mod3, scratch3 = fresh_module()
    u10 = upd(GROUP_A_ADMIN, GROUP_A)
    await mod3.cmd_setgrouppin(u10, ctx())  # new_group_pin_capture session opens
    token = next(t for t, s in mod3._pin_sessions.items() if s["chat_id"] == GROUP_A)
    u10b, q10b = qupd(PLAIN_MEMBER, GROUP_A, f"pin:{token}:1")
    await mod3.cmd_pin_key(u10b, ctx(status="member"))
    alerted = any(c.kwargs.get("show_alert") for c in q10b.answer.call_args_list)
    check("a non-admin cannot drive a group-PIN-capture keypad either", alerted)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


asyncio.run(main())
