#!/usr/bin/env python3
"""The model must be told what THIS BUILD can do, not what the install could.

An operator asked the bot in a group for a cut of a music video. It answered
that it "can only send local media files stored on disk" and offered YouTube
links instead. That looked like the model being unhelpful. It was not.

Checked on the real host: GEMINI.md and SOUL.md there were written on 2 Sep by
the install, and `install.sh` copies the templates only when the file does not
already exist (`[ -f ... ] || cp`), while /update never touches them at all --
correctly, because they hold the operator's own servers, tone and rules, and
overwriting them would throw that away.

So the brief still described the feature set of the day the box was set up.
Grepping it: GDRIVE_DELETE 0, GDRIVE_MOVE 0, yt-dlp 0, ffmpeg 0. Every
capability shipped after that install was invisible to the model, and no
rephrasing of the request could have reached it. The model was reporting its
instructions accurately.

Capability documentation therefore lives in code, next to what implements it,
and rides along with /update. This suite keeps it wired to both CLIs and keeps
it off resumed turns, where it would be paid for again for nothing.
"""
import atexit
import importlib.util
import os
import shutil as _shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

SRC = sys.argv[1]
scratch = Path(tempfile.mkdtemp(prefix="isla_capbrief_"))
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)
mod.LEDGER_FILE = scratch / "spend.jsonl"

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


brief = mod.CAPABILITIES_BRIEF

# --- the content the stale briefs were missing ----------------------------
for token, why in (
    ("yt-dlp", "fetching a video at all"),
    ("ffmpeg", "cutting and encoding"),
    ("MEDIA:", "the marker that actually sends the file"),
    ("GDRIVE_DELETE:", "deleting in Drive"),
    ("GDRIVE_MOVE:", "moving in Drive"),
):
    check(f"the brief covers {token} ({why})", token in brief)

# The specific failure the operator hit twice: a clip that arrives silent, and
# a bot that says it cannot send media.
check("it says to keep the audio stream (-c:a aac), the reason meme clips "
      "came back mute", "-c:a aac" in brief)
check("it forbids claiming the bot cannot send media",
      "never" in brief.lower() and "unable to send media" in brief.lower())
check("it carries the measured AV1 finding, so 'cut before encoding' reads "
      "as a fact rather than a preference", "67MB" in brief and "AV1" in brief)
check("it sets the ~10 second default for a meme or highlight",
      "10 seconds" in brief and "30" in brief)

# --- agy: injected when a conversation starts, never on a resumed turn ----
fresh = mod._build_agy_prompt("hi", include_env=True)
resumed = mod._build_agy_prompt("hi", include_env=False)
check("agy gets the capabilities when a conversation STARTS",
      "yt-dlp" in fresh and "MEDIA:" in fresh)
check("...and not again on a resumed turn (it is already in the history; "
      "re-sending it every turn is pure token waste)", "yt-dlp" not in resumed)

# It must not depend on GEMINI.md existing -- a host whose brief is missing
# entirely is exactly the host that needs this most.
mod.GEMINI_PROMPT_FILE = scratch / "definitely-absent.md"
check("...and it survives a missing GEMINI.md, rather than being skipped "
      "along with it", "yt-dlp" in mod._build_agy_prompt("hi", include_env=True))


# --- claude: same rule, via --append-system-prompt ------------------------
def claude_cmd(session_id):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        raise RuntimeError("stop here -- the command line is all we need")

    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        try:
            mod._run_claude_once("hi", session_id, "s", "sonnet")
        except Exception:
            pass
    return seen.get("cmd", [])


cmd_fresh = claude_cmd(None)
cmd_resumed = claude_cmd("sess-123")


def appended(cmd):
    if "--append-system-prompt" not in cmd:
        return ""
    return cmd[cmd.index("--append-system-prompt") + 1]


check("claude gets the capabilities on a FRESH session",
      "yt-dlp" in appended(cmd_fresh))
check("...passed via --append-system-prompt", "--append-system-prompt" in cmd_fresh)
check("...and NOT on a resumed session", "yt-dlp" not in appended(cmd_resumed))
check("...and a resumed session still resumes", "--resume" in cmd_resumed)

# --- token cost, stated rather than assumed -------------------------------
# Roughly 4 chars per token. This is paid once per conversation, on both CLIs.
approx = len(brief) / 4
check(f"the brief stays small enough to be worth its place "
      f"(~{approx:.0f} tokens, once per conversation)", approx < 800)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
