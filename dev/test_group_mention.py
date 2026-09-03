#!/usr/bin/env python3
"""Tests for the group "wake word" gate added so a real @mention can wake the
bot up in a group, not just a reply.

Confirmed live on 10.10.63.11: with Telegram's own Privacy Mode ON, a plain
"@botname" typed mid-sentence in a group does NOT reach the bot at all --
only slash commands and replies to the bot's own messages do, even though the
mention looks perfectly valid in the client. Verified three ways in the same
live chat: a bare @mention produced zero trace in the server log while it was
tailed in real time; a reply (pasting an OAuth code right after the bot asked
for it) worked; and a plain unrelated message ("test123", no mention, no
reply) also produced zero trace.

Making an actual @mention work requires Privacy Mode OFF (so every group
message reaches the bot), with this gate replacing what Privacy Mode used to
enforce -- a reply to the bot, or a real @mention entity, or the message
never reaches the wizard/server-input capture OR the model at all.

Entity offsets are UTF-16 code-unit based, not Python codepoint indices --
_entity_text/_strip_entity are tested against a message with an emoji BEFORE
the mention specifically because that is exactly the kind of case that once
produced a wrong "offsets don't line up" result in this project's own live
diagnostic script (see CHANGELOG), from naively slicing by codepoint index.
"""
import atexit
import shutil as _shutil
import asyncio, importlib.util, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_mention_"))
# Tests must not litter the machine they run on: 485 stale
# isla_* directories were found on a real server after a few days
# of runs. Registered rather than done at the end, so a failing
# assertion still cleans up.
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = "-999"

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

# LEDGER_FILE/MEMORY_DIR/MEMORY_FILE are BASE_DIR-relative (the module's own
# directory), NOT HOME-relative -- overriding HOME above does not sandbox
# them, and drive() below calls handle_message(), which reaches _run_turn()
# for anything that passes the mention/reply gate. See test_concurrency.py
# for the live confirmation: without this, running against a real checkout
# wrote spend.jsonl straight into the repo directory.
mod.LEDGER_FILE = scratch / "spend.jsonl"
mod.MEMORY_DIR = scratch / "memory"
mod.MEMORY_FILE = scratch / "MEMORY.md"

BOT_ID = 777
BOT_USERNAME = "bscloud_agent_bot"
OWNER = 111
GROUP = -999

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def entity(offset, length, etype="mention"):
    return SimpleNamespace(type=etype, offset=offset, length=length)


def ctx():
    return SimpleNamespace(bot=SimpleNamespace(
        id=BOT_ID, username=BOT_USERNAME, send_chat_action=AsyncMock()))


def group_update(text, entities=None, reply_from=None, chat_id=GROUP):
    reply_to = None
    if reply_from is not None:
        reply_to = SimpleNamespace(from_user=SimpleNamespace(id=reply_from))
    msg = SimpleNamespace(text=text, caption=None, photo=None, document=None,
                          entities=entities or [], reply_to_message=reply_to,
                          reply_text=AsyncMock())
    return SimpleNamespace(
        message=msg, effective_message=msg, callback_query=None,
        effective_user=SimpleNamespace(id=OWNER),
        effective_chat=SimpleNamespace(id=chat_id, type="group", title="g"),
    )


def private_update(text):
    msg = SimpleNamespace(text=text, caption=None, photo=None, document=None,
                          entities=[], reply_to_message=None, reply_text=AsyncMock())
    return SimpleNamespace(
        message=msg, effective_message=msg, callback_query=None,
        effective_user=SimpleNamespace(id=OWNER),
        effective_chat=SimpleNamespace(id=OWNER, type="private", title=None),
    )


