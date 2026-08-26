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

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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

def _build_agy_prompt(prompt: str) -> str:
    """agy has no --append-system-prompt equivalent, so MEMORY.md content (if
    any) is folded into the prompt text itself instead."""
    memory_text = load_memory_text()
    if not memory_text:
        return prompt
    return f"[Cross-session facts to remember:]\n{memory_text}\n\n[User's question:]\n{prompt}"


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
    agy_prompt = _build_agy_prompt(prompt)
    agy_convs = sess.setdefault("agy", {})  # {model_name: conversation_id}
    claude_sessions = sess.setdefault("claude", {})  # {model_name: session_id}

    for model in (AGY_MODEL_PRIMARY, AGY_MODEL_FALLBACK):
        try:
            parsed = _run_agy_once(agy_prompt, model, agy_convs.get(model))
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


async def _reply_chunked(update: Update, text: str) -> None:
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


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
            await update.message.reply_document(document=f, filename=p.name)
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
        "\U0001f680 Designed by Koko Ali & Dede · Developed by BSCloud.id Team\n"
        "Happy smart working! ✨"
    )


HELP_TEXT = f"""\U0001f4d6 *Lite Agent — Usage Guide*

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
/help — this guide

*3 habits that keep it cheap*
1. *`/new` every time the topic changes.* A continuing conversation history is EXPENSIVE -- the longer it gets, the more expensive every next turn (can be 10-20x if left to pile up). Infra case closed → want to ask something unrelated? `/new` first.
2. *Don't `/new` BETWEEN "generate a report" and "send it to me".* A fresh session has no memory of which report was just made -- ask "send the file" in a new session and it'll go looking through every old report that exists and offer all of them.
3. *Important facts → `/remember`, not chat history.* Cases that are closed/decided go into `/remember` so they don't get re-asked or re-investigated -- this is the ONLY thing that survives across `/new`.

*Using it in a Telegram Group*
If this group has been registered by an admin (check with `/chatid`), EVERY member can give the bot commands automatically -- no per-person whitelist needed.
1. If the bot only responds to *commands* (`/status` etc), not plain messages -- mention it (`@botname ...`) or reply to one of its messages to make sure it's seen (that's Telegram's own default setting, not a limitation on our end).
2. Sessions (`/new`, `/session`) in this group are *separate* from each member's private DM -- safely isolated. But `/remember` is *GLOBAL* across every chat including this group -- if someone remembers a fact here, everyone here (and in every other chat with this bot) can see it via `/memory`.
3. This group doesn't have access yet? An admin just needs to type `/registergroup` here -- takes effect immediately, no restart needed. (`/unregistergroup` to revoke it again.)

━━━━━━━━━━━━━━━━━━━
\U0001f680 *Designed by Koko Ali & Dede*
\U0001f4bb *Developed by BSCloud.id Team*

Happy smart working! ✨\U0001f929\U0001f60e"""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


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
    clean_text, media_paths = extract_media_paths(reply_text)
    if clean_text:
        await _reply_chunked(update, f"{clean_text}\n\n— _by {label}_")
    sent_hashes: set[str] = set()
    for path in media_paths:
        await _send_media_file(update, path, sent_hashes)

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
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("tools", cmd_tools))
    app.add_handler(CommandHandler("graduate", cmd_graduate))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("memory", cmd_memory))
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
