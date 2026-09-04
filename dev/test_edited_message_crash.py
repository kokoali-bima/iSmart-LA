#!/usr/bin/env python3
"""Tests for a real crash found live on bscloud: editing an already-sent
command message crashed the handler.

    File "lite_agent.py", line 4437, in cmd_usemodel
        await update.message.reply_text(_t(lang,
    AttributeError: 'NoneType' object has no attribute 'reply_text'

Twice in the same minute, both from the exact same user action: send
"/usemodel ...", then edit that message afterward (fixing a typo, say).
Telegram sends that as an `edited_message` update, not a `message` one -- the
content lives in `update.edited_message`, and `update.message` is None.

python-telegram-bot's CommandHandler matches an edited_message exactly like a
fresh one by default (it checks effective_message's text, which resolves to
edited_message when that's what is present) -- so any handler written against
raw `update.message` fires anyway and crashes on the first attribute access.
That pattern (`update.message.reply_text` with no None-check) appears at over
a hundred call sites in this file, so patching cmd_usemodel alone would leave
the other ~129 equally exposed to the same user action.

The first fix was to stop Telegram sending them at all:
`run_polling(allowed_updates=["message", "callback_query"])`. That closed the
crash in one place rather than in each of the hundred-plus call sites.

It also broke something people do constantly, and that took a report to
notice: send a message, see that the @mention is missing, edit it in -- and
get no answer, because the edit was never delivered. So since v0.2b.66
edited_message IS delivered again, and the crash is held off one layer lower:

  * `_authorized()` refuses any update with no `message` and no
    `callback_query` -- which is what an edited update looks like -- unless
    the caller passes `allow_edited=True`.
  * Exactly one caller does: `handle_message`, the plain-message path, which
    reads `effective_message` rather than `update.message`.
  * Commands never opt in. cmd_usemodel, where this crashed, still ignores an
    edited message entirely.

So the guarantee changed shape rather than weakening: the hundred-plus raw
`update.message.reply_text` sites are still unreachable from an edited
update, and the one path that can handle one now does.
"""
import ast, io, re, sys
from pathlib import Path

SRC = sys.argv[1]
src = io.open(SRC, encoding="utf-8").read()
tree = ast.parse(src)

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

# --- 1. find the run_polling(...) call and inspect its keyword arguments ---
run_polling_calls = [
    n for n in ast.walk(tree)
    if isinstance(n, ast.Call)
    and isinstance(n.func, ast.Attribute)
    and n.func.attr == "run_polling"
]
check("exactly one app.run_polling(...) call exists", len(run_polling_calls) == 1)
call = run_polling_calls[0]
kwargs = {kw.arg: kw.value for kw in call.keywords}

check("run_polling() passes allowed_updates explicitly (THE fix)",
      "allowed_updates" in kwargs)

if "allowed_updates" in kwargs:
    node = kwargs["allowed_updates"]
    values = None
    if isinstance(node, ast.List):
        values = [elt.value for elt in node.elts if isinstance(elt, ast.Constant)]
    check("allowed_updates is a literal list of update-type strings",
          values is not None)
    # The list grew by one, deliberately. Blocking edited_message outright
    # also broke something people do all the time: send a message, notice the
    # @mention is missing, edit it in -- and get no answer at all. Reported
    # that way. Since v0.2b.66 edited updates ARE delivered, and the crash is
    # held off at the guard instead: _authorized() refuses a message-less
    # update unless the caller opts in, and only handle_message does. Commands
    # -- which is where cmd_usemodel crashed -- never opt in.
    check("...contains exactly the three types this bot handles, nothing more",
          values is not None
          and sorted(values) == ["callback_query", "edited_message", "message"])
    check("channel posts and the rest are still excluded -- this is an "
          "allowlist, not everything",
          values is not None
          and not ({"channel_post", "edited_channel_post", "poll",
                    "my_chat_member"} & set(values)))
    src_text = Path(SRC).read_text(encoding="utf-8")
    check("the crash is now held off at the guard: a message-less update is "
          "refused unless the caller explicitly opts in",
          "allow_edited: bool = False" in src_text)
    check("...and ONLY the plain-message path opts in, never a command",
          src_text.count("allow_edited=True") == 1
          and "_authorized(update, allow_edited=True)" in src_text)

check("drop_pending_updates=False is preserved (an unrelated earlier fix -- "
      "a message sent during a brief restart must still be processed)",
      kwargs.get("drop_pending_updates") is not None
      and isinstance(kwargs["drop_pending_updates"], ast.Constant)
      and kwargs["drop_pending_updates"].value is False)

# --- 2. cross-check against every update type this bot actually handles ---
handler_calls = [
    n for n in ast.walk(tree)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    and n.func.id == "add_handler"
]
# add_handler(XHandler(...)) -- walk one level in to find the constructor name.
registered_types = set()
for n in ast.walk(tree):
    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id.endswith("Handler") and n.func.id != "add_handler"):
        registered_types.add(n.func.id)

check("this bot registers CommandHandler",
      "CommandHandler" in registered_types)
check("this bot registers MessageHandler",
      "MessageHandler" in registered_types)
check("this bot registers CallbackQueryHandler",
      "CallbackQueryHandler" in registered_types)
check("this bot registers NO handler type that would need edited_message, "
      "chat_member, poll, or inline_query updates (so restricting "
      "allowed_updates loses nothing this bot actually uses)",
      registered_types <= {"CommandHandler", "MessageHandler", "CallbackQueryHandler"})

# --- 3. the scale of what this one fix protects ----------------------------
raw_reply_sites = len(re.findall(r"update\.message\.reply_text", src))
check("well over a hundred call sites use raw update.message.reply_text -- "
      "consistent with fixing this at the allowed_updates source rather than "
      "patching each one",
      raw_reply_sites > 100)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
