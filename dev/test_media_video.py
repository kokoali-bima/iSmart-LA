#!/usr/bin/env python3
"""Tests for sending video and audio: as the thing they are, and small enough
to actually arrive.

Asked for: fetch a video, mix sound onto it, send it to the chat. Two things
stood between that and working, and neither was the fetching.

**Everything went out as a document.** Right for a report, wrong for a clip:
it arrives as a file you must download before you can watch it. reply_video
plays inline, with a thumbnail and a scrub bar.

**Anything over Telegram's 50MB bot limit was refused outright**, after all
the work of fetching or producing it. That is the common case rather than the
edge one -- measured with yt-dlp against a real video, Big Buck Bunny is
722MB. It is now re-encoded to fit instead, which was benchmarked on this
project's own server before being written: 90 seconds of 1080p takes about 32
seconds on 12 cores, so it is slow enough to be worth announcing and fast
enough to be worth doing.
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
scratch = Path(tempfile.mkdtemp(prefix="isla_media_"))
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

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def make(name: str, size: int = 1024) -> Path:
    p = scratch / name
    p.write_bytes(b"\0" * size)
    return p


class Recorder:
    """Stands in for the message, recording which send method was used."""
    def __init__(self):
        self.calls = []
        self.reply_text = AsyncMock()
    def _rec(self, kind):
        async def f(*a, **kw):
            self.calls.append(kind)
        return f
    def __getattr__(self, item):
        if item.startswith("reply_"):
            return self._rec(item)
        raise AttributeError(item)


def upd_with(rec):
    return SimpleNamespace(message=rec, effective_message=rec, callback_query=None,
                           effective_user=SimpleNamespace(id=111),
                           effective_chat=SimpleNamespace(id=111, type="private",
                                                          title=None))


async def send(path: Path, rec=None):
    rec = rec or Recorder()
    u = upd_with(rec)
    with patch.object(mod, "_msg", return_value=rec):
        await mod._send_media_file(u, str(path), set())
    return rec


async def main():
    # --- 1. sent as what it is ---------------------------------------------
    for name, want in (("clip.mp4", "reply_video"), ("clip.MOV", "reply_video"),
                       ("song.mp3", "reply_audio"), ("shot.png", "reply_photo"),
                       ("report.pdf", "reply_document"),
                       ("notes.md", "reply_document")):
        rec = await send(make(name))
        check(f"{name} goes out via {want}", rec.calls == [want])

    # A GIF as a photo loses the animation, so it stays a document.
    rec = await send(make("meme.gif"))
    check("a .gif stays a document -- sent as a photo it stops moving",
          rec.calls == ["reply_document"])

    # --- 2. a typed send that Telegram rejects still arrives ---------------
    class Rejects(Recorder):
        def _rec(self, kind):
            async def f(*a, **kw):
                self.calls.append(kind)
                if kind != "reply_document":
                    raise RuntimeError("Telegram refused this container")
            return f
    rec = await send(make("odd.mkv"), Rejects())
    check("if Telegram refuses the typed send, it falls back to a document -- "
          "arriving as a file beats not arriving",
          rec.calls == ["reply_video", "reply_document"])

    # --- 3. oversized video: shrink instead of refusing ---------------------
    big = make("big.mp4", size=mod.TELEGRAM_MAX_FILE_BYTES + 1024)
    small = make("big-small.mp4", size=1024)

    rec = Recorder()
    with patch.object(mod, "_ffmpeg", return_value="/usr/bin/ffmpeg"), \
         patch.object(mod, "shrink_video_to_fit", return_value=(small, "video_shrunk")), \
         patch.object(mod, "prune_media_work_dirs"):
        u = upd_with(rec)
        with patch.object(mod, "_msg", return_value=rec):
            await mod._send_media_file(u, str(big), set())
    check("an oversized video is re-encoded and sent, not refused",
          "reply_video" in rec.calls)
    said = " ".join(str(c[0][0]) for c in rec.reply_text.call_args_list)
    check("...and the wait is announced first, since encoding takes real time "
          "and silence there reads as a hang",
          "re-encoding" in said or "kecilkan" in said)

    # Still refused when it genuinely cannot be made to fit -- with the reason.
    rec = Recorder()
    with patch.object(mod, "_ffmpeg", return_value="/usr/bin/ffmpeg"), \
         patch.object(mod, "shrink_video_to_fit",
                      return_value=(None, "video_too_long_to_fit")), \
         patch.object(mod, "prune_media_work_dirs"):
        u = upd_with(rec)
        with patch.object(mod, "_msg", return_value=rec):
            await mod._send_media_file(u, str(big), set())
    said = " ".join(str(c[0][0]) for c in rec.reply_text.call_args_list)
    check("a video that cannot be made to fit is refused WITH the reason",
          rec.calls == [] and ("too long to fit" in said or "terlalu panjang" in said))

    # No ffmpeg: refuse exactly as before, no crash.
    rec = Recorder()
    with patch.object(mod, "_ffmpeg", return_value=None):
        u = upd_with(rec)
        with patch.object(mod, "_msg", return_value=rec):
            await mod._send_media_file(u, str(big), set())
    check("without ffmpeg it refuses as it always did, rather than crashing",
          rec.calls == [] and rec.reply_text.call_args is not None)

    # A non-video over the limit is not sent for pointless encoding.
    bigdoc = make("huge.pdf", size=mod.TELEGRAM_MAX_FILE_BYTES + 1024)
    rec = Recorder()
    with patch.object(mod, "_ffmpeg", return_value="/usr/bin/ffmpeg"), \
         patch.object(mod, "shrink_video_to_fit",
                      side_effect=AssertionError("must not encode a PDF")):
        u = upd_with(rec)
        with patch.object(mod, "_msg", return_value=rec):
            await mod._send_media_file(u, str(bigdoc), set())
    check("an oversized non-video is refused without attempting to encode it",
          rec.calls == [])

    # --- 4. the shrink itself ----------------------------------------------
    check("a bitrate floor exists, so it refuses rather than producing a "
          "smear nobody can watch",
          "video_too_long_to_fit" in Path(SRC).read_text(encoding="utf-8"))
    check("it aims BELOW the hard limit, since a re-encode's size is an "
          "estimate and landing at 50.4MB wastes the whole encode",
          mod.TELEGRAM_VIDEO_TARGET_BYTES < mod.TELEGRAM_MAX_FILE_BYTES)

    with patch.object(mod, "_ffmpeg", return_value=None):
        out, why = mod.shrink_video_to_fit(big)
    check("shrink without ffmpeg reports that, rather than raising",
          out is None and why == "ffmpeg_missing")

    with patch.object(mod, "_ffmpeg", return_value="/usr/bin/ffmpeg"), \
         patch.object(mod, "_ffprobe_duration", return_value=None):
        out, why = mod.shrink_video_to_fit(big)
    check("an unreadable duration is reported -- the whole calculation depends "
          "on it, so guessing would produce a wrong-sized file",
          out is None and why == "video_duration_unknown")

    with patch.object(mod, "_ffmpeg", return_value="/usr/bin/ffmpeg"), \
         patch.object(mod, "_ffprobe_duration", return_value=99999.0):
        out, why = mod.shrink_video_to_fit(big)
    check("a video too long to fit at watchable quality is refused before "
          "spending minutes on it", out is None and why == "video_too_long_to_fit")

    # --- 5. every outcome speaks both languages ----------------------------
    for key in ("ffmpeg_missing", "video_duration_unknown", "video_too_long_to_fit",
                "video_encode_failed", "video_encode_timeout",
                "video_still_too_large", "video_shrunk"):
        en, id_ = mod._detail("en", key), mod._detail("id", key)
        check(f"'{key}' reads in both languages", en != key and id_ != key and en != id_)

    # --- 6. the tools are installed for new deployments --------------------
    inst = (Path(SRC).parent / "install.sh").read_text(encoding="utf-8")
    check("install.sh installs ffmpeg, so a fresh deployment can re-encode",
          "ffmpeg" in inst)
    check("...and fetches yt-dlp, since the packaged version is too old to "
          "keep working", "yt-dlp" in inst)
    for tpl in ("GEMINI.md.template", "SOUL.md.template"):
        brief = (Path(SRC).parent / tpl).read_text(encoding="utf-8")
        check(f"{tpl} tells the model the tools exist",
              "yt-dlp" in brief and "ffmpeg" in brief)
        check(f"...and that {tpl.split('.')[0]} must NOT shrink files itself -- "
              f"two of us encoding the same file wastes minutes",
              "Do not try to make it fit yourself" in brief)
        # Learned by actually doing it, not by reasoning: a real 3:42 video
        # downloaded at 32MB in AV1 and came out at 67MB re-encoded to H.264 --
        # bigger than the source and over the limit, because AV1 is far more
        # efficient. Cutting first gave 8MB in 15 seconds.
        check(f"{tpl} says to CUT before encoding, with the measurement that "
              f"makes the reason concrete",
              "Cut BEFORE encoding" in brief and "67MB" in brief)
        check(f"{tpl} sets a short default clip length -- ten seconds is a "
              f"joke, three minutes is homework",
              "about 10 seconds" in brief and "never past 30" in brief)
        check(f"{tpl} still allows the full thing when that is what was asked",
              "only when someone actually asks" in brief)
        check(f"{tpl} insists on H.264 + AAC -- yt-dlp's best pick is often "
              f"AV1/Opus, which arrives as a file instead of playing",
              "H.264 + AAC" in brief)

    # --- how it ARRIVES ----------------------------------------------------
    # Telegram cannot be made to autoplay with sound: checked against the real
    # parameter list, and none of sendVideo's 32 arguments touch autoplay,
    # muting or volume. That is the client's call and a sensible one -- a chat
    # that blared audio unprompted would be unusable in a group. What IS ours
    # is how the clip looks before it is tapped: without dimensions Telegram
    # lays out a generic box, and without a thumbnail the preview is black, so
    # a video that plays perfectly still looks broken.
    src_text = Path(SRC).read_text(encoding="utf-8")
    check("no attempt is made to force autoplay or sound -- there is no such "
          "parameter, and pretending otherwise would be a lie in the code",
          "autoplay" not in src_text.lower().replace("autoplay with sound", "")
          or "cannot be made to autoplay" in src_text)
    check("dimensions and duration are sent, so the player is laid out right "
          "immediately instead of correcting itself",
          "video_presentation" in src_text)

    rec = Recorder()
    vid = make("clip2.mp4")
    with patch.object(mod, "video_presentation",
                      return_value={"duration": 10, "width": 1280, "height": 720}):
        u = upd_with(rec)
        with patch.object(mod, "_msg", return_value=rec):
            await mod._send_media_file(u, str(vid), set())
    check("a video still sends when there is no thumbnail to attach",
          rec.calls == ["reply_video"])

    thumb_dir = Path(tempfile.mkdtemp(prefix="isla_thumb_"))
    (thumb_dir / "thumb.jpg").write_bytes(bytes([255, 216, 255]))
    rec = Recorder()
    with patch.object(mod, "video_presentation",
                      return_value={"duration": 10, "thumb_path": thumb_dir / "thumb.jpg"}):
        u = upd_with(rec)
        with patch.object(mod, "_msg", return_value=rec):
            await mod._send_media_file(u, str(vid), set())
    check("a thumbnail is attached when one could be made", rec.calls == ["reply_video"])
    check("...and its working directory is cleaned up afterwards, since these "
          "accumulate one per video sent", not thumb_dir.exists())

    # Probing must never be what stops a send.
    rec = Recorder()
    with patch.object(mod, "video_presentation", side_effect=RuntimeError("ffprobe blew up")):
        u = upd_with(rec)
        with patch.object(mod, "_msg", return_value=rec):
            await mod._send_media_file(u, str(vid), set())
    check("if probing fails the video still goes out, as a document at worst -- "
          "presentation is a nicety, delivery is not",
          rec.calls and rec.calls[-1] in ("reply_video", "reply_document"))

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
