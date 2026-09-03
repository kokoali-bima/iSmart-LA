#!/usr/bin/env python3
"""Tests for /setscope, run against the REAL SOUL.md.template/GEMINI.md.template
content -- both a fresh (still-placeholder) brief and one /setbrief has
already filled in, and a re-run to prove it's re-editable, not a one-shot.
"""
import atexit
import shutil as _shutil
import asyncio, importlib.util, os, shutil, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

SRC = Path(sys.argv[1])
REPO = Path(sys.argv[2]) if len(sys.argv) > 2 else SRC.parent
# lite_agent.py does `from cli_login import ...` -- needs tools/ on sys.path
# too, or the module load below fails regardless of anything this suite is
# actually testing.
sys.path.insert(0, str(REPO / "tools"))

scratch = Path(tempfile.mkdtemp(prefix="isla_scope_"))
# Tests must not litter the machine they run on: 485 stale
# isla_* directories were found on a real server after a few days
# of runs. Registered rather than done at the end, so a failing
# assertion still cleans up.
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""
MOD = scratch / "lite_agent.py"
shutil.copy(SRC, MOD)
for f in ("SOUL.md.template", "GEMINI.md.template"):
    shutil.copy(REPO / f, scratch / f)
shutil.copy(scratch / "SOUL.md.template", scratch / "SOUL.md")
shutil.copy(scratch / "GEMINI.md.template", scratch / "GEMINI.md")

spec = importlib.util.spec_from_file_location("la", MOD)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

OWNER = 111
results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

def upd():
    msg = SimpleNamespace(reply_text=AsyncMock(), text=None)
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=OWNER),
                           effective_chat=SimpleNamespace(id=OWNER, type="private", title=None))
def ctx(args=None):
    return SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock()), args=args or [])

async def main():
    soul_before = (scratch / "SOUL.md").read_text()
    check("template starts as 'infrastructure' by default",
          mod.brief_scope() == "infrastructure")

    # --- bare /setscope shows guidance, changes nothing ---
    u0 = upd()
    await mod.cmd_setscope(u0, ctx([]))
    txt0 = u0.message.reply_text.call_args[0][0]
    check("bare /setscope explains itself", "/setscope" in txt0 and "assistant" in txt0)
    check("bare /setscope shows the current scope", "infrastructure" in txt0)
    check("bare /setscope changes nothing", mod.brief_scope() == "infrastructure")

    # --- set scope on a still-placeholder brief (role/org not set yet) ---
    u1 = upd()
    await mod.cmd_setscope(u1, ctx(["general-purpose,", "with", "strong", "infrastructure", "skills"]))
    txt1 = u1.message.reply_text.call_args[0][0]
    check("/setscope confirms the change", "general-purpose" in txt1)
    soul1 = (scratch / "SOUL.md").read_text()
    check("SOUL.md opening now reads the new scope",
          soul1.startswith("You are a general-purpose, with strong infrastructure skills assistant for"))
    check("the role placeholder is UNCHANGED by /setscope (that's /setbrief's job)",
          mod.BRIEF_PLACEHOLDER in soul1)
    gem1 = (scratch / "GEMINI.md").read_text()
    check("GEMINI.md changed too", gem1.startswith("You are a general-purpose"))

    m = soul1.index("<!-- LEARNED_ZONE -->")
    check("HARD BOUNDARIES still protected after /setscope",
          "HARD BOUNDARIES" in soul1 and soul1.index("HARD BOUNDARIES") < m)
    for conv in ("NEEDS_WRITE:", "SCHEDULE:", "GDRIVE:", "MEDIA:", "LEARN:"):
        check(f"{conv} still protected after /setscope", conv in soul1 and soul1.index(conv) < m)

    # --- /setbrief still works normally afterward (the two commands are independent) ---
    await mod.cmd_setbrief(upd(), ctx(["a", "7-node", "Proxmox", "cluster"]))
    soul2 = (scratch / "SOUL.md").read_text()
    check("/setbrief after /setscope: role filled in",
          "7-node Proxmox cluster" in soul2)
    check("/setbrief after /setscope: the custom scope SURVIVES",
          soul2.startswith("You are a general-purpose, with strong infrastructure skills assistant for"))

    # --- re-running /setscope changes it again (not a one-shot) ---
    u3 = upd()
    await mod.cmd_setscope(u3, ctx(["security-focused"]))
    soul3 = (scratch / "SOUL.md").read_text()
    check("second /setscope call replaces the phrase, doesn't append",
          soul3.startswith("You are a security-focused assistant for")
          and "general-purpose" not in soul3)
    check("brief_scope() reads back the new value", mod.brief_scope() == "security-focused")
    check("the role from /setbrief still survives a second /setscope",
          "7-node Proxmox cluster" in soul3)

    # --- article picking: a vs an ---
    await mod.cmd_setscope(upd(), ctx(["infrastructure"]))
    check("a/an: 'infrastructure' gets 'an'",
          (scratch / "SOUL.md").read_text().startswith("You are an infrastructure assistant for"))
    await mod.cmd_setscope(upd(), ctx(["general-purpose"]))
    check("a/an: 'general-purpose' gets 'a'",
          (scratch / "SOUL.md").read_text().startswith("You are a general-purpose assistant for"))

    # --- permission gating ---
    mod.ALLOWED_GROUP_IDS.add(-500)
    u4 = upd()
    u4.effective_user = SimpleNamespace(id=999)  # not the owner
    ctx4 = ctx(["something"])
    ctx4.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    u4.effective_chat = SimpleNamespace(id=-500, type="supergroup", title="G")
    u4.message = u4.effective_message  # keep .message and .effective_message in sync
    await mod.cmd_setscope(u4, ctx4)
    check("a non-admin cannot change scope",
          "owner" in u4.message.reply_text.call_args[0][0].lower()
          or "admin" in u4.message.reply_text.call_args[0][0].lower())

    # --- bilingual ---
    mod._write_chat_languages({str(OWNER): "id"})
    u5 = upd()
    await mod.cmd_setscope(u5, ctx([]))
    check("bare /setscope [ID] speaks Indonesian",
          "jenis apa" in u5.message.reply_text.call_args[0][0])
    mod._write_chat_languages({})

    # --- failure path: brief with no recognisable opening sentence ---
    (scratch / "SOUL.md").write_text("nothing template-shaped here at all\n")
    (scratch / "GEMINI.md").write_text("nothing template-shaped here at all either\n")
    u6 = upd()
    await mod.cmd_setscope(u6, ctx(["general-purpose"]))
    check("no matching opening sentence: refuses cleanly, says so",
          "Couldn't find" in u6.message.reply_text.call_args[0][0]
          or "Tidak ketemu" in u6.message.reply_text.call_args[0][0])
    check("...and truly changed nothing",
          "nothing template-shaped here at all" in (scratch / "SOUL.md").read_text())

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
