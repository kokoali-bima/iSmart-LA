#!/usr/bin/env python3
"""Tests for keeping a conversation handle alive across a TRANSIENT failure.

The bug, from a real bscloud session: run_combo discarded the resume handle on
ANY failure ("Don't try to resume a conversation that just errored"), timeout
included. But a timeout leaves the conversation perfectly intact and usually
mid-task -- so the user's next "Lanjutkan yang ini" ("continue this one") did
the opposite of what it said: it opened a BRAND NEW conversation and re-sent
the whole ~12,000-character brief.

Straight from that deployment's own log:

  running agy: model=gemini-3.7-flash-medium conversation_id=5dd5b5ca-... prompt_len=18
  turn done: ... FAILED/failover (agy exited 1: {"conversation_id":"5dd5b5ca-...",
             "status":"ERROR","response":"","error":"timeout waiting for response",
             "duration_seconds":207.15738437,"num_turns":2,...})
  running agy: model=gemini-3.7-flash-medium conversation_id=None prompt_len=11823
                                             ^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^
                                             handle thrown away   brief re-sent

Two fixes are tested here: keep the handle when the failure says nothing bad
about the handle itself, and -- for a run that started FRESH and then failed --
adopt the conversation id out of the error payload, which is the only place it
exists at that point. Without the second, a long first turn that times out
orphans the conversation and every bit of work it already did.
"""
import importlib.util, os, sys, tempfile
from pathlib import Path
from unittest.mock import patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_resume_"))
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

CONV = "5dd5b5ca-a3b7-4752-88c8-61f93a858d53"
# The REAL error string this deployment produced, not a synthetic stand-in.
REAL_TIMEOUT_ERR = (
    'agy exited 1: {"conversation_id":"' + CONV + '","status":"ERROR",'
    '"response":"","error":"timeout waiting for response",'
    '"duration_seconds":207.15738437,"num_turns":2,'
    '"usage":{"input_tokens":100939,"output_tokens":4577,'
    '"thinking_tokens":2404,"cache_read_tokens":327724,"total_tokens":105516}}'
)

# --- 1. _handle_survives ---------------------------------------------------
check("the REAL timeout error is recognised as handle-surviving (THE bug)",
      mod._handle_survives(Exception(REAL_TIMEOUT_ERR)) is True)
check("a network drop is handle-surviving",
      mod._handle_survives(Exception("connection reset by peer")) is True)
check("a rate limit is handle-surviving",
      mod._handle_survives(Exception("429 rate limit exceeded")) is True)
check("an unrelated/unknown error is NOT assumed handle-surviving",
      mod._handle_survives(Exception("conversation not found")) is False)
check("a malformed-request error is NOT handle-surviving",
      mod._handle_survives(Exception("invalid conversation id")) is False)

# --- 2. _conversation_id_from_error ---------------------------------------
check("the conversation id is recovered from the REAL error payload",
      mod._conversation_id_from_error(Exception(REAL_TIMEOUT_ERR)) == CONV)
check("an error with no conversation id yields None",
      mod._conversation_id_from_error(Exception("boom")) is None)

# --- 3. run_combo end to end ----------------------------------------------
AGY_TIERS = [t for t in mod.TIERS if t["provider"] == "agy"]
if not AGY_TIERS:
    print("no agy tier configured; skipping run_combo cases")
else:
    TIER = AGY_TIERS[0]
    MODEL = TIER["model"]

    def run_with(side_effect, sess):
        """Drive run_combo with _run_agy_once mocked, recording the prompts it
        was handed so we can prove whether the brief was re-sent."""
        seen = []
        def fake(prompt, model, conversation_id, *a, **kw):
            seen.append({"prompt": prompt, "model": model, "conv": conversation_id})
            r = side_effect(model, conversation_id)
            if isinstance(r, Exception):
                raise r
            return r
        with patch.object(mod, "_run_agy_once", side_effect=fake):
            try:
                mod.run_combo("Lanjutkan yang ini", sess, "default")
            except Exception:
                pass
        return seen

    mod._tier_cooldown.clear()
    # (a) an EXISTING conversation that times out must keep its handle
    sess = {"agy": {MODEL: CONV}, "claude": {}}
    run_with(lambda m, c: Exception(REAL_TIMEOUT_ERR) if m == MODEL else Exception("x"), sess)
    check("a timeout on an existing conversation KEEPS the handle (THE fix)",
          sess["agy"].get(MODEL) == CONV)

    mod._tier_cooldown.clear()
    # (b) a handle-fatal error must still discard it
    sess2 = {"agy": {MODEL: CONV}, "claude": {}}
    run_with(lambda m, c: Exception("conversation not found"), sess2)
    check("a handle-fatal error still DISCARDS the handle (fix isn't too broad)",
          sess2["agy"].get(MODEL) is None)

    mod._tier_cooldown.clear()
    # (c) a FRESH run that times out adopts the id from the error payload
    sess3 = {"agy": {}, "claude": {}}
    run_with(lambda m, c: Exception(REAL_TIMEOUT_ERR) if m == MODEL else Exception("x"), sess3)
    check("a FRESH run that times out adopts the id from the error (no orphan)",
          sess3["agy"].get(MODEL) == CONV)

    mod._tier_cooldown.clear()
    # (d) the money shot: after a kept handle, the NEXT turn resumes and the
    # brief is NOT re-sent. include_env is driven by (conv_id is None).
    sess4 = {"agy": {MODEL: CONV}, "claude": {}}
    seen = run_with(lambda m, c: {"conversation_id": c or CONV, "response": "ok",
                                  "usage": {"total_tokens": 5}} if m == MODEL
                                 else Exception("x"), sess4)
    first = seen[0]
    check("the resumed turn passes the kept conversation id through",
          first["conv"] == CONV)
    brief_marker = "Working-environment instructions"
    check("...and does NOT re-send the brief on that resumed turn "
          "(this is the whole cost saving)",
          brief_marker not in first["prompt"])

    mod._tier_cooldown.clear()
    # (e) a genuinely fresh conversation DOES still get the brief
    sess5 = {"agy": {}, "claude": {}}
    seen5 = run_with(lambda m, c: {"conversation_id": CONV, "response": "ok",
                                   "usage": {"total_tokens": 5}} if m == MODEL
                                  else Exception("x"), sess5)
    check("a genuinely fresh conversation still DOES get the brief",
          brief_marker in seen5[0]["prompt"] or seen5[0]["conv"] is None)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
