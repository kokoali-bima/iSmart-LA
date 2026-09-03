#!/usr/bin/env python3
"""Tests for per-chat serialisation + process-wide capping of model turns.

Before this, _run_turn called run_combo directly -- a plain synchronous
function that shells out to agy/claude and blocks for minutes. One live turn
on bscloud ran 00:31:11 -> 00:35:46, four minutes thirty-five seconds, and for
every second of it the whole asyncio loop was blocked: the bot could not
answer /status, could not accept /cancel, and could not serve any other chat.
python-telegram-bot's default (concurrent_updates unset) serialised updates on
top of that, so the two together made the bot strictly one-turn-at-a-time.

Fourteen other blocking calls in the same file already went through an
executor. The single longest one did not.

What is asserted here: the loop stays responsive while a turn runs, different
chats overlap, the SAME chat does not (two turns there share a session file
and per-tier conversation ids -- overlapping them would clobber the resume
handles, the same class of bug v0.2b.39 fixed), and MAX_CONCURRENT_TURNS is a
real ceiling rather than decoration.
"""
import atexit
import shutil as _shutil
import asyncio, importlib.util, os, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_conc_"))
# Tests must not litter the machine they run on: 485 stale
# isla_* directories were found on a real server after a few days
# of runs. Registered rather than done at the end, so a failing
# assertion still cleans up.
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

# LEDGER_FILE/MEMORY_DIR/MEMORY_FILE are BASE_DIR-relative (the module's own
# directory), NOT HOME-relative -- overriding HOME above does not sandbox
# them. This suite drives real _run_turn() calls (only run_combo is mocked),
# which write a real ledger row per turn. Without this, running it against a
# real checkout (SRC pointing at the actual repo, not a scratch copy) wrote
# spend.jsonl straight into the repo directory -- confirmed live: exactly
# that file turned up in `git status` after a local run.
mod.LEDGER_FILE = scratch / "spend.jsonl"
mod.MEMORY_DIR = scratch / "memory"
mod.MEMORY_FILE = scratch / "MEMORY.md"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

def upd(chat_id):
    msg = SimpleNamespace(text="hi", reply_text=AsyncMock())
    return SimpleNamespace(
        message=msg, effective_message=msg, callback_query=None,
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=chat_id, type="private", title=None),
    )

def ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

TURN_SECONDS = 0.4

def make_tracker():
    """A blocking fake run_combo that records overlap, like the real one would."""
    state = {"live": 0, "peak": 0, "order": []}
    def fake(text, sess, active, forced_tier=None, **kw):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        state["order"].append(("start", text))
        time.sleep(TURN_SECONDS)          # blocking on purpose -- that's the point
        state["live"] -= 1
        state["order"].append(("end", text))
        return {"result": "ok", "usage": {}}, "claude-haiku-4-5-20251001", []
    return fake, state

def driving(fake):
    return [
        patch.object(mod, "run_combo", side_effect=fake),
        patch.object(mod, "load_sessions", return_value={}),
        patch.object(mod, "get_chat_state",
                     return_value={"active": "default", "sessions": {"default": {}}}),
        patch.object(mod, "save_sessions"),
        patch.object(mod, "append_learned", return_value=[]),
        patch.object(mod, "register_snapshot"),
        patch.object(mod, "_maybe_notify_update", new=AsyncMock()),
    ]

async def run_turns(chat_ids, fake):
    ps = driving(fake)
    for p in ps: p.start()
    try:
        t0 = time.monotonic()
        await asyncio.gather(*(mod._run_turn(upd(c), ctx(), f"msg-{i}")
                               for i, c in enumerate(chat_ids)))
        return time.monotonic() - t0
    finally:
        for p in ps: p.stop()

async def main():
    # --- 1. the loop stays responsive while a turn runs (THE bug) ----------
    ticks = {"n": 0}
    async def ticker(stop):
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.02)
    stop = asyncio.Event()
    t = asyncio.create_task(ticker(stop))
    fake, _ = make_tracker()
    await run_turns([1], fake)
    stop.set(); await t
    check("the event loop keeps running during a turn -- other chats and "
          "zero-token commands are no longer frozen (THE bug)",
          ticks["n"] >= 5)

    # --- 2. different chats overlap ---------------------------------------
    mod._turn_slots = None
    mod._chat_turn_locks.clear()
    fake, st = make_tracker()
    elapsed = await run_turns([101, 202, 303], fake)
    check("three DIFFERENT chats actually run in parallel", st["peak"] >= 2)
    check("...and finish in well under the serial time",
          elapsed < TURN_SECONDS * 3 * 0.8)

    # --- 3. the same chat does NOT overlap --------------------------------
    mod._turn_slots = None
    mod._chat_turn_locks.clear()
    fake2, st2 = make_tracker()
    await run_turns([777, 777, 777], fake2)
    check("three turns in the SAME chat never overlap (session/handle safety)",
          st2["peak"] == 1)
    starts_ends = "".join("S" if k == "start" else "E" for k, _ in st2["order"])
    check("...they strictly alternate start/end, never interleave",
          starts_ends == "SESESE")

    # --- 4. the cap is real ------------------------------------------------
    mod._turn_slots = None
    mod._chat_turn_locks.clear()
    old = mod.MAX_CONCURRENT_TURNS
    mod.MAX_CONCURRENT_TURNS = 2
    try:
        fake3, st3 = make_tracker()
        await run_turns([11, 22, 33, 44, 55], fake3)
        check("MAX_CONCURRENT_TURNS caps real concurrency (memory ceiling)",
              st3["peak"] <= 2)
    finally:
        mod.MAX_CONCURRENT_TURNS = old
        mod._turn_slots = None
        mod._chat_turn_locks.clear()

    # --- 5. all five turns still actually completed ------------------------
    check("every queued turn still ran -- capping delays, never drops",
          len([1 for k, _ in st3["order"] if k == "end"]) == 5)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