async def main():
    # --- 1. _entity_text / _strip_entity: UTF-16 offset correctness --------
    # A real Telegram update for "🚀 hi @bscloud_agent_bot now" gives the
    # mention entity offset/length in UTF-16 code units. The rocket emoji is
    # outside the BMP (2 UTF-16 code units, 1 Python codepoint) -- exactly the
    # kind of character whose presence earlier in the string breaks a naive
    # Python string-index slice.
    text = "\U0001F680 hi @bscloud_agent_bot now"
    # UTF-16 code units: [rocket(2)] [' hi '(4)] ['@bscloud_agent_bot'(19)] [' now'(4)]
    offset = 2 + 4  # after the 2-code-unit emoji and " hi "
    length = len("@bscloud_agent_bot")
    check("_entity_text extracts the right substring across a non-BMP emoji",
          mod._entity_text(text, offset, length) == "@bscloud_agent_bot")
    check("_strip_entity removes just the mention, leaving the rest intact",
          mod._strip_entity(text, offset, length) == "\U0001F680 hi  now")

    # --- 2. _group_mention_span -------------------------------------------
    u1 = group_update("hari ini hari apa @bscloud_agent_bot ?",
                       entities=[entity(18, len("@bscloud_agent_bot"))])
    span1 = mod._group_mention_span(u1, ctx())
    check("_group_mention_span finds a real mention of the bot's own username",
          span1 == (18, len("@bscloud_agent_bot")))

    u2 = group_update("ask @someone_else about this",
                       entities=[entity(4, len("@someone_else"))])
    check("_group_mention_span ignores a mention of a DIFFERENT username",
          mod._group_mention_span(u2, ctx()) is None)

    u3 = group_update("just chatting, no mention entity at all", entities=[])
    check("_group_mention_span is None with no entities",
          mod._group_mention_span(u3, ctx()) is None)

    u4 = group_update("literal text containing @bscloud_agent_bot as a string "
                       "Telegram itself did not parse as an entity", entities=[])
    check("a literal '@botname' substring with NO entity does not count "
          "(must match what Telegram itself recognized, nothing looser)",
          mod._group_mention_span(u4, ctx()) is None)

    # --- 3. _is_reply_to_bot ------------------------------------------------
    u5 = group_update("4/0AT...", reply_from=BOT_ID)
    check("_is_reply_to_bot is True when replying to the bot's own message",
          mod._is_reply_to_bot(u5, ctx()) is True)
    u6 = group_update("4/0AT...", reply_from=222)
    check("_is_reply_to_bot is False when replying to some OTHER user",
          mod._is_reply_to_bot(u6, ctx()) is False)
    u7 = group_update("no reply here")
    check("_is_reply_to_bot is False with no reply at all",
          mod._is_reply_to_bot(u7, ctx()) is False)

    # --- 4. handle_message end to end, run_combo mocked as the boundary ----
    def fake_run_combo(text, *a, **kw):
        fake_run_combo.seen_text = text
        return {"result": "ok", "usage": {}}, "claude-haiku-4-5-20251001", []

    async def drive(update):
        fake_run_combo.seen_text = None
        with patch.object(mod, "run_combo", side_effect=fake_run_combo), \
             patch.object(mod, "load_sessions", return_value={}), \
             patch.object(mod, "get_chat_state",
                           return_value={"active": "default", "sessions": {"default": {}}}), \
             patch.object(mod, "save_sessions"), \
             patch.object(mod, "append_learned", return_value=[]), \
             patch.object(mod, "register_snapshot"), \
             patch.object(mod, "_maybe_notify_update", new=AsyncMock()):
            await mod.handle_message(update, ctx())
        return fake_run_combo.seen_text

    seen = await drive(group_update("just chatting, nothing special"))
    check("plain group chatter (no mention, no reply) never reaches run_combo "
          "(THE gate this suite exists for)", seen is None)

    seen = await drive(group_update("ask @someone_else something",
                                     entities=[entity(4, len("@someone_else"))]))
    check("a mention of someone ELSE in the group also never reaches run_combo",
          seen is None)

    seen = await drive(group_update("hari ini hari apa @bscloud_agent_bot ?",
                                     entities=[entity(18, len("@bscloud_agent_bot"))]))
    check("an actual @mention of the bot DOES reach run_combo",
          seen is not None)
    check("...with the mention itself stripped out of the text the model sees",
          seen is not None and "@bscloud_agent_bot" not in seen
          and "hari ini hari apa" in seen)

    seen = await drive(group_update("some code or answer", reply_from=BOT_ID))
    check("a reply to the bot's own message reaches run_combo (unchanged "
          "behaviour, mention or not)", seen is not None and seen == "some code or answer")

    seen = await drive(group_update("some code or answer", reply_from=222))
    check("a reply to someone ELSE (not the bot) does not reach run_combo",
          seen is None)

    seen = await drive(private_update("anything at all, no mention, no reply"))
    check("a private DM is never gated -- the mention/reply rule is group-only",
          seen == "anything at all, no mention, no reply")

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
