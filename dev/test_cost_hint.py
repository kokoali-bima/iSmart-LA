#!/usr/bin/env python3
"""The cost hint, and the never-worse guard behind it.

A week of real usage was measured before this suite was written, and it said
something uncomfortable: 122,038,414 tokens across 145 turns, and ONE
conversation took 46.4% of it -- 56.7M tokens over 11 turns, 5.15M per turn.
Two conversations took 69.8%. The median conversation cost 572k.

So the money is not spread across usage. It is a couple of runaways, and the
hint that exists to catch exactly that fired ONCE, early in the climb, and then
stayed silent through the whole expensive part. The fix is doubling: warn at the
threshold, then again whenever the per-turn cost doubles. Across that 5.15M
conversation that is about five notes, not one and not eleven.

The second half of this file guards the same principle one layer down: a step
whose whole purpose is to make something smaller must never hand back something
bigger. Measured here: a 32MB AV1 clip re-encoded to H.264 came back at 67MB.
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
scratch = Path(tempfile.mkdtemp(prefix="isla_costhint_"))
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


BASE = mod.TURN_COST_HINT_TOKENS
check(f"the base threshold is set ({BASE:,} tokens)", BASE > 0)


def fire(sess, turn_in):
    """Run the rule and apply its side effect the way the turn handler does."""
    due, prev = mod.cost_hint_due(sess, turn_in)
    if due:
        sess["cost_hint_at"] = turn_in
        sess["cost_hint_shown"] = True
    return due


# --- the basic ladder ------------------------------------------------------
s = {}
check("a cheap turn says nothing", not fire(s, BASE - 1))
check("crossing the threshold warns once", fire(s, BASE))
check("...and does NOT warn again on the very next turn at the same cost",
      not fire(s, BASE))
check("...nor on a turn that grew but has not doubled",
      not fire(s, int(BASE * 1.9)))
check("doubling warns again", fire(s, BASE * 2))
check("...then goes quiet until the NEXT doubling",
      not fire(s, BASE * 3))
check("...which warns", fire(s, BASE * 4))

# --- the runaway this was written for --------------------------------------
# The real conversation: 11 turns, 56.7M tokens, 5.15M per turn. Model it as a
# cost that climbs steadily and count how often the person is told.
s2 = {}
notes = 0
per_turn = 5_151_059
for turn in range(1, 12):
    if fire(s2, int(per_turn * turn / 11)):
        notes += 1
check(f"the 56.7M-token runaway produces a handful of notes, not one "
      f"({notes} across 11 turns)", 2 <= notes <= 6)
check("...and the old once-per-session rule would have produced exactly one",
      notes > 1)

# A conversation that never gets expensive is never interrupted.
s3 = {}
quiet = sum(1 for _ in range(40) if fire(s3, 40_000))
check("a conversation that stays cheap is never interrupted", quiet == 0)

# --- upgrading from the old flag -------------------------------------------
# Sessions written by the previous build carry cost_hint_shown and no level.
old = {"cost_hint_shown": True}
check("an upgraded session is not re-warned at its old level",
      not fire(dict(old), BASE))
check("...but is warned once it doubles past it", fire(dict(old), BASE * 2))

# --- the off switch --------------------------------------------------------
with patch.object(mod, "TURN_COST_HINT_TOKENS", 0):
    check("setting the threshold to 0 disables the hint entirely",
          not fire({}, 10_000_000))

# --- never worse: the shrink that grew -------------------------------------
_MSG = getattr(mod, "_MSG", {})
check("the 'shrink made it bigger' reason is translated in both languages",
      "video_shrink_made_it_bigger" in _MSG
      and len(_MSG["video_shrink_made_it_bigger"]) == 2
      and all(_MSG["video_shrink_made_it_bigger"]))

src_text = Path(SRC).read_text(encoding="utf-8")
check("the shrink compares against the ORIGINAL, not only the send limit",
      "out.stat().st_size >= path.stat().st_size" in src_text)


def run_shrink(original_bytes, produced_bytes):
    """Drive shrink_video_to_fit with ffmpeg stubbed, and real file sizes."""
    work = Path(tempfile.mkdtemp(dir=scratch))
    src = work / "in.mp4"
    src.write_bytes(b"\0" * original_bytes)

    def fake_run(argv, **kw):
        # second pass writes the output file
        for i, a in enumerate(argv):
            if a == "-pass" and i + 1 < len(argv) and argv[i + 1] == "2":
                Path(argv[-1]).write_bytes(b"\0" * produced_bytes)
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(mod, "_ffmpeg", return_value="/usr/bin/ffmpeg"), \
            patch.object(mod, "_ffprobe_duration", return_value=120.0), \
            patch.object(mod.subprocess, "run", side_effect=fake_run):
        return mod.shrink_video_to_fit(src, target=10 * 1024 * 1024)


out, reason = run_shrink(4 * 1024 * 1024, 8 * 1024 * 1024)
check("a re-encode that GREW the file is refused, not sent as a 'shrink'",
      out is None and reason == "video_shrink_made_it_bigger")

out, reason = run_shrink(8 * 1024 * 1024, 2 * 1024 * 1024)
check("...while a genuine shrink still comes back", out is not None
      and reason == "video_shrunk")

out, reason = run_shrink(4 * 1024 * 1024, 4 * 1024 * 1024)
check("a re-encode that changed nothing is refused too -- spending two "
      "minutes to produce the same size is not a win",
      out is None and reason == "video_shrink_made_it_bigger")

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
