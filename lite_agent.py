#!/usr/bin/env python3
"""
iSmart-LA (Lite Agent) -- a lightweight Telegram bridge to Claude Code and
Antigravity CLI, with 9Router as the Claude-side gateway.

Instead of reimplementing agent logic, this script shells out to two
first-party CLIs that already do the hard part (tool execution, reasoning,
safety self-gating via a system prompt) and relays the result over Telegram,
keeping per-chat, *named* session mappings so conversations continue
naturally across messages -- including across different days/cases, via
/session.

Design principles:
  - No agentic/retry-capable background processes. Every token spent is
    because the user directly asked for something, right now. Nothing in
    this script decides on its own to re-run, review, or "improve" a past
    turn -- that pattern is what silently burns budget in other agent
    frameworks (see README "Why not automatic memory?").
  - Bounded automation IS allowed when it's a single deterministic operation
    with a clear trigger condition (e.g. a CLI's own built-in context
    auto-compaction). That's a fundamentally different risk class from an
    agent freely deciding what tool calls to retry.
  - Full context every time, not a silent distillation system. SOUL.md /
    GEMINI.md (persona/environment/rules) is passed in full on every call;
    MEMORY.md (curated cross-session facts) is appended on top, but MEMORY.md
    is only ever edited by an explicit /remember command -- never written to
    automatically.
  - Named sessions (/session <name>) let a chat hold multiple independent,
    resumable conversations (one per "case") instead of one ever-growing
    thread -- so picking up yesterday's case back up doesn't require
    dragging in today's unrelated context, and vice versa.
  - Four explicit fallback tiers, cheapest first: Gemini Flash ("mini") ->
    Gemini Pro-low ("mini pro") -> Claude Haiku ("dede iku") -> Claude Sonnet
    ("dede nnet"). BOTH sides are fixed-price subscriptions, not pay-per-
    token API billing: Gemini via the Antigravity CLI (agy) on a Google AI
    Pro/Ultra plan, Claude via 9Router's Claude Code OAuth connection on a
    Claude Pro (or higher) plan. Gemini is tried first anyway -- not to dodge
    per-token cost, but to spread routine load across a SEPARATE subscription
    and keep the Claude plan's own usage quota in reserve for when it's
    genuinely needed.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = BASE_DIR / "sessions.json"
ALLOWED_GROUPS_FILE = BASE_DIR / "allowed_groups.json"
SYSTEM_PROMPT_FILE = BASE_DIR / "SOUL.md"
GEMINI_PROMPT_FILE = BASE_DIR / "GEMINI.md"
MEMORY_FILE = BASE_DIR / "MEMORY.md"
LOG_FILE = BASE_DIR / "lite-agent.log"
# Deterministic collectors: known questions answered by a script, no LLM
# involved, so a repeat of an already-solved case costs zero tokens. See /status.
SNAPSHOT_SCRIPT = BASE_DIR / "tools" / "cluster_snapshot.py"
LIST_TOOLS_SCRIPT = BASE_DIR / "tools" / "list_tools.py"

# /graduate turns the case you *just* solved into a reusable parameterized
# script. It is a single, explicitly-user-triggered call -- never a
# background job. The instruction below is fixed (the user only supplies a
# name), so it cannot drift into an open-ended self-directed loop.
GRADUATE_INSTRUCTION = """Turn the work you JUST finished in this conversation into a reusable script named "{name}".

Required steps, in order:
1. Run `python3 tools/list_tools.py` first. If a skill already answers this same class of question, DO NOT write a new script -- just report which skill already handles it, then stop.
2. If nothing exists yet, write `tools/{name}.py` (Python 3, stdlib only) that:
   - Takes the parts that vary as ARGUMENTS (e.g. --node, --id, --target). NEVER hardcode a specific ID or hostname/IP -- read that mapping from tools/cluster_snapshot.py if needed.
   - Batches queries as much as possible (ideally one round-trip for the whole scope, like cluster_snapshot.py), not one call per target.
   - Prints output that is ALREADY computed and sorted (percentages, rankings), concise -- not raw JSON dumps.
   - Exits non-zero with a clear stderr message on failure.
