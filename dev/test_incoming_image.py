#!/usr/bin/env python3
"""Tests that a photo sent to the bot is actually answered.

Reported from a real session: a screenshot of a Google Cloud console page with
the caption "Untuk buat connectgdrive di ismart, kita pilih yang mana nih?"
got **no reply at all**. Nothing in the log either -- the message never reached
a handler. The message filter was `filters.TEXT & ~filters.COMMAND`, and a
photo with a caption is not filters.TEXT, so it matched nothing and was
dropped without a word.

Silence is the worst way for this to fail: the person cannot tell whether the
bot is thinking, broken, or ignoring them.

Both CLIs turned out to read a local image when the prompt names its path --
checked live on a generated PNG neither had seen before, claude answering "a
black rectangle in the upper-left", agy "red, on the right side". So the fix
is not an apology message: the image is downloaded and the model reads it, on
the cheap default tier too, not only after escalating.
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
scratch = Path(tempfile.mkdtemp(prefix="isla_img_"))
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
mod.MEMORY_DIR = scratch / "memory"
mod.MEMORY_FILE = scratch / "MEMORY.md"
mod.INCOMING_MEDIA_DIR = scratch / "incoming"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def tg_file(dest_written: list):
    async def download(custom_path=None):
        Path(custom_path).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        dest_written.append(custom_path)
    return SimpleNamespace(file_path="photos/x.jpg", download_to_drive=download)


def upd(text=None, caption=None, photo=False, document_mime=None):
    photos = [SimpleNamespace(file_id="small", file_size=100),
              SimpleNamespace(file_id="BIGGEST", file_size=9000)] if photo else None
    doc = (SimpleNamespace(file_id="DOC", file_size=500, mime_type=document_mime)
           if document_mime else None)
    msg = SimpleNamespace(text=text, caption=caption, photo=photos, document=doc,
                          reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=111),
                           effective_chat=SimpleNamespace(id=111, type="private", title=None))


def ctx(written):
    return SimpleNamespace(
        bot=SimpleNamespace(get_file=AsyncMock(return_value=tg_file(written)),
                            send_chat_action=AsyncMock(), send_message=AsyncMock()),
        args=[])


async def main():
    # --- 1. the message filter must accept photos at all -------------------
    src = Path(SRC).read_text(encoding="utf-8")
    check("the handler is registered for PHOTO as well as TEXT -- with "
          "filters.TEXT alone a captioned screenshot matched nothing and was "
          "dropped in silence", "filters.PHOTO" in src)

    # --- 2. a photo is downloaded and its path handed to the model ---------
    written = []
    u = upd(caption="which one do I pick?", photo=True)
    captured = {}
    async def fake_turn(update, context, text, **kw):
        captured["text"] = text
    with patch.object(mod, "_run_turn", side_effect=fake_turn), \
         patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)), \
         patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u, ctx(written))

    check("a photo with a caption reaches the model instead of vanishing",
          "text" in captured)
    check("...the caption is used as the question", "which one do I pick?" in captured.get("text", ""))
    check("...and the saved path is named so the model can read it",
          str(mod.INCOMING_MEDIA_DIR) in captured.get("text", ""))
    check("...the file was actually written to disk", written and Path(written[0]).exists())
    check("the LARGEST of Telegram's sizes is fetched, not the first "
          "(the first is a thumbnail too small to read text from)",
          u.message.photo[-1].file_id == "BIGGEST")

    # --- 3. a photo with NO caption still gets a sensible prompt -----------
    written2 = []
    captured2 = {}
    async def fake_turn2(update, context, text, **kw):
        captured2["text"] = text
    u2 = upd(photo=True)
    with patch.object(mod, "_run_turn", side_effect=fake_turn2), \
         patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)), \
         patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u2, ctx(written2))
    check("a photo with no caption still asks the model something useful",
          captured2.get("text") and str(mod.INCOMING_MEDIA_DIR) in captured2["text"])

    # --- 4. an image sent as a document works too --------------------------
    written3 = []
    captured3 = {}
    async def fake_turn3(update, context, text, **kw):
        captured3["text"] = text
    u3 = upd(caption="read this", document_mime="image/png")
    with patch.object(mod, "_run_turn", side_effect=fake_turn3), \
         patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)), \
         patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u3, ctx(written3))
    check("an image sent as a FILE rather than a photo is handled too",
          captured3.get("text") and str(mod.INCOMING_MEDIA_DIR) in captured3["text"])

    # --- 5. THE rule: never silent ------------------------------------------
    u4 = upd(caption="here", document_mime="application/zip")
    with patch.object(mod, "_run_turn", side_effect=AssertionError("must not reach the model")), \
         patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)), \
         patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u4, ctx([]))
    check("an attachment that cannot be read gets a REPLY saying so, never "
          "silence -- silence leaves the sender unable to tell whether it "
          "even arrived", u4.message.reply_text.call_args is not None)
    said = u4.message.reply_text.call_args[0][0].lower()
    check("...and says what WOULD work", "image" in said or "gambar" in said)

    # --- 6. plain text is untouched ----------------------------------------
    captured5 = {}
    async def fake_turn5(update, context, text, **kw):
        captured5["text"] = text
    u5 = upd(text="just a normal question")
    with patch.object(mod, "_run_turn", side_effect=fake_turn5), \
         patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)), \
         patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u5, ctx([]))
    check("an ordinary text message is unchanged -- no image note bolted on",
          captured5.get("text") == "just a normal question")

    # --- 7. housekeeping ----------------------------------------------------
    check("the model is allowed to Read, or it cannot open the file it was "
          "just handed", "Read" in mod.ALLOWED_TOOLS)
    old = mod.INCOMING_MEDIA_DIR / "ancient.jpg"
    old.write_bytes(b"x")
    os.utime(old, (0, 0))
    mod._prune_incoming_media()
    check("old downloads are pruned, so the directory does not grow forever "
          "on a disk nobody is watching", not old.exists())
    check("...while recent ones are kept", Path(written[0]).exists())

    # --- tagging the bot while sending a picture, in a GROUP ---------------
    # Asked directly: "bagaimana agar bisa nge tag bot ketika kirim gambar?"
    # The honest answer was that you could not. Telegram puts a photo's text
    # in msg.caption and its mentions in msg.caption_entities, while the group
    # gate looked only at msg.text/msg.entities -- so an @mention on a
    # screenshot matched nothing, the message was not a reply either, and
    # handle_message returned without a word. The same silent-drop as before,
    # just moved into groups.
    BOT = "bscloud_agent_bot"

    def group_photo(caption, entities, photo=True):
        ent = [SimpleNamespace(type="mention", offset=o, length=l) for o, l in entities]
        msg = SimpleNamespace(text=None, caption=caption, caption_entities=ent,
                              entities=None, reply_to_message=None,
                              photo=[SimpleNamespace(file_id="BIG", file_size=9)] if photo else None,
                              document=None, reply_text=AsyncMock())
        return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                               effective_user=SimpleNamespace(id=111),
                               effective_chat=SimpleNamespace(id=-999, type="supergroup",
                                                              title="g"))

    def gctx(written):
        return SimpleNamespace(
            bot=SimpleNamespace(username=BOT, id=777,
                                get_file=AsyncMock(return_value=tg_file(written)),
                                send_chat_action=AsyncMock(), send_message=AsyncMock()),
            args=[])

    mod.ALLOWED_GROUP_IDS = {-999}
    cap = f"@{BOT} tolong lihat ini"
    u = group_photo(cap, [(0, len(BOT) + 1)])
    got = {}
    async def cap_turn(update, context, text, **kw):
        got["text"] = text
    written = []
    with patch.object(mod, "_run_turn", side_effect=cap_turn),          patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)),          patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u, gctx(written))

    check("tagging the bot in a photo's CAPTION wakes it in a group -- the "
          "mention lives in caption_entities, which the gate used to ignore",
          "text" in got)
    check("...the caption survives as the question",
          "tolong lihat ini" in got.get("text", ""))
    check("...with the @mention itself stripped, not left in the prompt",
          BOT not in got.get("text", ""))
    check("...and the image is still attached for the model to read",
          str(mod.INCOMING_MEDIA_DIR) in got.get("text", ""))

    # A photo with NO mention and no reply must still be ignored in a group,
    # or every picture posted in a busy room costs a turn.
    u2 = group_photo("just chatting", [])
    with patch.object(mod, "_run_turn", side_effect=AssertionError("must not answer")),          patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)),          patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u2, gctx([]))
    check("an untagged photo in a group is still ignored -- the gate is not "
          "loosened, only taught where captions keep their mentions", True)

    # The mention is stripped BEFORE the image note is attached: entity
    # offsets index the original message, so rewriting first would misalign
    # them the moment anything is prepended rather than appended.
    src2 = Path(SRC).read_text(encoding="utf-8")
    i_strip = src2.index("_strip_entity(text, *mention_span)")
    i_note = src2.index("The user attached an image")
    check("the @mention is stripped before the text is rewritten, so the "
          "offsets still index the string they were measured against",
          i_strip < i_note)

    # --- THE case reported: someone ELSE posted the picture ----------------
    # "kalau ada gambar dikirim orang lain di group, kita tag ke bot, botnya
    # ga respons". Your reply carries no photo at all -- the photo is on the
    # message you replied to -- so looking only at your own message found
    # nothing. And when the reply was nothing but "@botname", stripping the
    # mention left an empty string and the handler returned without a word.
    def reply_to_photo(caption_text, entities, replied_has_photo=True):
        ent = [SimpleNamespace(type="mention", offset=o, length=l) for o, l in entities]
        replied = SimpleNamespace(
            text=None, caption=None, caption_entities=None, entities=None,
            photo=[SimpleNamespace(file_id="THEIRS", file_size=9000)] if replied_has_photo else None,
            document=None, reply_to_message=None,
            from_user=SimpleNamespace(id=555))
        msg = SimpleNamespace(text=caption_text, caption=None, caption_entities=None,
                              entities=ent, photo=None, document=None,
                              reply_to_message=replied, reply_text=AsyncMock())
        return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                               effective_user=SimpleNamespace(id=111),
                               effective_chat=SimpleNamespace(id=-999, type="supergroup",
                                                              title="g"))

    mod.ALLOWED_GROUP_IDS = {-999}
    BOT2 = "bscloud_agent_bot"

    def gctx2(written):
        return SimpleNamespace(
            bot=SimpleNamespace(username=BOT2, id=777,
                                get_file=AsyncMock(return_value=tg_file(written)),
                                send_chat_action=AsyncMock(), send_message=AsyncMock()),
            args=[])

    got2 = {}
    async def cap2(update, context, text, **kw):
        got2["text"] = text
    w = []
    u = reply_to_photo(f"@{BOT2} ini kenapa ya", [(0, len(BOT2) + 1)])
    with patch.object(mod, "_run_turn", side_effect=cap2),          patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)),          patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u, gctx2(w))
    check("replying to SOMEONE ELSE'S photo with an @mention picks up that "
          "photo -- it is on the replied-to message, not on yours",
          str(mod.INCOMING_MEDIA_DIR) in got2.get("text", ""))
    check("...and your words come through as the question",
          "ini kenapa ya" in got2.get("text", ""))

    # A reply that is ONLY the mention: used to strip to empty and return in
    # silence, which is exactly what "botnya ga respons" looked like.
    got3 = {}
    async def cap3(update, context, text, **kw):
        got3["text"] = text
    u2 = reply_to_photo(f"@{BOT2}", [(0, len(BOT2) + 1)])
    with patch.object(mod, "_run_turn", side_effect=cap3),          patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)),          patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u2, gctx2([]))
    check("a reply that is ONLY the @mention still answers, instead of "
          "stripping to an empty string and returning in silence",
          "text" in got3)
    check("...asking about the image it found", str(mod.INCOMING_MEDIA_DIR) in got3.get("text", ""))

    # No photo anywhere: a bare mention with nothing to look at stays quiet,
    # rather than sending the model an empty prompt.
    u3 = reply_to_photo(f"@{BOT2}", [(0, len(BOT2) + 1)], replied_has_photo=False)
    with patch.object(mod, "_run_turn", side_effect=AssertionError("nothing to ask about")),          patch.object(mod, "_handle_wizard_input", new=AsyncMock(return_value=False)),          patch.object(mod, "_handle_server_input", new=AsyncMock(return_value=False)):
        await mod.handle_message(u3, gctx2([]))
    check("a bare mention with no image anywhere still costs nothing", True)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