3. Test-run the script once to prove the output is correct.
4. Register it in `tools/registry.json`: add one entry to the "tools" array with name, script, usage, description, and "answers" (a list of question phrasings it covers).
5. Close with one example invocation.
"""

DEFAULT_SESSION_NAME = "default"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}


def _load_allowed_groups_file() -> set[int]:
    if ALLOWED_GROUPS_FILE.exists():
        try:
            return {int(x) for x in json.loads(ALLOWED_GROUPS_FILE.read_text())}
        except Exception:
            logger.warning("allowed_groups.json unreadable, ignoring", exc_info=True)
    return set()


def _save_allowed_groups_file(ids: set[int]) -> None:
    ALLOWED_GROUPS_FILE.write_text(json.dumps(sorted(ids)))


# Chat IDs (groups/supergroups are negative numbers) where EVERY member is
# authorized, no per-person whitelist -- chosen deliberately over a broader
# "anyone" default given this bot has real SSH/infra access. Two sources,
# merged: a static seed from .env, and a mutable file that /registergroup
# (admin-only, see cmd_registergroup) updates at runtime -- so an admin can
# grant a new group access from inside Telegram itself, no SSH/restart needed.
# This set object is mutated in place by /registergroup, not reassigned, so
# the change is visible immediately without reloading the module.
ALLOWED_GROUP_IDS: set[int] = (
    {int(x) for x in os.environ.get("ALLOWED_GROUP_IDS", "").split(",") if x.strip()}
    | _load_allowed_groups_file()
)

ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:20128")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Explicit literal models, not a 9Router combo alias (e.g. "combo-bayar") --
# a combo hides which underlying model actually answered from us (9Router's
# own JSON always reports the alias, not the resolved sub-model), which would
# break the per-backend "who answered" tag below. Two tiers, mirroring agy's.
# The "cc/" prefix is 9Router's provider tag for the Claude Code OAuth
# connection -- confirmed required directly (a bare model id 404s).
CLAUDE_MODEL_PRIMARY = os.environ.get("CLAUDE_MODEL_PRIMARY", "cc/claude-haiku-4-5-20251001")
CLAUDE_MODEL_FALLBACK = os.environ.get("CLAUDE_MODEL_FALLBACK", "cc/claude-sonnet-5")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
ALLOWED_TOOLS = os.environ.get("ALLOWED_TOOLS", "Bash,WebSearch,WebFetch")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "600"))

# agy (Antigravity CLI) -- native, first-party Gemini access on a fixed-price
# Google AI Pro subscription. Tried FIRST -- both this and the Claude tiers
# below are fixed-price subscriptions (not pay-per-token API billing), so this
# ordering is about keeping the SEPARATE Claude Pro/Max quota in reserve, not
# about dodging per-token spend. Claude via 9Router is the fallback. Two
# Gemini tiers are tried in order before giving up on Gemini entirely: a
# Haiku-equivalent (fast/cheap)
# then a Sonnet-equivalent (more capable). The highest-effort Pro tier is
# deliberately NOT used as the fallback -- in testing it blew past agy's
# 5-minute print-timeout on even a trivial single-target query; the
# lower-effort Pro tier is the practical choice.
AGY_BIN = os.environ.get("AGY_BIN", str(Path.home() / ".local" / "bin" / "agy"))
AGY_WORKDIR = BASE_DIR  # GEMINI.md (agy's SOUL.md-equivalent) lives here
AGY_MODEL_PRIMARY = os.environ.get("AGY_MODEL_PRIMARY", "gemini-3.7-flash-medium")
AGY_MODEL_FALLBACK = os.environ.get("AGY_MODEL_FALLBACK", "gemini-3.1-pro-low")
# agy's own --print-timeout (default 5m); our subprocess timeout is set a bit
# above it so agy reports its own clean "timeout waiting for response" JSON
# instead of us hard-killing it mid-response with no usage data to log.
AGY_PRINT_TIMEOUT = os.environ.get("AGY_PRINT_TIMEOUT", "280s")
AGY_TIMEOUT = int(os.environ.get("AGY_TIMEOUT_SECONDS", "300"))
# Telegram Bot API's own upload limit for bot-sent documents.
TELEGRAM_MAX_FILE_BYTES = 50 * 1024 * 1024
# python-telegram-bot's default HTTP timeouts (~5s read/write) are tuned for
# small text replies, not multi-MB document uploads -- a legitimately slow
# upload (large file, or Telegram taking a moment to process/store it before
# replying) trips a ReadTimeout on OUR side even though the file already
# reached Telegram and the message posts anyway, producing a confusing
# "failed to send" error alongside a file that's actually sitting right there
# in the chat. Give document uploads a much longer budget specifically.
MEDIA_UPLOAD_TIMEOUT = 120
# Claude Code emits a line like "MEDIA:/tmp/report.pdf" in its final reply
# when it wants a generated file delivered as a real attachment (see SOUL.md's
# report-generation guidance) -- this pulls those lines out and sends the
# file for real.
MEDIA_LINE_RE = re.compile(r"^\s*MEDIA:\s*(.+?)\s*$", re.MULTILINE)
# agy/Gemini was ALSO told about the MEDIA: convention (in GEMINI.md) but
# never follows it in practice -- it consistently emits file:///abs/path
# links instead (its native IDE "clickable file" habit, which does nothing
# useful in Telegram). Rather than keep fighting an instruction it won't
# follow, we just also detect its actual pattern and deliver those files
# for real too.
FILE_URI_RE = re.compile(r"file://(/[^\s\)\]\"'>]+)")

# --------------------------------------------------------------------------
# Self-learning environment brief (two-zone model)
#
# The brief files (SOUL.md / GEMINI.md) are split into two zones by a marker:
#
#   ... PROTECTED: persona, HARD BOUNDARIES, safety rules -- humans only ...
#   <!-- LEARNED_ZONE -->
#   ... facts the agent discovered about this environment -- auto-appended ...
#
# The model is NEVER given write access to these files. It can already run
# arbitrary shell commands, so "please don't edit above the marker" would be a
# request, not a boundary -- and the PROTECTED zone is exactly where the rules
# saying which VMs must never be touched live. An agent able to quietly delete
# its own safety rules is not a risk worth taking for convenience.
#
# Instead the model emits a "LEARN: <fact>" line (same convention as MEDIA:)
# and THIS code decides where it lands -- always appended inside the LEARNED
# zone, never anywhere else. The zone boundary is then enforced structurally
# rather than by the model's cooperation.
LEARN_LINE_RE = re.compile(r"^\s*LEARN:\s*(.+?)\s*$", re.MULTILINE)
LEARNED_ZONE_MARKER = "<!-- LEARNED_ZONE -->"
# The LEARNED zone is re-sent at the start of every new conversation, so
# unbounded growth would quietly raise the floor cost of every future turn.
# Oldest entries are dropped past this cap.
LEARNED_MAX_FACTS = 60
# Which of the 4 tiers actually answered, in one glance -- if it's ever NOT
# "mini" (the primary/cheapest tier), that's a visible signal something
# upstream (rate limit, auth hiccup, timeout) forced an escalation.
BACKEND_LABELS = {
    AGY_MODEL_PRIMARY: "mini",
    AGY_MODEL_FALLBACK: "mini pro",
    CLAUDE_MODEL_PRIMARY: "dede iku",
    CLAUDE_MODEL_FALLBACK: "dede nnet",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger("lite-agent")


# --------------------------------------------------------------------------
# Session persistence: telegram chat_id -> { active: name, sessions: {name: {
#   "claude": {model: session_id}, "agy": {model: conversation_id}
# }}}. Each of the 4 tiers keeps its OWN resume handle because they don't
# share conversation history with each other -- if a turn falls through from
# one tier to the next, the next tier starts that turn without the previous
# tier's history, but each tier's OWN history stays continuous across turns.
# --------------------------------------------------------------------------

EMPTY_SESSION = {"claude": {}, "agy": {}}
# Both "agy" and "claude" are dicts keyed by model name -- each of the 4 tiers
# gets its OWN session/conversation id. Resuming a conversation started on one
# model using a DIFFERENT model produced a dramatically more expensive single
# turn in testing than the same fresh conversation -- cross-model resume is
# expensive/unreliable, so tiers are never allowed to share history.


def load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except Exception:
            logger.warning("sessions.json unreadable, starting fresh", exc_info=True)
    return {}


def save_sessions(sessions: dict) -> None:
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


def get_chat_state(sessions: dict, chat_id: str) -> dict:
    """Return this chat's {active, sessions} block, migrating older formats
    transparently: (1) flat {chat_id: session_id}, (2) {active, sessions: {name:
    session_id | None}}, (3) {..., "claude": session_id|None, "agy": {model: id}}
    -- all upgraded to the current shape where BOTH "claude" and "agy" are
    dicts keyed by model name (needed once Claude also got two explicit tiers)."""
    entry = sessions.get(chat_id)
    if entry is None:
        entry = {"active": DEFAULT_SESSION_NAME, "sessions": {DEFAULT_SESSION_NAME: dict(EMPTY_SESSION)}}
    elif isinstance(entry, str):
        # oldest format: chat_id mapped directly to a claude session_id string
        entry = {
            "active": DEFAULT_SESSION_NAME,
            "sessions": {DEFAULT_SESSION_NAME: {"claude": {CLAUDE_MODEL_PRIMARY: entry}, "agy": {}}},
        }
    else:
        entry.setdefault("active", DEFAULT_SESSION_NAME)
        entry.setdefault("sessions", {})
        for name, val in list(entry["sessions"].items()):
            if val is None or isinstance(val, str):
                # oldest format: bare claude session_id (or None) per name
                entry["sessions"][name] = {
                    "claude": {CLAUDE_MODEL_PRIMARY: val} if val else {},
                    "agy": {},
                }
                continue
            if not isinstance(val.get("agy"), dict):
                # intermediate format: single shared agy conversation_id (or
                # None) for all Gemini tiers -- found to cause expensive
                # cross-model resume. Drop it; a fresh conversation per tier
                # costs less than one bad cross-model resume anyway.
                val["agy"] = {}
            if not isinstance(val.get("claude"), dict):
                # intermediate format: single claude session_id (or None),
                # from back when Claude was routed through a 9Router combo
                # alias instead of an explicit model.
                val["claude"] = {CLAUDE_MODEL_PRIMARY: val["claude"]} if val.get("claude") else {}
        entry["sessions"].setdefault(entry["active"], dict(EMPTY_SESSION))
        for name in entry["sessions"]:
            entry["sessions"][name].setdefault("claude", {})
            entry["sessions"][name].setdefault("agy", {})
    sessions[chat_id] = entry
    return entry


# --------------------------------------------------------------------------
# Memory: MEMORY.md, edited ONLY via explicit /remember -- never automatically
# --------------------------------------------------------------------------

def load_memory_text() -> str:
    if not MEMORY_FILE.exists():
        return ""
    text = MEMORY_FILE.read_text().strip()
    return text


def _brief_files() -> list[Path]:
    """Both backends' briefs. A fact learned while Gemini was answering is just
    as true when Claude answers next, so learned facts go to both."""
    return [p for p in (SYSTEM_PROMPT_FILE, GEMINI_PROMPT_FILE) if p.exists()]


def _split_zones(text: str) -> tuple[str, str]:
    """-> (protected_part, learned_part). A brief with no marker yet is treated
    as entirely protected, and the marker is added on first write."""
    if LEARNED_ZONE_MARKER in text:
        head, _, tail = text.partition(LEARNED_ZONE_MARKER)
        return head.rstrip(), tail.strip()
    return text.rstrip(), ""


def append_learned(facts: list[str]) -> list[str]:
    """Append discovered environment facts to the LEARNED zone of every brief.

    Returns the facts actually written (duplicates and junk are dropped, so the
    caller can tell the user what really got saved rather than what was asked
    for). Nothing above the marker is ever read back out and rewritten -- the
    protected half is passed through byte-for-byte.
    """
    cleaned: list[str] = []
    for fact in facts:
        fact = " ".join(fact.split())
        # Guard against the model "learning" something useless or enormous.
        if 10 <= len(fact) <= 400:
            cleaned.append(fact)
    if not cleaned:
        return []

    written: list[str] = []
    ts = _dt.datetime.now().strftime("%Y-%m-%d")
    for path in _brief_files():
        protected, learned = _split_zones(path.read_text())
        existing_lines = [ln for ln in learned.split("\n") if ln.strip().startswith("- ")]
        # Case-insensitive dedup on the fact text, ignoring the date prefix, so
        # the same discovery re-reported next week doesn't accumulate.
        seen = {ln.split("] ", 1)[-1].strip().lower() for ln in existing_lines}
        for fact in cleaned:
            if fact.lower() in seen:
                continue
            seen.add(fact.lower())
            existing_lines.append(f"- [{ts}] {fact}")
            if fact not in written:
                written.append(fact)
        existing_lines = existing_lines[-LEARNED_MAX_FACTS:]
        body = "\n".join(existing_lines)
        path.write_text(
            f"{protected}\n\n{LEARNED_ZONE_MARKER}\n"
            "## Learned about this environment\n\n"
            "Facts the agent discovered and recorded itself. Safe to edit or delete by\n"
            "hand -- nothing above the marker line is ever touched automatically.\n\n"
            f"{body}\n"
        )
    if written:
        logger.info("learned %d new fact(s): %s", len(written), " | ".join(written))
    return written


def extract_learned(text: str) -> tuple[str, list[str]]:
    """Pull LEARN: lines out of a reply and strip them from the visible text."""
    facts = [f.strip() for f in LEARN_LINE_RE.findall(text)]
    return LEARN_LINE_RE.sub("", text).strip(), facts


def append_memory(fact: str) -> None:
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text(
            "# Memory\n\nCross-session facts, curated manually via /remember. "
            "Never written to automatically.\n\n"
        )
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- [{ts}] {fact}\n")


# --------------------------------------------------------------------------
# Claude Code invocation
# --------------------------------------------------------------------------

def _run_claude_once(prompt: str, session_id: Optional[str], session_name: str, model: str) -> dict:
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL
    env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
    env["ANTHROPIC_MODEL"] = model

    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--system-prompt-file", str(SYSTEM_PROMPT_FILE),
        "--output-format", "json",
        "--allowedTools", ALLOWED_TOOLS,
    ]

    memory_text = load_memory_text()
    if memory_text:
        cmd += ["--append-system-prompt", memory_text]

    if session_id:
        cmd += ["--resume", session_id]
    else:
        # Fresh session: tag it with our session_name so it's identifiable if
        # someone inspects `claude` sessions directly on the box.
        cmd += ["--name", session_name]

    logger.info(
        "running claude: session_name=%s session_id=%s prompt_len=%d memory_chars=%d",
        session_name, session_id, len(prompt), len(memory_text),
    )
    proc = subprocess.run(
        cmd, cwd=str(BASE_DIR), env=env,
        capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Claude Code exited {proc.returncode}: {proc.stderr[-800:]}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude Code returned non-JSON output: {proc.stdout[-800:]}")


def run_claude(prompt: str, session_id: Optional[str], session_name: str, model: str) -> dict:
    """Run one Claude Code turn on an explicit model. Falls back to a fresh
    session if --resume fails (e.g. the referenced session expired)."""
    try:
        return _run_claude_once(prompt, session_id, session_name, model)
    except RuntimeError as exc:
        if session_id:
            logger.warning("resume with session=%s failed (%s), retrying fresh", session_id, exc)
            return _run_claude_once(prompt, None, session_name, model)
        raise


# --------------------------------------------------------------------------
# agy (Antigravity CLI) invocation -- native Gemini access, fixed-price
# --------------------------------------------------------------------------

def _build_agy_prompt(prompt: str, include_env: bool = False) -> str:
    """agy has no --append-system-prompt equivalent, so context is folded into
    the prompt text itself.

    GEMINI.md (the environment brief) is SUPPOSED to be auto-loaded by agy from
    its working directory, but that behaviour is undocumented and could not be
    verified from the outside -- and there is direct evidence against relying on
    it: a rule added to GEMINI.md ("export images as JPEG, never PNG") was
    ignored by the very next fresh conversation, which went on to render an 89MB
    PNG that Telegram then refused to accept. Rather than keep guessing, the
    brief is now injected explicitly, exactly like MEMORY.md already was.

    It is only injected when STARTING a conversation (include_env=True), never
    on a resumed turn -- once it is in the conversation history the model still
    has it, so re-sending ~2.5k tokens every turn would be pure waste.
    """
    parts: list[str] = []
    if include_env and GEMINI_PROMPT_FILE.exists():
        env_text = GEMINI_PROMPT_FILE.read_text().strip()
        if env_text:
            parts.append(
                "[Working-environment instructions -- MUST be followed for this "
                f"entire conversation:]\n{env_text}"
            )
    memory_text = load_memory_text()
    if memory_text:
        parts.append(f"[Cross-session facts to remember:]\n{memory_text}")
    if not parts:
        return prompt
    parts.append(f"[User's question:]\n{prompt}")
    return "\n\n".join(parts)


def _run_agy_once(prompt: str, model: str, conversation_id: Optional[str]) -> dict:
    cmd = [
        AGY_BIN, "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--print-timeout", AGY_PRINT_TIMEOUT,
    ]
    if conversation_id:
        cmd += ["--conversation", conversation_id]

    logger.info(
        "running agy: model=%s conversation_id=%s prompt_len=%d",
        model, conversation_id, len(prompt),
    )
    proc = subprocess.run(
        cmd, cwd=str(AGY_WORKDIR), capture_output=True, text=True, timeout=AGY_TIMEOUT,
    )

    # Parse stdout regardless of returncode -- a denied/failed run can still
    # carry a usage block (tokens already spent before the failure), which we
    # want to surface for waste auditing rather than discard.
    parsed = None
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pass

    if proc.returncode != 0:
        raise RuntimeError(f"agy exited {proc.returncode}: {proc.stderr[-500:] or proc.stdout[-500:]}")
    if parsed is None:
        raise RuntimeError(f"agy returned non-JSON output: {proc.stdout[-500:]}")
    if parsed.get("status") != "SUCCESS" or not parsed.get("response"):
        u = parsed.get("usage", {}) or {}
        raise RuntimeError(
            f"agy status={parsed.get('status')} error={parsed.get('error') or '(empty response)'} "
            f"wasted_tokens={u.get('total_tokens', 0)}"
        )
    return parsed


def _normalize_agy_result(parsed: dict) -> dict:
    """Reshape agy's JSON envelope to the same {result, session_id, usage,
    total_cost_usd} shape run_claude() returns, so callers don't need to care
    which backend actually answered."""
    u = parsed.get("usage", {}) or {}
    return {
        "result": parsed.get("response"),
        "session_id": parsed.get("conversation_id"),
        "usage": {
            "input_tokens": u.get("input_tokens"),
            "output_tokens": u.get("output_tokens"),
            "cache_read_input_tokens": u.get("cache_read_tokens"),
            "cache_creation_input_tokens": None,  # agy does not report this separately
        },
        "total_cost_usd": None,  # agy does not report cost (fixed-price subscription)
    }


# --------------------------------------------------------------------------
# Combo orchestrator: 4 explicit tiers, cheapest first --
#   agy flash-medium ("mini") -> agy pro-low ("mini pro")
#   -> claude haiku ("dede iku") -> claude sonnet ("dede nnet")
# --------------------------------------------------------------------------

def run_combo(prompt: str, sess: dict, session_name: str) -> tuple[dict, str, list[str]]:
    """Try each tier in order, falling through only when the current one fails
    outright. `sess` is this named session's {"claude": {model: id}, "agy":
    {model: id}} dict -- mutated in place with whichever tier's resume handle
    actually got used; the caller persists it afterward.

    Returns (normalized_result, model_name, attempt_log). model_name is the
    literal model that answered (key into BACKEND_LABELS for the "mini" /
    "dede iku" style tag) -- NOT a combo alias, so it's always known exactly.
    attempt_log is a list of short strings, one per attempt, logged verbatim
    so a human can see exactly how much (if anything) was spent on failed
    attempts before the turn that actually produced the answer.
    """
    attempts: list[str] = []
    agy_convs = sess.setdefault("agy", {})  # {model_name: conversation_id}
    claude_sessions = sess.setdefault("claude", {})  # {model_name: session_id}

    for model in (AGY_MODEL_PRIMARY, AGY_MODEL_FALLBACK):
        conv_id = agy_convs.get(model)
        # Each tier keeps its OWN conversation, so "is this a fresh conversation"
        # is a per-model question -- the env brief has to go to whichever tier is
        # starting from nothing, which may be the fallback even while the primary
        # is already mid-conversation.
        agy_prompt = _build_agy_prompt(prompt, include_env=(conv_id is None))
        try:
            parsed = _run_agy_once(agy_prompt, model, conv_id)
            u = parsed.get("usage", {}) or {}
            attempts.append(f"agy:{model} OK ({u.get('total_tokens', '?')} tok)")
            agy_convs[model] = parsed.get("conversation_id")
            return _normalize_agy_result(parsed), model, attempts
        except Exception as exc:
            attempts.append(f"agy:{model} FAILED ({exc})")
            logger.warning("agy model=%s failed: %s", model, exc)
            agy_convs[model] = None  # don't try to resume a conversation that just errored

    logger.warning("both Gemini tiers failed for session=%s, falling back to claude", session_name)
    for model in (CLAUDE_MODEL_PRIMARY, CLAUDE_MODEL_FALLBACK):
        try:
            result = run_claude(prompt, claude_sessions.get(model), session_name, model)
            u = result.get("usage", {}) or {}
            total = sum(v for v in u.values() if isinstance(v, int))
            attempts.append(f"claude:{model} OK ({total} tok)")
            claude_sessions[model] = result.get("session_id")
            return result, model, attempts
        except Exception as exc:
            attempts.append(f"claude:{model} FAILED ({exc})")
            logger.warning("claude model=%s failed: %s", model, exc)
            claude_sessions[model] = None

    raise RuntimeError(f"All 4 tiers failed: {' -> '.join(attempts)}")


# --------------------------------------------------------------------------
# Telegram handlers
# --------------------------------------------------------------------------

def _authorized(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    # Whole-group authorization: any message from a chat_id in ALLOWED_GROUP_IDS
    # is treated as authorized regardless of WHICH member sent it -- this is a
    # deliberate access-control choice (vs. per-person whitelist), made because
    # this bot has real SSH/infra access, so it's scoped to specific registered
    # groups rather than defaulting open.
    if chat_id is not None and chat_id in ALLOWED_GROUP_IDS:
        return True
    if not ALLOWED_USER_IDS:
        logger.warning("No ALLOWED_USER_IDS configured -- bot is open to anyone!")
        return True
    return user_id in ALLOWED_USER_IDS


# --------------------------------------------------------------------------
# Rendering model output for Telegram
#
# Models write Markdown. Telegram does NOT render Markdown unless asked, and
# even then its Markdown dialects are strict enough that one stray character in
# a long generated report rejects the WHOLE message. So replies used to be sent
# with no parse_mode at all -- which is why real reports arrived showing literal
# "### Heading", "**bold**" and backticks instead of formatted text.
#
# HTML mode is the forgiving option: escape the text first, then emit only the
# small tag set Telegram actually supports (b, i, u, s, code, pre, a). Anything
# Telegram has no concept of -- headings, tables, bullet syntax, horizontal
# rules -- is converted into something that still reads correctly rather than
# being passed through raw.
# --------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```[ \t]*([\w+-]*)[ \t]*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+|file://[^)\s]+)\)")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]*(.+?)[ \t]*#*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+(?=\S)", re.MULTILINE)
_HRULE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$")


def _tg_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _wrap_tables(text: str) -> str:
    """Telegram cannot render tables at all. Rather than let a report's table
    arrive as a pile of stray pipes, wrap contiguous table blocks in <pre> so
    the columns at least stay aligned in the monospace font."""
    lines = text.split("\n")
    out: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        # Only treat it as a table if a separator row was present; a lone line
        # containing a pipe is far more likely to be prose or a shell command.
        if any(_TABLE_SEP_RE.match(ln) for ln in block):
            out.append("<pre>" + "\n".join(block) + "</pre>")
        else:
            out.extend(block)
        block.clear()

    for line in lines:
        if line.count("|") >= 2:
            block.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def _md_to_telegram_html(text: str) -> str:
    """Convert a model's Markdown into the subset of HTML Telegram accepts."""
    placeholders: list[str] = []

    def _stash(html: str) -> str:
        placeholders.append(html)
        return f"\x00{len(placeholders) - 1}\x00"

    # Code first: its contents must survive untouched by every rule below.
    def _code_block(m: re.Match) -> str:
        lang, body = m.group(1), m.group(2)
        body = _tg_escape(body.rstrip("\n"))
        if lang:
            return _stash(f'<pre><code class="language-{lang}">{body}</code></pre>')
        return _stash(f"<pre>{body}</pre>")

    text = _CODE_BLOCK_RE.sub(_code_block, text)
    text = _INLINE_CODE_RE.sub(lambda m: _stash(f"<code>{_tg_escape(m.group(1))}</code>"), text)
    text = _LINK_RE.sub(
        lambda m: _stash(f'<a href="{_tg_escape(m.group(2))}">{_tg_escape(m.group(1))}</a>'),
        text,
    )

    text = _tg_escape(text)
    text = _wrap_tables(text)
    text = _HRULE_RE.sub("─" * 20, text)
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)

    for i, html in enumerate(placeholders):
        text = text.replace(f"\x00{i}\x00", html)
    return text


def _chunk_lines(text: str, limit: int) -> list[str]:
    """Split on line boundaries, never mid-line. Conversion happens per chunk,
    so tags can never end up split across two messages (which would make
    Telegram reject the half that has an unclosed tag)."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # pathological single long line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


async def _reply_chunked(update: Update, text: str, tag_html: str = "") -> None:
    """Send a (possibly long) model reply, formatted. `tag_html` is appended to
    the LAST chunk only and is passed through as trusted HTML -- it's our own
    backend label, not model output."""
    chunks = _chunk_lines(text, 3500)
    for idx, chunk in enumerate(chunks):
        body = _md_to_telegram_html(chunk)
        if idx == len(chunks) - 1 and tag_html:
            body = f"{body}\n\n{tag_html}"
        try:
            await update.message.reply_text(body, parse_mode="HTML")
        except BadRequest as exc:
            # A malformed-entity rejection must never cost the user the whole
            # answer -- fall back to sending it unformatted rather than losing it.
            logger.warning("HTML formatting rejected by Telegram (%s); sending as plain text", exc)
            plain = chunk
            if idx == len(chunks) - 1 and tag_html:
                plain = f"{plain}\n\n{re.sub(r'<[^>]+>', '', tag_html)}"
            await update.message.reply_text(plain)



def extract_media_paths(text: str) -> tuple[str, list[str]]:
    """Pull deliverable file paths out of a reply, from two conventions:
    (1) an explicit "MEDIA:<path>" marker line (Claude Code follows this when
    instructed -- those lines are stripped from the visible text), and
    (2) file:///abs/path links (agy/Gemini's actual habit regardless of being
    told about MEDIA: -- left visible in the text since they're still useful
    as text, but their targets are queued for real delivery too). Duplicate
    targets (e.g. the same file linked twice) are only sent once. Pure string
    logic, no I/O -- files are only touched later, at send time."""
    marker_paths = [p.strip() for p in MEDIA_LINE_RE.findall(text)]
    clean_text = MEDIA_LINE_RE.sub("", text).strip()
    uri_paths = FILE_URI_RE.findall(clean_text)
    # NOTE: an earlier version also dropped every path under agy's own data
    # dir (".gemini/antigravity-cli/"), meant to skip its internal "Artifact"
    # mirror of a file saved elsewhere. That broke the case where agy saves
    # ONLY to its own scratch/ dir (no separate copy exists) -- the filter
    # discarded the one and only copy, so nothing got sent at all. Content-
    # hash dedup (below) already collapses true duplicates regardless of
    # which paths they live at, so this blanket path exclusion isn't needed
    # and was actively harmful; removed.
    # A file:// link isn't always "please deliver this" -- the model sometimes
    # cites the SCRIPT it used as its data source (e.g. "data came from
    # tools/cluster_snapshot.py"), which our regex can't tell apart from an
    # actual deliverable by pattern alone. Our own tool scripts (tools/*.py)
    # are never a report meant for the user, so exclude them outright.
    uri_paths = [
        p for p in uri_paths
        if not p.endswith(".py") and "/tools/" not in p
    ]

    seen: set[str] = set()
    paths: list[str] = []
    for p in marker_paths + uri_paths:
        key = os.path.realpath(p) if os.path.isabs(p) else p
        if key not in seen:
            seen.add(key)
            paths.append(p)
    return clean_text, paths


def _file_sha256(p: Path) -> Optional[str]:
    """Content hash for dedup. agy repeatedly links the SAME report at multiple
    different paths (an internal "Artifact" mirror, a stray copy in the cwd,
    etc.) -- path-pattern filtering keeps missing new variants of this, so
    dedup is done on actual file content instead, which can't be dodged by a
    new path shape. Returns None if the file can't be read (caller decides
    what to do; _send_media_file will surface the real error either way)."""
    try:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


async def _send_media_file(update: Update, path: str, sent_hashes: set[str]) -> None:
    p = Path(path)
    if not p.is_absolute():
        p = BASE_DIR / p
    if not p.exists() or not p.is_file():
        logger.warning("MEDIA path not found: %s", path)
        await update.message.reply_text(f"⚠️ File not found, couldn't send it: {path}")
        return
    size = p.stat().st_size
    if size > TELEGRAM_MAX_FILE_BYTES:
        await update.message.reply_text(
            f"⚠️ File {p.name} ({size / 1024 / 1024:.1f}MB) exceeds Telegram's bot "
            f"upload limit (50MB), can't send it."
        )
        return
    digest = _file_sha256(p)
    if digest is not None and digest in sent_hashes:
        logger.info("skipped duplicate media file (same content already sent): %s", p)
        return
    try:
        with p.open("rb") as f:
            await update.message.reply_document(
                document=f,
                filename=p.name,
                read_timeout=MEDIA_UPLOAD_TIMEOUT,
                write_timeout=MEDIA_UPLOAD_TIMEOUT,
                connect_timeout=30,
            )
        logger.info("sent media file: %s (%d bytes)", p, size)
        if digest is not None:
            sent_hashes.add(digest)
    except Exception:
        logger.exception("failed to send media file %s", path)
        await update.message.reply_text(f"⚠️ Failed to send {p.name}: upload error.")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    chat_id = str(update.effective_chat.id)
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    active = state["active"]
    state["sessions"][active] = dict(EMPTY_SESSION)
    save_sessions(sessions)
    await update.message.reply_text(f"Session '{active}' restarted (fresh). \U0001f9f9")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    name = " ".join(context.args).strip() if context.args else ""
    if not name:
        await update.message.reply_text(
            "Usage: /session <name>\nExample: /session incident-123\n"
            "Use /sessions to see existing sessions."
        )
        return
    chat_id = str(update.effective_chat.id)
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    is_new = name not in state["sessions"]
    if is_new:
        state["sessions"][name] = dict(EMPTY_SESSION)
    state["active"] = name
    save_sessions(sessions)
    if is_new:
        await update.message.reply_text(f"New session '{name}' created & active. \U0001f4c2")
    else:
        await update.message.reply_text(f"Switched to session '{name}' (continuing from before). \U0001f4c2")


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    chat_id = str(update.effective_chat.id)
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    lines = ["\U0001f4c2 Saved sessions:"]
    for name in state["sessions"]:
        marker = " (active)" if name == state["active"] else ""
        lines.append(f"• {name}{marker}")
    lines.append("\nSwitch session: /session <name>")
    await update.message.reply_text("\n".join(lines))


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    fact = " ".join(context.args).strip() if context.args else ""
    if not fact:
        await update.message.reply_text("Usage: /remember <fact to remember permanently>")
        return
    append_memory(fact)
    await update.message.reply_text(f"\U0001f4dd Remembered: {fact}")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = load_memory_text()
    if not text:
        await update.message.reply_text("MEMORY.md is empty. Add facts with /remember <fact>.")
        return
    await _reply_chunked(update, text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Status check with ZERO model tokens: runs the snapshot collector
    directly and prints its already-digested output. This is the 'graduated
    skill' path -- a question we've already solved is answered by a script,
    not by re-deriving it with an LLM every time."""
    if not _authorized(update):
        return
    if not SNAPSHOT_SCRIPT.exists():
        await update.message.reply_text(f"⚠️ Collector not installed: {SNAPSHOT_SCRIPT}")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    force = bool(context.args and context.args[0].lower() in ("force", "fresh", "-f"))
    cmd = ["python3", str(SNAPSHOT_SCRIPT)] + (["--force"] if force else [])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("⚠️ Collector timeout (>120s).")
        return
    if proc.returncode != 0:
        await update.message.reply_text(f"⚠️ Collector failed: {proc.stderr[-500:]}")
        return
    logger.info("status command served (0 model tokens, force=%s)", force)
    await _reply_chunked(update, f"```\n{proc.stdout.strip()}\n```")


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List graduated skills. Zero model tokens -- just reads the registry."""
    if not _authorized(update):
        return
    if not LIST_TOOLS_SCRIPT.exists():
        await update.message.reply_text(f"⚠️ Not installed: {LIST_TOOLS_SCRIPT}")
        return
    proc = subprocess.run(
        ["python3", str(LIST_TOOLS_SCRIPT)], capture_output=True, text=True, timeout=30
    )
    out = proc.stdout.strip() or proc.stderr.strip() or "(empty)"
    await _reply_chunked(update, f"```\n{out}\n```")


async def cmd_graduate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn the case just solved in this session into a reusable script.

    Explicitly user-triggered, one bounded call, fixed instruction --
    deliberately NOT a background job that decides on its own what is
    worth saving."""
    if not _authorized(update):
        return
    name = "-".join(context.args).strip().lower() if context.args else ""
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        await update.message.reply_text(
            "Usage: /graduate <script-name>\n"
            "Example: /graduate backup-coverage\n"
            "(lowercase letters, digits, - and _ only)"
        )
        return

    chat_id = str(update.effective_chat.id)
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    active = state["active"]
    claude_session_id = state["sessions"].get(active, {}).get("claude", {}).get(CLAUDE_MODEL_PRIMARY)
    if not claude_session_id:
        await update.message.reply_text(
            "No Claude ('dede iku') conversation in this session to graduate yet "
            "(if the last turn was answered by Gemini/'mini', /graduate can't see "
            "that history -- current limitation, each tier keeps its own history). "
            "Ask again until Claude answers, or finish the case first, then /graduate."
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        result = run_claude(GRADUATE_INSTRUCTION.format(name=name), claude_session_id, active, CLAUDE_MODEL_PRIMARY)
    except Exception as exc:
        logger.exception("graduate failed")
        await update.message.reply_text(f"⚠️ Error: {exc}")
        return

    new_session_id = result.get("session_id")
    if new_session_id:
        sessions = load_sessions()
        state = get_chat_state(sessions, chat_id)
        state["sessions"][active]["claude"][CLAUDE_MODEL_PRIMARY] = new_session_id
        save_sessions(sessions)

    usage = result.get("usage", {})
    logger.info(
        "graduate done: name=%s in=%s out=%s cache_read=%s",
        name, usage.get("input_tokens"), usage.get("output_tokens"),
        usage.get("cache_read_input_tokens"),
    )
    await _reply_chunked(update, result.get("result") or "(no response)")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliberately NOT gated by _authorized() -- its only job is to reveal IDs
    needed for setup (registering a group in ALLOWED_GROUP_IDS, or a person in
    ALLOWED_USER_IDS). It reveals no infra data and takes no action, so it's
    safe to leave open even to people not yet authorized for anything else."""
    chat = update.effective_chat
    user = update.effective_user
    kind = {"private": "Private DM", "group": "Group", "supergroup": "Supergroup", "channel": "Channel"}.get(
        chat.type, chat.type
    )
    lines = [
        f"\U0001f4cd Chat ID ({kind}): `{chat.id}`",
    ]
    if user:
        lines.append(f"\U0001f464 Your User ID: `{user.id}`")
    if chat.type != "private":
        lines.append(
            "\nTo open this bot to EVERY member of this group, ask an admin to run "
            "`/registergroup` here (or send the Chat ID above to an admin)."
        )
    else:
        lines.append(
            "\nTo request personal access (not via a group), send the User ID above "
            "to an admin to be added to `ALLOWED_USER_IDS`."
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def _is_admin(update: Update) -> bool:
    """Stricter than _authorized(): TRUE only for someone in the named
    ALLOWED_USER_IDS list, never via group-wide access. Granting a NEW group
    access is deliberately not something being authorized-via-an-existing-
    group is enough to do -- otherwise trust could cascade to groups the
    original admin never intended (member of group A adds group B, etc.)."""
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def cmd_registergroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only, self-service group authorization -- no SSH/restart needed.
    Mutates ALLOWED_GROUP_IDS in place (takes effect immediately for this
    running process) and persists to allowed_groups.json (survives restart)."""
    if not _is_admin(update):
        # Deliberately don't reveal *why* -- same non-response whether the
        # command doesn't exist or the user isn't allowed to use it.
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text(
            "This command is for groups, not a private DM -- run it inside the group you want to open access to."
        )
        return
    if chat.id in ALLOWED_GROUP_IDS:
        await update.message.reply_text(f"This group (`{chat.id}`) is already registered.", parse_mode="Markdown")
        return
    ALLOWED_GROUP_IDS.add(chat.id)
    _save_allowed_groups_file(ALLOWED_GROUP_IDS)
    logger.info("group registered by admin: chat_id=%s title=%s", chat.id, chat.title)
    await update.message.reply_text(
        f"✅ Group *{chat.title or chat.id}* registered. Every member can now use this bot.",
        parse_mode="Markdown",
    )


async def cmd_unregistergroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only. Revokes a group's whole-group access; per-person entries in
    ALLOWED_USER_IDS (if any of that group's members were also individually
    whitelisted) are untouched."""
    if not _is_admin(update):
        return
    chat = update.effective_chat
    if chat.id not in ALLOWED_GROUP_IDS:
        await update.message.reply_text("This group isn't registered.")
        return
    ALLOWED_GROUP_IDS.discard(chat.id)
    _save_allowed_groups_file(ALLOWED_GROUP_IDS)
    logger.info("group unregistered by admin: chat_id=%s title=%s", chat.id, chat.title)
    await update.message.reply_text(f"Access revoked for group *{chat.title or chat.id}*.", parse_mode="Markdown")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return
    await update.message.reply_text(
        "\U0001f44b Lite Agent is ready. Send anything to get started (status checks, "
        "investigations, reports, etc).\n\n"
        "Type /help for the full guide + command list.\n\n"
        "\U0001f680 Designed by Koko Ali & Dede · Developed by Infrasoft.cloud & BSCloud.id Team\n"
        "Happy smart working! ✨"
    )


_HELP_CREDITS = (
    "━━━━━━━━━━━━━━━━━━━\n"
    "\U0001f680 *Designed by Koko Ali & Dede*\n"
    "\U0001f4bb *Developed by Infrasoft.cloud & BSCloud.id Team*\n\n"
    "Happy smart working! ✨\U0001f929\U0001f60e"
)

HELP_TEXT_EN = f"""\U0001f4d6 *iSmart-LA — Usage Guide*

*How it works*
Every message is tried through 4 tiers, cheapest first:
1. {BACKEND_LABELS[AGY_MODEL_PRIMARY]} — Gemini Flash (fixed-price, primary)
2. {BACKEND_LABELS[AGY_MODEL_FALLBACK]} — Gemini Pro-low (fallback #1)
3. {BACKEND_LABELS[CLAUDE_MODEL_PRIMARY]} — Claude Haiku (fallback #2)
4. {BACKEND_LABELS[CLAUDE_MODEL_FALLBACK]} — Claude Sonnet (last resort)

Every reply ends with a "— by ..." tag. If it's ever NOT "{BACKEND_LABELS[AGY_MODEL_PRIMARY]}", that's a signal something upstream is having trouble (rate limit, auth, etc) -- useful for keeping an eye on system health at a glance.

*Available commands*
/status — instant status check, ZERO tokens (straight from a script, not a model)
/tools — list of skills already turned into scripts, ZERO tokens
/graduate <name> — turn the case you JUST solved into a reusable script (free to reuse afterward)
/new — restart the ACTIVE session from scratch (conversation history reset, MEMORY.md untouched)
/session <name> — create/switch to a named session, for keeping different cases separate
/sessions — list all saved sessions
/remember <fact> — save a fact PERMANENTLY, read in EVERY session & EVERY tier, even after many /new
/memory — view current memory contents
/learned — see what the agent figured out about this environment BY ITSELF
/forget <number> — delete one wrong learned fact (numbers come from /learned)
/help — this guide (choose EN or ID)

*3 habits that keep it cheap*
1. *`/new` every time the topic changes.* A continuing conversation history is EXPENSIVE -- the longer it gets, the more expensive every next turn (can be 10-20x if left to pile up). Infra case closed → want to ask something unrelated? `/new` first.
2. *Don't `/new` BETWEEN "generate a report" and "send it to me".* A fresh session has no memory of which report was just made -- ask "send the file" in a new session and it'll go looking through every old report that exists and offer all of them.
3. *Important facts → `/remember`, not chat history.* Cases that are closed/decided go into `/remember` so they don't get re-asked or re-investigated -- this is the ONLY thing that survives across `/new`.

*Using it in a Telegram Group*
If this group has been registered by an admin (check with `/chatid`), EVERY member can give the bot commands automatically -- no per-person whitelist needed.
1. If the bot only responds to *commands* (`/status` etc), not plain messages -- mention it (`@botname ...`) or reply to one of its messages to make sure it's seen (that's Telegram's own default setting, not a limitation on our end).
2. Sessions (`/new`, `/session`) in this group are *separate* from each member's private DM -- safely isolated. But `/remember` is *GLOBAL* across every chat including this group -- if someone remembers a fact here, everyone here (and in every other chat with this bot) can see it via `/memory`.
3. This group doesn't have access yet? An admin just needs to type `/registergroup` here -- takes effect immediately, no restart needed. (`/unregistergroup` to revoke it again.)

{_HELP_CREDITS}"""

HELP_TEXT_ID = f"""\U0001f4d6 *iSmart-LA — Panduan Pemakaian*

*Cara kerjanya*
Setiap pesan dicoba lewat 4 tingkatan, dari yang paling murah dulu:
1. {BACKEND_LABELS[AGY_MODEL_PRIMARY]} — Gemini Flash (harga tetap, utama)
2. {BACKEND_LABELS[AGY_MODEL_FALLBACK]} — Gemini Pro-low (cadangan #1)
3. {BACKEND_LABELS[CLAUDE_MODEL_PRIMARY]} — Claude Haiku (cadangan #2)
4. {BACKEND_LABELS[CLAUDE_MODEL_FALLBACK]} — Claude Sonnet (opsi terakhir)

Setiap balasan diakhiri tanda "— by ...". Kalau tandanya BUKAN "{BACKEND_LABELS[AGY_MODEL_PRIMARY]}", itu sinyal ada gangguan di salah satu layanan (rate limit, auth, dll) -- gampang dipantau sekilas tanpa perlu buka log.

*Daftar perintah*
/status — cek status instan, NOL token (langsung dari script, bukan model)
/tools — daftar skill yang sudah dijadikan script, NOL token
/graduate <nama> — ubah case yang BARU SAJA diselesaikan jadi script yang bisa dipakai ulang (gratis dipakai lagi)
/new — mulai ulang sesi AKTIF dari nol (riwayat percakapan direset, MEMORY.md tidak berubah)
/session <nama> — buat/pindah ke sesi bernama, untuk memisahkan case yang berbeda
/sessions — lihat semua sesi yang tersimpan
/remember <fakta> — simpan fakta PERMANEN, dibaca di SETIAP sesi & SETIAP tingkatan, meski sudah berkali-kali /new
/memory — lihat isi memori saat ini
/help — panduan ini (pilih EN atau ID)

*3 kebiasaan supaya tetap irit*
1. *`/new` setiap topik berganti.* Riwayat percakapan yang terus nyambung itu MAHAL -- makin panjang, makin mahal setiap giliran berikutnya (bisa 10-20x kalau dibiarkan menumpuk). Case infra sudah selesai → mau tanya hal lain yang tidak nyambung? `/new` dulu.
2. *Jangan `/new` DI ANTARA "buatkan laporan" dan "kirim ke saya".* Sesi baru tidak ingat laporan mana yang baru saja dibuat -- minta "kirim filenya" di sesi baru malah bikin dia mencari semua laporan lama yang pernah ada dan menawarkan semuanya.
3. *Fakta penting → `/remember`, bukan riwayat chat.* Case yang sudah selesai/diputuskan masukkan ke `/remember` supaya tidak ditanyakan/diselidiki ulang -- ini SATU-SATUNYA yang bertahan lintas `/new`.

*Memakainya di Grup Telegram*
Kalau grup ini sudah didaftarkan admin (cek dengan `/chatid`), SEMUA anggota otomatis bisa kasih perintah ke bot -- tidak perlu whitelist per orang.
1. Kalau bot cuma merespon *perintah* (`/status` dll), bukan pesan biasa -- mention botnya (`@namabot ...`) atau reply salah satu pesannya supaya pasti terlihat (ini pengaturan default Telegram sendiri, bukan keterbatasan dari sisi kita).
2. Sesi (`/new`, `/session`) di grup ini *terpisah* dari DM pribadi masing-masing anggota -- aman terisolasi. Tapi `/remember` bersifat *GLOBAL* di semua chat termasuk grup ini -- kalau seseorang me-remember fakta di sini, semua orang di sini (dan di semua chat lain dengan bot ini) bisa melihatnya lewat `/memory`.
3. Grup ini belum punya akses? Admin tinggal ketik `/registergroup` di sini -- langsung aktif, tanpa perlu restart. (`/unregistergroup` untuk mencabutnya lagi.)

{_HELP_CREDITS}"""

_HELP_LANG_KEYBOARD = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("\U0001f1ee\U0001f1e9 Indonesia", callback_data="help_id"),
        InlineKeyboardButton("\U0001f1ec\U0001f1e7 English", callback_data="help_en"),
    ]]
)

_HELP_LANG_PROMPT = "Pilih bahasa / Choose a language:"


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg in ("id", "indonesia", "indonesian"):
        await update.message.reply_text(HELP_TEXT_ID, parse_mode="Markdown")
        return
    if arg in ("en", "english"):
        await update.message.reply_text(HELP_TEXT_EN, parse_mode="Markdown")
        return
    await update.message.reply_text(_HELP_LANG_PROMPT, reply_markup=_HELP_LANG_KEYBOARD)


async def cmd_help_lang_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for the /help language-picker buttons."""
    query = update.callback_query
    if not _authorized(update):
        await query.answer()
        return
    text = HELP_TEXT_ID if query.data == "help_id" else HELP_TEXT_EN
    await query.answer()
    await query.edit_message_text(text, parse_mode="Markdown")


def _learned_facts() -> list[str]:
    """Learned entries as displayed, read from the first brief that has any."""
    for path in _brief_files():
        _, learned = _split_zones(path.read_text())
        lines = [ln.strip() for ln in learned.split("\n") if ln.strip().startswith("- ")]
        if lines:
            return lines
    return []


async def cmd_learned(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everything the agent worked out about this environment by itself."""
    if not _authorized(update):
        return
    facts = _learned_facts()
    if not facts:
        await update.message.reply_text(
            "The agent hasn't recorded anything about this environment yet.\n\n"
            "Environment knowledge fills itself in as you use it. Safety rules "
            "(hard boundaries) live in the protected zone and never change with it."
        )
        return
    numbered = "\n".join(f"{i}. {f[2:]}" for i, f in enumerate(facts, 1))
    await _reply_chunked(
        update,
        f"🧠 What the agent has worked out about this environment ({len(facts)}):\n\n{numbered}"
        "\n\nSomething wrong in there? Remove it with /forget <number>.",
    )


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove one learned fact. The counterpart to automatic learning: anything
    written without being asked must be just as easy to take back."""
    if not _authorized(update):
        return
    arg = (context.args[0] if context.args else "").strip()
    facts = _learned_facts()
    if not arg.isdigit() or not (1 <= int(arg) <= len(facts)):
        await update.message.reply_text(
            f"Usage: /forget <number>\nNumbers come from /learned "
            f"({len(facts)} recorded right now)."
        )
        return
    idx = int(arg) - 1
    target = facts[idx]
    target_text = target.split("] ", 1)[-1].strip().lower()
    removed = False
    for path in _brief_files():
        protected, learned = _split_zones(path.read_text())
        kept = [
            ln for ln in learned.split("\n")
            if not (ln.strip().startswith("- ")
                    and ln.split("] ", 1)[-1].strip().lower() == target_text)
        ]
        kept_facts = [ln for ln in kept if ln.strip().startswith("- ")]
        path.write_text(
            f"{protected}\n\n{LEARNED_ZONE_MARKER}\n"
            "## Learned about this environment\n\n"
            "Facts the agent discovered and recorded itself. Safe to edit or delete by\n"
            "hand -- nothing above the marker line is ever touched automatically.\n\n"
            + "\n".join(kept_facts) + "\n"
        )
        removed = True
    logger.info("forgot learned fact: %s", target)
    await update.message.reply_text(
        f"🗑 Forgotten:\n{target[2:]}" if removed else "Nothing was removed."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cluster status with ZERO model tokens: runs the snapshot collector directly
    and prints its already-digested output. This is the 'graduated skill' path --
    a question we've already solved is answered by a script, not by re-deriving it
    with an LLM every time."""
    if not _authorized(update):
        return
    if not SNAPSHOT_SCRIPT.exists():
        await update.message.reply_text(f"⚠️ Collector belum terpasang: {SNAPSHOT_SCRIPT}")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    force = bool(context.args and context.args[0].lower() in ("force", "fresh", "-f"))
    cmd = ["python3", str(SNAPSHOT_SCRIPT)] + (["--force"] if force else [])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("⚠️ Collector timeout (>120s).")
        return
    if proc.returncode != 0:
        await update.message.reply_text(f"⚠️ Collector gagal: {proc.stderr[-500:]}")
        return
    logger.info("status command served (0 model tokens, force=%s)", force)
    await _reply_chunked(update, f"```\n{proc.stdout.strip()}\n```")


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List graduated skills. Zero model tokens -- just reads the registry."""
    if not _authorized(update):
        return
    if not LIST_TOOLS_SCRIPT.exists():
        await update.message.reply_text(f"⚠️ Belum terpasang: {LIST_TOOLS_SCRIPT}")
        return
    proc = subprocess.run(
        ["python3", str(LIST_TOOLS_SCRIPT)], capture_output=True, text=True, timeout=30
    )
    out = proc.stdout.strip() or proc.stderr.strip() or "(kosong)"
    await _reply_chunked(update, f"```\n{out}\n```")


async def cmd_graduate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn the case just solved in this session into a reusable script.

    Explicitly user-triggered, one bounded call, fixed instruction -- deliberately
    NOT a background job that decides on its own what is worth saving."""
    if not _authorized(update):
        return
    name = "-".join(context.args).strip().lower() if context.args else ""
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        await update.message.reply_text(
            "Usage: /graduate <script-name>\n"
            "Example: /graduate backup-coverage\n"
            "(lowercase letters, digits, - and _ only)"
        )
        return

    chat_id = str(update.effective_chat.id)
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    active = state["active"]
    claude_session_id = state["sessions"].get(active, {}).get("claude", {}).get(CLAUDE_MODEL_PRIMARY)
    if not claude_session_id:
        await update.message.reply_text(
            "Belum ada percakapan Claude (dede iku) di sesi ini buat di-graduate (kalau turn "
            "terakhir dijawab Gemini/mini, /graduate belum bisa lihat history-nya -- "
            "keterbatasan saat ini, tiap backend punya history sendiri). Coba tanya ulang "
            "sampai dijawab Claude dulu, atau selesaikan kasusnya, baru /graduate."
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        result = run_claude(GRADUATE_INSTRUCTION.format(name=name), claude_session_id, active, CLAUDE_MODEL_PRIMARY)
    except Exception as exc:
        logger.exception("graduate failed")
        await update.message.reply_text(f"⚠️ Error: {exc}")
        return

    new_session_id = result.get("session_id")
    if new_session_id:
        sessions = load_sessions()
        state = get_chat_state(sessions, chat_id)
        state["sessions"][active]["claude"][CLAUDE_MODEL_PRIMARY] = new_session_id
        save_sessions(sessions)

    usage = result.get("usage", {})
    logger.info(
        "graduate done: name=%s in=%s out=%s cache_read=%s",
        name, usage.get("input_tokens"), usage.get("output_tokens"),
        usage.get("cache_read_input_tokens"),
    )
    await _reply_chunked(update, result.get("result") or "(tidak ada respons)")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliberately NOT gated by _authorized() -- its only job is to reveal IDs
    needed for setup (registering a group in ALLOWED_GROUP_IDS, or a person in
    ALLOWED_USER_IDS). It reveals no infra data and takes no action, so it's
    safe to leave open even to people not yet authorized for anything else."""
    chat = update.effective_chat
    user = update.effective_user
    kind = {"private": "DM pribadi", "group": "Grup", "supergroup": "Supergrup", "channel": "Channel"}.get(
        chat.type, chat.type
    )
    lines = [
        f"\U0001f4cd Chat ID ({kind}): `{chat.id}`",
    ]
    if user:
        lines.append(f"\U0001f464 User ID kamu: `{user.id}`")
    if chat.type != "private":
        lines.append(
            "\nBuat buka akses bot ini ke SEMUA anggota grup ini, minta admin ketik "
            "`/registergroup` di grup ini (atau kirim Chat ID di atas ke admin)."
        )
    else:
        lines.append(
            "\nBuat minta akses personal (bukan lewat grup), kirim User ID di atas "
            "ke admin buat ditambahkan ke `ALLOWED_USER_IDS`."
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def _is_admin(update: Update) -> bool:
    """Stricter than _authorized(): TRUE only for someone in the named
    ALLOWED_USER_IDS list, never via group-wide access. Granting a NEW group
    access is deliberately not something being authorized-via-an-existing-
    group is enough to do -- otherwise trust could cascade to groups the
    original admin never intended (member of group A adds group B, etc.)."""
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def cmd_registergroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only, self-service group authorization -- no SSH/restart needed.
    Mutates ALLOWED_GROUP_IDS in place (takes effect immediately for this
    running process) and persists to allowed_groups.json (survives restart)."""
    if not _is_admin(update):
        # Deliberately don't reveal *why* -- same denial shape whether the
        # command doesn't exist or the user isn't allowed to use it.
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text(
            "Command ini buat grup, bukan DM pribadi -- jalankan di dalam grup yang mau dibuka aksesnya."
        )
        return
    if chat.id in ALLOWED_GROUP_IDS:
        await update.message.reply_text(f"Grup ini (`{chat.id}`) sudah terdaftar sebelumnya.", parse_mode="Markdown")
        return
    ALLOWED_GROUP_IDS.add(chat.id)
    _save_allowed_groups_file(ALLOWED_GROUP_IDS)
    logger.info("group registered by admin: chat_id=%s title=%s", chat.id, chat.title)
    await update.message.reply_text(
        f"✅ Grup *{chat.title or chat.id}* terdaftar. Semua anggota sekarang bisa pakai bot ini.",
        parse_mode="Markdown",
    )


async def cmd_unregistergroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only. Revokes a group's whole-group access; per-person entries in
    ALLOWED_USER_IDS (if any of that group's members were also individually
    whitelisted) are untouched."""
    if not _is_admin(update):
        return
    chat = update.effective_chat
    if chat.id not in ALLOWED_GROUP_IDS:
        await update.message.reply_text("Grup ini belum/tidak terdaftar.")
        return
    ALLOWED_GROUP_IDS.discard(chat.id)
    _save_allowed_groups_file(ALLOWED_GROUP_IDS)
    logger.info("group unregistered by admin: chat_id=%s title=%s", chat.id, chat.title)
    await update.message.reply_text(f"Akses grup *{chat.title or chat.id}* dicabut.", parse_mode="Markdown")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.message.reply_text("Maaf, kamu belum diotorisasi buat pakai bot ini.")
        return
    await update.message.reply_text(
        "\U0001f44b Lite Agent siap. Kirim pesan apa aja buat mulai (cek status VM, "
        "investigasi, bikin laporan, dll).\n\n"
        "Ketik /help buat panduan lengkap + daftar semua command.\n\n"
        "\U0001f680 Designed by Koko Ali & Dede · Developed by Infrasoft.cloud & BSCloud.id Team\n"
        "Happy smart working! ✨"
    )


_HELP_CREDITS = (
    "━━━━━━━━━━━━━━━━━━━\n"
    "\U0001f680 *Designed by Koko Ali & Dede*\n"
    "\U0001f4bb *Developed by Infrasoft.cloud & BSCloud.id Team*\n\n"
    "Happy smart working! ✨\U0001f929\U0001f60e"
)

HELP_TEXT_ID = f"""\U0001f4d6 *Lite Agent — Panduan Pakai*

*Cara kerja singkat*
Tiap pesan dicoba lewat 4 tingkatan model, dari yang paling murah dulu:
1. {BACKEND_LABELS[AGY_MODEL_PRIMARY]} — Gemini Flash (fixed-price, utama)
2. {BACKEND_LABELS[AGY_MODEL_FALLBACK]} — Gemini Pro-low (fallback ke-1)
3. {BACKEND_LABELS[CLAUDE_MODEL_PRIMARY]} — Claude Haiku (fallback ke-2)
4. {BACKEND_LABELS[CLAUDE_MODEL_FALLBACK]} — Claude Sonnet (fallback terakhir)

Tiap jawaban diakhiri tanda "— by ...". Kalau yang jawab BUKAN "{BACKEND_LABELS[AGY_MODEL_PRIMARY]}", itu tanda ada yang lagi bermasalah di tingkat sebelumnya (rate limit, auth, dll) — bisa dipakai buat mantau kesehatan sistem.

*Command tersedia*
/status — kondisi cluster instan, NOL token (langsung dari script, bukan model)
/tools — daftar skill yang sudah jadi script, NOL token
/graduate <nama> — ubah kasus yang BARU SAJA selesai jadi script reusable (nol biaya kalau dipakai ulang lewat /status-style tools)
/new — mulai ulang sesi AKTIF dari nol (riwayat percakapan direset, MEMORY.md tetap ada)
/session <nama> — buat/pindah ke sesi bernama, buat pisahin kasus berbeda
/sessions — lihat semua sesi tersimpan
/remember <fakta> — simpan fakta PERMANEN, ikut kebaca di SEMUA sesi & SEMUA model, walau sudah /new berkali-kali
/memory — lihat isi memory saat ini
/learned — lihat apa saja yang agent pelajari SENDIRI soal lingkungan ini
/forget <nomor> — hapus satu catatan hasil belajar yang keliru (nomornya dari /learned)
/help — panduan ini (pilih EN atau ID)

*Biar tetap irit, 3 kebiasaan penting*
1. *`/new` tiap ganti topik.* Riwayat percakapan yang nyambung itu MAHAL — makin panjang, makin mahal tiap turn berikutnya (bisa 10-20x lipat kalau dibiarkan menumpuk). Infra hari ini beres → mau nanya hal lain (berita, dll)? `/new` dulu.
2. *Jangan `/new` di ANTARA "bikin laporan" dan "kirim ke saya".* Sesi fresh nggak ingat laporan mana yang baru dibuat — kalau langsung tanya "kirim filenya" di sesi baru, dia bakal nyari SEMUA laporan lama yang ada dan nawarin semuanya.
3. *Fakta penting → `/remember`, bukan andalkan riwayat chat.* Kasus yang sudah kelar/diputuskan, taruh di `/remember` biar nggak ditanyain ulang atau terulang — ini SATU-SATUNYA hal yang bertahan lintas `/new`.

*Pakai di Grup Telegram*
Kalau grup ini sudah didaftarkan admin (lihat status pakai `/chatid`), SEMUA anggota grup otomatis bisa kasih command ke bot — nggak perlu izin satu-satu.
1. Kalau bot cuma respons *command* (`/status` dst), bukan chat biasa — mention bot-nya (`@namabot ...`) atau reply pesan bot biar tetap kebaca (ini pengaturan default Telegram, bukan batasan kita).
2. Sesi (`/new`, `/session`) di grup ini *terpisah* dari DM pribadi masing-masing anggota — aman nggak nyampur. Tapi `/remember` itu *GLOBAL* buat SEMUA chat termasuk grup ini — kalau ada yang `/remember` sesuatu, semua orang di sini (dan di chat lain bot ini) bisa lihat lewat `/memory`.
3. Grup ini belum otomatis punya akses? Admin tinggal ketik `/registergroup` di grup ini — langsung aktif, nggak perlu restart apapun. (`/unregistergroup` buat cabut lagi.)

{_HELP_CREDITS}"""

HELP_TEXT_EN = f"""\U0001f4d6 *Lite Agent — Usage Guide*

*How it works*
Every message is tried through 4 tiers, cheapest first:
1. {BACKEND_LABELS[AGY_MODEL_PRIMARY]} — Gemini Flash (fixed-price, primary)
2. {BACKEND_LABELS[AGY_MODEL_FALLBACK]} — Gemini Pro-low (fallback #1)
3. {BACKEND_LABELS[CLAUDE_MODEL_PRIMARY]} — Claude Haiku (fallback #2)
4. {BACKEND_LABELS[CLAUDE_MODEL_FALLBACK]} — Claude Sonnet (last resort)

Every reply ends with a "— by ..." tag. If it's ever NOT "{BACKEND_LABELS[AGY_MODEL_PRIMARY]}", that's a signal something upstream is having trouble (rate limit, auth, etc) -- useful for keeping an eye on system health at a glance.

*Available commands*
/status — instant status check, ZERO tokens (straight from a script, not a model)
/tools — list of skills already turned into scripts, ZERO tokens
/graduate <name> — turn the case you JUST solved into a reusable script (free to reuse afterward)
/new — restart the ACTIVE session from scratch (conversation history reset, MEMORY.md untouched)
/session <name> — create/switch to a named session, for keeping different cases separate
/sessions — list all saved sessions
/remember <fact> — save a fact PERMANENTLY, read in EVERY session & EVERY tier, even after many /new
/memory — view current memory contents
/learned — see what the agent figured out about this environment BY ITSELF
/forget <number> — delete one wrong learned fact (numbers come from /learned)
/learned — see what the agent figured out about this environment BY ITSELF
/forget <number> — delete one wrong learned fact (numbers come from /learned)
/help — this guide (choose EN or ID)

*3 habits that keep it cheap*
1. *`/new` every time the topic changes.* A continuing conversation history is EXPENSIVE -- the longer it gets, the more expensive every next turn (can be 10-20x if left to pile up). Infra case closed → want to ask something unrelated? `/new` first.
2. *Don't `/new` BETWEEN "generate a report" and "send it to me".* A fresh session has no memory of which report was just made -- ask "send the file" in a new session and it'll go looking through every old report that exists and offer all of them.
3. *Important facts → `/remember`, not chat history.* Cases that are closed/decided go into `/remember` so they don't get re-asked or re-investigated -- this is the ONLY thing that survives across `/new`.

*Using it in a Telegram Group*
If this group has been registered by an admin (check with `/chatid`), EVERY member can give the bot commands automatically -- no per-person whitelist needed.
1. If the bot only responds to *commands* (`/status` etc), not plain messages -- mention it (`@botname ...`) or reply to one of its messages to make sure it's seen (that's Telegram's own default setting, not a limitation on our end).
2. Sessions (`/new`, `/session`) in this group are *separate* from each member's private DM -- safely isolated. But `/remember` is *GLOBAL* across every chat including this group -- if someone remembers a fact here, everyone here (and in every other chat with this bot) can see it via `/memory`.
3. This group doesn't have access yet? An admin just needs to type `/registergroup` here -- takes effect immediately, no restart needed. (`/unregistergroup` to revoke it again.)

{_HELP_CREDITS}"""

_HELP_LANG_KEYBOARD = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("\U0001f1ee\U0001f1e9 Indonesia", callback_data="help_id"),
        InlineKeyboardButton("\U0001f1ec\U0001f1e7 English", callback_data="help_en"),
    ]]
)

_HELP_LANG_PROMPT = "Pilih bahasa / Choose a language:"


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg in ("id", "indonesia", "indonesian"):
        await update.message.reply_text(HELP_TEXT_ID, parse_mode="Markdown")
        return
    if arg in ("en", "english"):
        await update.message.reply_text(HELP_TEXT_EN, parse_mode="Markdown")
        return
    await update.message.reply_text(_HELP_LANG_PROMPT, reply_markup=_HELP_LANG_KEYBOARD)


async def cmd_help_lang_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for the /help language-picker buttons."""
    query = update.callback_query
    if not _authorized(update):
        await query.answer()
        return
    text = HELP_TEXT_ID if query.data == "help_id" else HELP_TEXT_EN
    await query.answer()
    await query.edit_message_text(text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    chat_id = str(update.effective_chat.id)
    text = update.message.text or ""
    if not text.strip():
        return

    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    active = state["active"]
    sess = state["sessions"][active]  # {"claude": {model: id}, "agy": {model: id}}, mutated by run_combo

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        result, model, attempts = run_combo(text, sess, active)
    except Exception as exc:
        logger.exception("combo run failed")
        await update.message.reply_text(f"⚠️ Error: {exc}")
        return

    # re-load in case /session or /remember ran concurrently -- unlikely with
    # single-user polling, but avoid clobbering another chat's write.
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    state["sessions"][active] = sess
    save_sessions(sessions)

    label = BACKEND_LABELS.get(model, model)
    reply_text = result.get("result") or "(no response)"
    reply_text, learned_facts = extract_learned(reply_text)
    clean_text, media_paths = extract_media_paths(reply_text)

    # Persist BEFORE delivery: if Telegram is having a bad moment, the thing the
    # agent worked out about this environment shouldn't be lost with the message.
    newly_learned = append_learned(learned_facts) if learned_facts else []

    # The model has ALREADY produced (and been quota-billed for) a real answer at
    # this point -- a transient network blip talking to Telegram (seen in
    # production: httpx ConnectTimeout / TimedOut) must never turn into the user
    # getting silent nothing. Retry the whole delivery once after a short pause
    # (these blips clear in seconds), and if it STILL fails, say so explicitly
    # instead of leaving the chat looking like it's still "thinking".
    for attempt in (1, 2):
        try:
            if clean_text:
                await _reply_chunked(update, clean_text, tag_html=f"— <i>by {label}</i>")
            sent_hashes: set[str] = set()
            for path in media_paths:
                await _send_media_file(update, path, sent_hashes)
            if newly_learned:
                # Visible, not silent: auto-writes the user can't see are how a
                # brief quietly drifts away from what they think it says.
                bullets = "\n".join(f"• {_tg_escape(f)}" for f in newly_learned)
                await update.message.reply_text(
                    f"🧠 <i>Recorded to environment knowledge ({len(newly_learned)} new):</i>\n{bullets}",
                    parse_mode="HTML",
                )
            break
        except Exception:
            logger.exception(
                "delivery to Telegram failed (attempt %d/2, chat=%s session=%s model=%s)",
                attempt, chat_id, active, model,
            )
            if attempt == 1:
                await asyncio.sleep(3)
            else:
                try:
                    await update.message.reply_text(
                        "⚠️ The answer finished processing but couldn't be delivered "
                        "(connection issue reaching Telegram). Please resend the same message."
                    )
                except Exception:
                    logger.exception("even the failure notice couldn't be delivered (chat=%s)", chat_id)

    usage = result.get("usage", {})
    cost = result.get("total_cost_usd")
    logger.info(
        "turn done: chat=%s session=%s model=%s (%s) cost=$%s in=%s out=%s cache_read=%s cache_write=%s | %s",
        chat_id, active, model, label, cost,
        usage.get("input_tokens"), usage.get("output_tokens"),
        usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"),
        " -> ".join(attempts),
    )


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

def main() -> None:
    if not SYSTEM_PROMPT_FILE.exists():
        logger.error("System prompt file not found: %s", SYSTEM_PROMPT_FILE)
        sys.exit(1)
    if not GEMINI_PROMPT_FILE.exists():
        logger.warning(
            "GEMINI.md not found at %s -- agy will run with no environment "
            "context and will likely under-perform. Copy GEMINI.md.template "
            "and fill it in.", GEMINI_PROMPT_FILE,
        )

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("registergroup", cmd_registergroup))
    app.add_handler(CommandHandler("unregistergroup", cmd_unregistergroup))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(cmd_help_lang_chosen, pattern="^help_(en|id)$"))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("tools", cmd_tools))
    app.add_handler(CommandHandler("graduate", cmd_graduate))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("learned", cmd_learned))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Lite Agent starting (allowed users: %s)", ALLOWED_USER_IDS or "ANY (no allowlist!)")
    # drop_pending_updates=False: a transient crash (network blip, etc.) is
    # recovered by systemd's Restart=on-failure in seconds, but with the old
    # True setting, ANY message sent during that gap was silently discarded
    # on restart -- the user got no response and no error, just silence.
    # False means a message sent during a brief outage is still processed
    # once the service comes back, instead of vanishing.
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
