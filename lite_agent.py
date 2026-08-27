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
import hmac
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

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
from cli_login import LoginHandle, tmux_available  # noqa: E402
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
RUN_SCHEDULED = BASE_DIR / "tools" / "run_scheduled.py"
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

# Claude Code signs in to a Claude Pro/Max subscription itself (`claude auth
# login`), so no gateway is needed. Setting BOTH of these routes it through an
# Anthropic-compatible gateway instead -- 9Router, LiteLLM, whatever -- which is
# how this project used to work and still can. Leave them unset for the simple
# path.
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
USE_GATEWAY = bool(ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY)
# Explicit literal models, not a 9Router combo alias (e.g. "combo-bayar") --
# a combo hides which underlying model actually answered from us (9Router's
# own JSON always reports the alias, not the resolved sub-model), which would
# break the per-backend "who answered" tag below. Two tiers, mirroring agy's.
# Plain Anthropic model ids, used as-is by Claude Code's own login. Behind a
# gateway these may need that gateway's provider prefix instead (9Router, for
# instance, wants "cc/" -- a bare id 404s there).
CLAUDE_MODEL_PRIMARY = os.environ.get("CLAUDE_MODEL_PRIMARY", "claude-haiku-4-5-20251001")
CLAUDE_MODEL_FALLBACK = os.environ.get("CLAUDE_MODEL_FALLBACK", "claude-sonnet-5")

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
# --------------------------------------------------------------------------
# Write mode: full capability, but only while a human has explicitly opened it
#
# This agent is meant to do real work -- create VMs, repair them, tweak node and
# cluster config, run security audits. Cutting it down to read-only would make
# it useless for exactly the jobs it exists for, so that is not the boundary.
#
# The boundary is WHO ASKED, and WHEN. Almost every turn is a question, and a
# question needs no write access at all. The rare turn that changes something is
# one a human deliberately initiated. So capability is not removed, it is gated:
#
#   locked (default)  -> ~/.ssh/agent_active points at a restricted key whose
#                        authorized_keys entry forces a read-only guard on the
#                        node. Reads, audits and monitoring all work normally.
#   unlocked (opt-in) -> the same symlink points at the full root key, for a
#                        fixed number of minutes, then flips back on its own.
#
# What makes this hold: unlocking is a Telegram command, admin-only, private-DM
# only. It is not something the model can do, ask for, or be talked into by
# text it read in a log file, a web page, or a group message -- the same class
# of input that can otherwise steer a turn. Prompt injection can make the agent
# TRY to change something; it cannot make the credential exist.
#
# The failure mode is also the safe one: if anything about the swap goes wrong,
# what is left in place is the restricted key.
# --------------------------------------------------------------------------

WRITE_STATE_FILE = BASE_DIR / "write_mode.json"
SSH_ACTIVE_KEY = Path(os.environ.get("SSH_ACTIVE_KEY", str(Path.home() / ".ssh/agent_active")))
SSH_RO_KEY = Path(os.environ.get("SSH_RO_KEY", str(Path.home() / ".ssh/agent_readonly")))
SSH_RW_KEY = Path(os.environ.get("SSH_RW_KEY", str(Path.home() / ".ssh/agent_write")))
WRITE_MODE_MAX_MINUTES = int(os.environ.get("WRITE_MODE_MAX_MINUTES", "60"))
WRITE_MODE_DEFAULT_MINUTES = int(os.environ.get("WRITE_MODE_DEFAULT_MINUTES", "15"))


def _keys_configured() -> bool:
    """Both keys present? If not, this whole mechanism is inert and we say so
    rather than pretending a boundary exists that doesn't."""
    return SSH_RO_KEY.exists() and SSH_RW_KEY.exists()


def _point_active_key_at(target: Path) -> None:
    tmp = SSH_ACTIVE_KEY.with_suffix(".swap")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target)
    tmp.replace(SSH_ACTIVE_KEY)  # atomic: never a moment with no key at all


def write_mode_expires_at() -> Optional[float]:
    """Unix ts while write mode is open, else None. Self-healing: an expired
    state file re-locks on read, so expiry needs no timer or background task --
    which matters, because a background task is exactly what this project
    avoids."""
    if not WRITE_STATE_FILE.exists():
        return None
    try:
        until = float(json.loads(WRITE_STATE_FILE.read_text()).get("until", 0))
    except Exception:
        logger.warning("write_mode.json unreadable -- re-locking to be safe", exc_info=True)
        lock_write_mode()
        return None
    if _dt.datetime.now().timestamp() >= until:
        lock_write_mode()
        return None
    return until


def lock_write_mode() -> None:
    try:
        WRITE_STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    if _keys_configured():
        try:
            _point_active_key_at(SSH_RO_KEY)
        except OSError:
            logger.exception("could not swap back to the read-only key")


def unlock_write_mode(minutes: int) -> float:
    minutes = max(1, min(minutes, WRITE_MODE_MAX_MINUTES))
    until = _dt.datetime.now().timestamp() + minutes * 60
    _point_active_key_at(SSH_RW_KEY)
    WRITE_STATE_FILE.write_text(json.dumps({"until": until}))
    logger.warning("WRITE MODE OPENED for %d minute(s)", minutes)
    return until


def write_mode_notice() -> str:
    """Told to the model each turn, so it explains the situation instead of
    hitting a confusing permission error and guessing at the cause."""
    if not _keys_configured():
        return ""
    until = write_mode_expires_at()
    if until:
        left = int((until - _dt.datetime.now().timestamp()) / 60) + 1
        return (
            f"[WRITE MODE IS OPEN for about {left} more minute(s). Changes to the managed "
            "environment are permitted this turn. Still confirm anything destructive with "
            "the user first, and stay inside the hard boundaries above -- those never lift.]"
        )
    return (
        "[READ-ONLY MODE. Your credential physically cannot change the managed environment "
        "right now -- investigate, audit and report freely, but do not attempt changes. If "
        "the user is asking for a change, say plainly that they need to run /unlock first, "
        "and tell them exactly what you would do once they do. Do not try to work around "
        "this, and do not attempt to modify SSH keys or this bot's own files.]"
    )


# --------------------------------------------------------------------------
# Outbound secret redaction
#
# The agent has an unrestricted shell as the same user this bot runs as, so it
# can read .env -- and in practice it did: asked to set up a scheduled report
# that posts to Telegram, it reasonably went and read the bot token out of
# .env. Nothing malicious, just a sensible step with a bad second-order effect,
# and those secrets then sat in plaintext in the CLI's own transcript files.
#
# Trying to stop it reading the file is the wrong place to fight. File
# permissions can't separate the agent from the bot (same user), and the CLIs'
# own deny rules turned out to have surprising semantics (agy silently ignores
# a wildcard deny -- verified by testing, not assumed). Any of those would give
# the *appearance* of a boundary.
#
# So guard the last gate we fully control instead: nothing leaves this process
# toward Telegram carrying a known secret, regardless of how the model came by
# it. Cheap, unconditional, and it does not depend on the model cooperating.
# --------------------------------------------------------------------------
# PIN gate for sensitive actions
#
# A button tap only proves "somebody holding this Telegram account pressed it".
# That is not enough for the cases actually worth defending against: a member's
# phone being compromised, or someone getting into a group they shouldn't be in.
# In both, the attacker HAS the account, so a tap proves nothing. A PIN is a
# second factor that does not travel with the device session.
#
# THE PIN IS NEVER TYPED AS A MESSAGE.
#   Digits are entered on an inline keypad, so they ride in callback_data and
#   never become a chat message. Nothing lands in the chat history, nothing is
#   left for someone scrolling back later, and there is no message to forget to
#   delete. This is the whole reason not to use a typed password: a password in
#   a chat log is a permanent credential sitting in cleartext on servers we do
#   not control.
#
# Stored as a salted scrypt hash -- never the PIN itself. Six digits is only a
# million combinations, so the lockout below is doing real work, not decoration:
# without it, a compromised account could simply walk the space.
# --------------------------------------------------------------------------

PIN_FILE = BASE_DIR / "pin.json"
PIN_LENGTH = 6
PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_SECONDS = 900          # 15 min after burning through the attempts
PIN_ENTRY_TTL_SECONDS = 180        # a half-finished entry expires on its own
# token -> {action, payload, digits, attempts, expires, chat_id}
_pin_sessions: dict[str, dict] = {}
# PIN flows that are safe to run in a group: the digits ride in callback_data,
# so a group only ever sees "somebody is entering a PIN", never the PIN. The
# actions NOT listed here (opening write access, installing an unattended job)
# stay private-DM-only -- a group is exactly where input this deployment does
# not vet arrives, so it is not the place to authorise production changes.
PIN_ACTIONS_ALLOWED_IN_GROUP = frozenset({
    "new_pin_capture", "new_pin_confirm", "change_pin_start",
})
_pin_lockout_until: float = 0.0


def pin_is_set() -> bool:
    return PIN_FILE.exists()


def set_pin(pin: str) -> None:
    salt = os.urandom(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    PIN_FILE.write_text(json.dumps({"salt": salt.hex(), "hash": digest.hex()}))
    try:
        PIN_FILE.chmod(0o600)
    except OSError:
        pass
    logger.warning("PIN was set/changed")


def verify_pin(pin: str) -> bool:
    if not pin_is_set():
        return False
    try:
        data = json.loads(PIN_FILE.read_text())
        salt = bytes.fromhex(data["salt"])
        expected = bytes.fromhex(data["hash"])
    except Exception:
        logger.error("pin.json unreadable -- refusing to verify", exc_info=True)
        return False
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    # Constant-time: a length/short-circuit difference is a timing oracle, and
    # six digits is a small enough space that it would matter.
    return hmac.compare_digest(digest, expected)


def pin_locked_out() -> int:
    """Seconds remaining in lockout, 0 if not locked out."""
    remaining = _pin_lockout_until - _dt.datetime.now().timestamp()
    return int(remaining) if remaining > 0 else 0


def _pin_keyboard(token: str, filled: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(str(d), callback_data=f"pin:{token}:{d}") for d in row]
        for row in ((1, 2, 3), (4, 5, 6), (7, 8, 9))
    ]
    rows.append([
        InlineKeyboardButton("⌫", callback_data=f"pin:{token}:del"),
        InlineKeyboardButton("0", callback_data=f"pin:{token}:0"),
        InlineKeyboardButton("✖️", callback_data=f"pin:{token}:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _pin_masked(filled: int) -> str:
    return "● " * filled + "○ " * (PIN_LENGTH - filled)


def _new_pin_session(action: str, payload: dict, chat_id: int) -> str:
    token = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    _pin_sessions[token] = {
        "action": action,
        "payload": payload,
        "digits": "",
        "chat_id": chat_id,
        "expires": _dt.datetime.now().timestamp() + PIN_ENTRY_TTL_SECONDS,
    }
    # Opportunistic cleanup: no timer, no background task.
    now = _dt.datetime.now().timestamp()
    for t in [t for t, s in _pin_sessions.items() if s["expires"] < now]:
        _pin_sessions.pop(t, None)
    return token


# --------------------------------------------------------------------------
# Scheduled tasks
#
# The agent used to create schedules by editing the crontab itself. That worked,
# and produced a real daily report -- but the job existed nowhere else: it could
# not be listed, could not be removed from Telegram, and nobody would notice a
# second one appearing. An unattended job nobody can enumerate is the exact
# shape of the problem this project exists to avoid.
#
# So schedules follow the same shape as LEARN: the agent PROPOSES one by
# emitting a marker line, and this code owns everything after that -- the
# registry, the crontab, and the human confirmation in between. The agent is
# told not to touch cron directly, and even if it tried, anything it installed
# outside the managed block is visible as "unmanaged" in /schedules rather than
# silently blending in.
#
# Confirmation is a button, not a typed password. A tap proves a human acted; it
# cannot be produced by text the model read in a log file or a group message,
# and unlike a password typed into a chat it leaves no secret in Telegram's
# history. That distinction is the whole point -- a password in a chat log is a
# permanent credential sitting in cleartext on someone else's servers.
# --------------------------------------------------------------------------

SCHEDULES_FILE = BASE_DIR / "schedules.json"
CRON_BEGIN = "# BEGIN iSmart-LA managed -- edited by the bot, do not hand-edit"
CRON_END = "# END iSmart-LA managed"
# SCHEDULE: name=daily-report | when=0 8 * * * | run=python3 x.py | write=no
SCHEDULE_LINE_RE = re.compile(r"^\s*SCHEDULE:\s*(.+?)\s*$", re.MULTILINE)
_CRON_FIELD_RE = re.compile(r"^[\d*/,\-]+$")
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
# Proposals waiting on a button press: token -> schedule dict. Deliberately
# in-memory: a proposal that outlives a restart is a proposal nobody remembers
# agreeing to, so losing them on restart is the correct behaviour.
_pending_schedules: dict[str, dict] = {}


def _read_schedules() -> list[dict]:
    if not SCHEDULES_FILE.exists():
        return []
    try:
        return json.loads(SCHEDULES_FILE.read_text())
    except Exception:
        logger.warning("schedules.json unreadable", exc_info=True)
        return []


def _write_schedules(items: list[dict]) -> None:
    SCHEDULES_FILE.write_text(json.dumps(items, indent=2))


def _valid_cron(expr: str) -> bool:
    fields = expr.split()
    return len(fields) == 5 and all(_CRON_FIELD_RE.match(f) for f in fields)


def _current_crontab() -> str:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def _rebuild_crontab(items: list[dict]) -> None:
    """Regenerate ONLY our managed block, leaving any other crontab entry the
    operator has alone -- clobbering somebody's unrelated cron job would be a
    far worse bug than anything this feature is meant to fix."""
    existing = _current_crontab()
    if CRON_BEGIN in existing and CRON_END in existing:
        head = existing.split(CRON_BEGIN)[0].rstrip("\n")
        tail = existing.split(CRON_END, 1)[1].lstrip("\n")
    else:
        head, tail = existing.rstrip("\n"), ""

    lines = [CRON_BEGIN]
    for it in items:
        lines.append(
            f"{it['when']} {RUN_SCHEDULED} {it['name']} "
            f">> {BASE_DIR / 'scheduled.log'} 2>&1  # ismart:{it['name']}"
        )
    lines.append(CRON_END)
    body = "\n".join(x for x in (head, "\n".join(lines), tail) if x).rstrip("\n") + "\n"

    proc = subprocess.run(["crontab", "-"], input=body, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"crontab update failed: {proc.stderr.strip()}")


def parse_schedule_proposal(raw: str) -> Optional[dict]:
    """Parse one SCHEDULE: line. Returns None if it's malformed -- a proposal we
    cannot fully understand is never partially applied."""
    fields: dict[str, str] = {}
    for chunk in raw.split("|"):
        if "=" not in chunk:
            return None
        k, _, v = chunk.partition("=")
        fields[k.strip().lower()] = v.strip()
    name, when, run = fields.get("name", ""), fields.get("when", ""), fields.get("run", "")
    if not (_SAFE_NAME_RE.match(name) and _valid_cron(when) and run):
        return None
    if any(c in run for c in ";`$\n") or ".." in run:
        return None
    return {
        "name": name,
        "when": when,
        "run": run,
        "needs_write": fields.get("write", "no").lower() in ("yes", "true", "1"),
    }


def extract_schedules(text: str) -> tuple[str, list[dict]]:
    """Pull SCHEDULE: lines out of a reply and strip them from what's shown."""
    proposals = []
    for raw in SCHEDULE_LINE_RE.findall(text):
        parsed = parse_schedule_proposal(raw)
        if parsed:
            proposals.append(parsed)
        else:
            logger.warning("ignored malformed SCHEDULE: line: %s", raw[:120])
    return SCHEDULE_LINE_RE.sub("", text).strip(), proposals


def install_schedule(item: dict, created_by: int) -> None:
    items = [s for s in _read_schedules() if s["name"] != item["name"]]
    items.append({
        **item,
        "created_by": created_by,
        "created_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    _rebuild_crontab(items)
    _write_schedules(items)
    logger.warning(
        "SCHEDULE INSTALLED name=%s when=%r write=%s by=%s",
        item["name"], item["when"], item["needs_write"], created_by,
    )


def remove_schedule(name: str) -> bool:
    items = _read_schedules()
    kept = [s for s in items if s["name"] != name]
    if len(kept) == len(items):
        return False
    _rebuild_crontab(kept)
    _write_schedules(kept)
    logger.warning("SCHEDULE REMOVED name=%s", name)
    return True


def unmanaged_cron_lines() -> list[str]:
    """Cron entries that are NOT ours. Surfaced in /schedules rather than
    hidden: the point of this feature is that nothing runs unattended without
    being visible, and that has to include things we didn't install."""
    existing = _current_crontab()
    if CRON_BEGIN in existing and CRON_END in existing:
        head = existing.split(CRON_BEGIN)[0]
        tail = existing.split(CRON_END, 1)[1]
        existing = head + tail
    return [
        ln.strip() for ln in existing.split("\n")
        if ln.strip() and not ln.strip().startswith("#")
    ]


_SECRET_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY")

_SECRETS = tuple(
    v for v in (os.environ.get(k, "") for k in _SECRET_ENV_KEYS)
    if len(v) >= 12  # short/blank values would match everywhere
)

def redact_secrets(text: str) -> tuple[str, bool]:
    """-> (clean_text, was_redacted)."""
    found = False
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
            found = True
    return text, found


# --------------------------------------------------------------------------
# /start setup wizard
#
# The installer's job stops at "the bot answers on Telegram". Everything that
# needs a human decision or a browser -- signing in to each AI provider, setting
# the PIN -- happens here instead, because Telegram is where the operator
# already is and a browser is where OAuth has to end up anyway.
#
# Both providers sign in the same way (URL out, code back), so both are driven
# through the same tmux helper. The code you paste is short-lived and single-use,
# which is why it is acceptable for it to pass through a chat message at all --
# unlike a password or a private key, which is why neither of those is ever
# asked for here.
# --------------------------------------------------------------------------

WIZARD_STATE_FILE = BASE_DIR / "setup_state.json"
# chat_id -> {"step": ..., "login": LoginHandle, "expires": ts}
_wizard: dict[int, dict] = {}
WIZARD_TTL_SECONDS = 900


def _setup_state() -> dict:
    if WIZARD_STATE_FILE.exists():
        try:
            return json.loads(WIZARD_STATE_FILE.read_text())
        except Exception:
            logger.warning("setup_state.json unreadable, treating as empty", exc_info=True)
    return {}


def _mark_setup(key: str, by: Optional[int] = None) -> None:
    st = _setup_state()
    st[key] = {"done_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "by": by}
    WIZARD_STATE_FILE.write_text(json.dumps(st, indent=2))


def agy_signed_in() -> bool:
    """Best-effort: agy keeps its OAuth material under ~/.gemini. Checking the
    filesystem rather than running agy keeps /start free -- probing by making a
    real call would cost tokens every time someone opens the menu."""
    creds = Path.home() / ".gemini" / "antigravity-cli"
    return creds.exists() and any(creds.rglob("*token*")) or "agy" in _setup_state()


def claude_signed_in() -> bool:
    if USE_GATEWAY:
        return True  # a gateway carries its own credentials
    try:
        # `claude auth status` prints JSON on its own -- passing --output-format
        # makes it exit with "unknown option", which previously read as a
        # missing login even when one was fine.
        proc = subprocess.run([CLAUDE_BIN, "auth", "status"],
                              capture_output=True, text=True, timeout=20)
        return bool(json.loads(proc.stdout).get("loggedIn"))
    except Exception:
        logger.debug("could not read claude auth status", exc_info=True)
        return "claude" in _setup_state()


def setup_summary() -> list[tuple[str, bool, str]]:
    """(label, done, hint) for each thing /start can set up."""
    return [
        ("Gemini (Antigravity)", agy_signed_in(), "the primary tiers -- mini / mini pro"),
        ("Claude Code", claude_signed_in(),
         "gateway configured" if USE_GATEWAY else "the fallback tiers -- dede iku / dede nnet"),
        ("Security PIN", pin_is_set(), "guards changes to production and scheduled tasks"),
    ]


def _wizard_keyboard(state: dict) -> InlineKeyboardMarkup:
    rows = []
    for key, (label, done, _) in zip(("agy", "claude", "pin"), setup_summary()):
        mark = "✅" if done else "⬜"
        verb = "Change" if done else "Set up"
        rows.append([InlineKeyboardButton(f"{mark} {verb} {label}", callback_data=f"setup:{key}")])
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="setup:close")])
    return InlineKeyboardMarkup(rows)


def _wizard_text() -> str:
    lines = ["\U0001f6e0 <b>iSmart-LA setup</b>", ""]
    for label, done, hint in setup_summary():
        lines.append(f"{'✅' if done else '⬜'} <b>{label}</b>\n   <i>{hint}</i>")
    if all(done for _, done, _ in setup_summary()):
        lines.append("\nEverything is set up. Just talk to me normally.")
    else:
        lines.append("\nTap anything above to set it up. You can stop and come back later.")
    return "\n".join(lines)


LEARN_LINE_RE = re.compile(r"^\s*LEARN:\s*(.+?)\s*$", re.MULTILINE)
LEARNED_ZONE_MARKER = "<!-- LEARNED_ZONE -->"
# The LEARNED zone is re-sent at the start of every new conversation, so
# unbounded growth would quietly raise the floor cost of every future turn.
# Oldest entries are dropped past this cap.
LEARNED_MAX_FACTS = 60
# Which of the 4 tiers actually answered, in one glance -- if it's ever NOT
# "mini" (the primary/cheapest tier), that's a visible signal something
# upstream (rate limit, auth hiccup, timeout) forced an escalation.


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger("lite-agent")
# httpx logs every request URL at INFO -- and the Telegram bot token is IN the
# URL path, so at one poll every 10s that writes the bot's own credential to
# disk thousands of times a day (and to journald, which is readable more
# widely). Nothing here needs a per-poll HTTP log line, so lift it to WARNING:
# real HTTP problems still surface, the credential stops being written, and the
# log stops being ~95% getUpdates noise.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
try:  # keep the log readable only by the user the service runs as
    LOG_FILE.touch(exist_ok=True)
    LOG_FILE.chmod(0o600)
except OSError:  # pragma: no cover -- never worth failing startup over
    pass


# --------------------------------------------------------------------------
# Fallback chain
#
# Which providers are used, in what order, and what each one is called, all come
# from one setting. The order in TIERS *is* the fallback order -- first entry is
# tried first, and the chain moves on only when a tier fails in a way that
# another tier could plausibly do better (see _classify_failure).
#
#   TIERS=agy:gemini-3.7-flash-medium:mini,claude:claude-haiku-4-5-20251001:dede iku
#          ^^^ provider  ^^^ model                  ^^^ label shown as "— by <label>"
#
# One list rather than four separate PRIMARY/FALLBACK variables, because the
# useful setups are not all two-plus-two: Gemini only, Claude only, one Gemini
# then straight to Sonnet, or three of one and one of the other. Expressing that
# as an ordered list makes those a config change instead of a code change.
#
# Deployments that predate this still work: with TIERS unset, the chain is
# rebuilt from the old AGY_/CLAUDE_MODEL_* variables in their original order.
# --------------------------------------------------------------------------

KNOWN_PROVIDERS = ("agy", "claude")
_DEFAULT_TIERS = (
    f"agy:{AGY_MODEL_PRIMARY}:mini,"
    f"agy:{AGY_MODEL_FALLBACK}:mini pro,"
    f"claude:{CLAUDE_MODEL_PRIMARY}:dede iku,"
    f"claude:{CLAUDE_MODEL_FALLBACK}:dede nnet"
)


def _parse_tiers(raw: str) -> list[dict]:
    tiers: list[dict] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) < 2:
            logger.error("TIERS entry %r is malformed (need provider:model[:label]) -- skipped", entry)
            continue
        provider, model = parts[0].strip(), parts[1].strip()
        label = parts[2].strip() if len(parts) > 2 and parts[2].strip() else model
        if provider not in KNOWN_PROVIDERS:
            logger.error("TIERS entry %r names unknown provider %r -- skipped", entry, provider)
            continue
        if not model:
            logger.error("TIERS entry %r has no model -- skipped", entry)
            continue
        tiers.append({"provider": provider, "model": model, "label": label})
    return tiers


TIERS = _parse_tiers(os.environ.get("TIERS", "") or _DEFAULT_TIERS)
if not TIERS:
    # Falling back to a hardcoded chain would quietly bill a provider the
    # operator may have deliberately removed. Refusing to start is louder and
    # safer than guessing what they meant.
    raise SystemExit(
        "TIERS is set but no usable entry could be parsed -- refusing to start.\n"
        "Expected: provider:model:label,provider:model:label\n"
        "  providers: " + ", ".join(KNOWN_PROVIDERS)
    )

# Which of the tiers actually answered, in one glance -- if it's ever not the
# FIRST one, something upstream (rate limit, auth hiccup, timeout) forced an
# escalation, and that's worth noticing without digging through logs.
BACKEND_LABELS = {t["model"]: t["label"] for t in TIERS}


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
    if USE_GATEWAY:
        env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL
        env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
    else:
        # Inherited values would silently override the CLI's own subscription
        # login and send traffic somewhere the operator didn't ask for.
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_API_KEY", None)
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
    # Placed BEFORE the early return below: on a resumed turn with an empty
    # MEMORY.md there is nothing else to prepend, and an earlier version
    # returned here -- silently dropping the mode notice in exactly the case
    # it matters most (a long conversation where the model has lost track of
    # whether it is currently allowed to change anything).
    notice = write_mode_notice()
    if notice:
        parts.append(notice)
    if not parts:
        return prompt
    parts.append(f"[User's question:]\n{prompt}")
    return "\n\n".join(parts)


_FAILOVER_HINTS = (
    "rate limit", "ratelimit", "429", "quota", "overloaded", "capacity",
    "unauthenticated", "unauthorized", "401", "403", "auth", "credential",
    "timeout", "timed out", "deadline", "unavailable", "503", "502", "500",
    "connection", "network",
)

_TERMINAL_HINTS = (
    "context length", "context window", "too long", "maximum context",
    "token limit", "prompt is too", "safety", "blocked by", "content policy",
    "recitation",
)

TIER_COOLDOWNS = (60, 300, 900)  # 1m, 5m, then 15m as the cap

_tier_cooldown: dict[str, tuple[float, int]] = {}  # model -> (until_ts, consecutive_failures)

def _classify_failure(exc: Exception) -> str:
    """-> "failover" (try next tier) or "terminal" (stop, it'll fail everywhere)."""
    msg = str(exc).lower()
    for hint in _TERMINAL_HINTS:
        if hint in msg:
            return "terminal"
    for hint in _FAILOVER_HINTS:
        if hint in msg:
            return "failover"
    # Unknown failures get the benefit of the doubt and are treated as
    # provider-specific -- the previous behaviour, kept so a novel error
    # message doesn't silently stop the fallback chain working.
    return "failover"

def _tier_available(model: str) -> bool:
    entry = _tier_cooldown.get(model)
    if not entry:
        return True
    until, _ = entry
    if _dt.datetime.now().timestamp() >= until:
        return True
    return False

def _note_tier_failure(model: str) -> None:
    _, fails = _tier_cooldown.get(model, (0.0, 0))
    fails += 1
    delay = TIER_COOLDOWNS[min(fails - 1, len(TIER_COOLDOWNS) - 1)]
    _tier_cooldown[model] = (_dt.datetime.now().timestamp() + delay, fails)
    logger.warning("tier %s on cooldown for %ds (consecutive failures: %d)", model, delay, fails)

def _note_tier_success(model: str) -> None:
    if model in _tier_cooldown:
        logger.info("tier %s recovered", model)
        _tier_cooldown.pop(model, None)


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
    """Walk the TIERS chain in order, moving on only when a tier fails in a way
    another tier could plausibly survive.

    `sess` is this named session's {"claude": {model: id}, "agy": {model: id}}
    dict -- mutated in place with whichever tier's resume handle actually got
    used; the caller persists it afterward.

    Returns (normalized_result, model_name, attempt_log). model_name is the
    literal model that answered -- never an alias -- so the "— by <label>" tag
    is always exact. attempt_log records every attempt, including skipped and
    failed ones, so a human can see what a turn really cost before it landed.
    """
    attempts: list[str] = []
    agy_convs = sess.setdefault("agy", {})       # {model: conversation_id}
    claude_sessions = sess.setdefault("claude", {})  # {model: session_id}

    for tier in TIERS:
        provider, model = tier["provider"], tier["model"]
        if not _tier_available(model):
            attempts.append(f"{provider}:{model} SKIPPED (cooldown)")
            continue
        try:
            if provider == "agy":
                conv_id = agy_convs.get(model)
                # Each tier keeps its OWN conversation, so "is this fresh?" is a
                # per-tier question -- the brief has to go to whichever tier is
                # starting from nothing, which may be a later one even while an
                # earlier one is mid-conversation.
                parsed = _run_agy_once(
                    _build_agy_prompt(prompt, include_env=(conv_id is None)), model, conv_id
                )
                usage = parsed.get("usage", {}) or {}
                attempts.append(f"agy:{model} OK ({usage.get('total_tokens', '?')} tok)")
                agy_convs[model] = parsed.get("conversation_id")
                _note_tier_success(model)
                return _normalize_agy_result(parsed), model, attempts

            result = run_claude(prompt, claude_sessions.get(model), session_name, model)
            usage = result.get("usage", {}) or {}
            total = sum(v for v in usage.values() if isinstance(v, int))
            attempts.append(f"claude:{model} OK ({total} tok)")
            claude_sessions[model] = result.get("session_id")
            _note_tier_success(model)
            return result, model, attempts

        except Exception as exc:
            kind = _classify_failure(exc)
            attempts.append(f"{provider}:{model} FAILED/{kind} ({exc})")
            logger.warning("%s model=%s failed (%s): %s", provider, model, kind, exc)
            # Don't try to resume a conversation that just errored.
            (agy_convs if provider == "agy" else claude_sessions)[model] = None
            if kind == "terminal":
                # The request itself is the problem -- every remaining tier would
                # spend real quota to fail in exactly the same way.
                raise RuntimeError(
                    f"Request rejected and retrying elsewhere won't help: {exc}"
                ) from exc
            _note_tier_failure(model)

    # Everything was tried and failed, or skipped as cooling down. If it was ALL
    # cooldowns we haven't actually spent anything, so clear them and let the
    # next message retry properly rather than staying dark indefinitely.
    if attempts and all("SKIPPED" in a for a in attempts):
        _tier_cooldown.clear()
        raise RuntimeError("Every tier is cooling down after recent failures; try again shortly.")
    raise RuntimeError(f"All {len(TIERS)} tier(s) failed: {' -> '.join(attempts)}")


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
    """Split on line boundaries, never mid-line, and never mid-STRUCTURE.

    Each chunk is converted independently, so a chunk has to be valid Markdown
    on its own. Splitting purely on length breaks that for anything spanning
    more lines than fit: a fenced code block cut in half leaves both halves
    with an unmatched fence (so neither converts, and the reader sees literal
    backticks), and a table cut below its separator row leaves the tail as a
    pile of stray pipes. Both are easy to hit here -- a long log dump, or a
    table with a row per VM.

    So structure is tracked while chunking: an open code fence is closed at the
    break and reopened (with its original language) on the next chunk, and a
    table's header + separator are repeated, which also just reads better --
    the continuation arrives with its column headings instead of bare rows.
    """
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    open_fence: Optional[str] = None   # the opening fence line, while inside one
    table_head: list[str] = []         # [header, separator], while inside a table

    def carry() -> list[str]:
        if open_fence:
            return [open_fence]
        return list(table_head)

    def flush() -> None:
        nonlocal current, size
        body = list(current)
        if open_fence:
            body.append("```")
        if any(ln.strip() for ln in body):
            chunks.append("\n".join(body))
        current = carry()
        size = sum(len(ln) + 1 for ln in current)

    for raw in lines:
        line = raw
        while len(line) > limit:  # pathological single very long line
            if current:
                flush()
            chunks.append(line[:limit])
            line = line[limit:]

        if current and size + len(line) + 1 > limit:
            flush()
        current.append(line)
        size += len(line) + 1

        stripped = line.strip()
        if stripped.startswith("```"):
            open_fence = None if open_fence else line
            table_head = []
        elif open_fence is None:
            if _TABLE_SEP_RE.match(line):
                header = current[-2] if len(current) >= 2 else ""
                table_head = [header, line] if header.count("|") >= 2 else []
            elif line.count("|") < 2:
                table_head = []

    body = list(current)
    if open_fence:
        body.append("```")
    if any(ln.strip() for ln in body):
        chunks.append("\n".join(body))
    return chunks or [""]


async def _reply_chunked(update: Update, text: str, tag_html: str = "") -> None:
    """Send a (possibly long) model reply, formatted. `tag_html` is appended to
    the LAST chunk only and is passed through as trusted HTML -- it's our own
    backend label, not model output."""
    text, redacted = redact_secrets(text)
    if redacted:
        logger.warning("outbound message contained a known secret -- redacted before sending")
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
    # Same gate as for text, applied to attachments: a report or script that
    # happens to embed a credential must not leave the box. Refuse rather than
    # silently strip -- rewriting someone's file on the way out would be worse
    # than telling them why it wasn't sent.
    if _SECRETS and size <= 8 * 1024 * 1024:
        try:
            blob = p.read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            blob = ""
        if any(s in blob for s in _SECRETS):
            logger.error("REFUSED to send %s -- it contains a credential", p)
            await update.message.reply_text(
                f"🔒 *{p.name}* was not sent: it contains a credential "
                f"(token/API key). Strip that out first if it really needs sending.",
                parse_mode="Markdown",
            )
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


def _is_owner(update: Update) -> bool:
    """Is this the bot owner, regardless of WHERE they're speaking from?

    Weaker than _is_trusted_origin: it says who, not where. Used for actions
    that are safe to run in a group because nothing secret is exposed there --
    /setpin being the case that matters, since the digits travel in
    callback_data and never become a visible message.
    """
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def _is_trusted_origin(update: Update) -> bool:
    """A private DM from a named ALLOWED_USER_IDS account.

    This is the line between "may I answer this?" (_authorized, which includes
    every member of a registered group) and "may this change durable state?".
    Group membership is the weaker claim: the sender is whoever happens to be
    in the group, and their message text is input this deployment does not
    control. Answering it is fine; letting it write to the environment brief
    is not.
    """
    chat = update.effective_chat
    user = update.effective_user
    return bool(
        chat and chat.type == "private"
        and user and user.id in ALLOWED_USER_IDS
    )


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


def _may_run_setup(update: Update) -> bool:
    """Owner anywhere, or a group admin inside a registered group.

    Setup decides which AI account answers on this bot's behalf, so it is not
    open to every group member -- but requiring the owner personally would mean
    a team can't fix a broken login while they're away, which is the moment it
    matters most.
    """
    if _is_owner(update):
        return True
    chat = update.effective_chat
    return bool(chat and chat.type != "private" and chat.id in ALLOWED_GROUP_IDS)


async def _is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user or chat.type == "private":
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("creator", "administrator")
    except Exception:
        logger.warning("could not check admin status in chat=%s", chat.id, exc_info=True)
        return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The setup wizard, and the first thing anyone runs."""
    if not _authorized(update):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return
    if not _may_run_setup(update):
        await update.message.reply_text(
            "\U0001f44b I'm ready. Send anything to get started — status checks, "
            "investigations, reports.\n\nType /help for the full guide."
        )
        return
    if not _is_owner(update) and not await _is_group_admin(update, context):
        await update.message.reply_text(
            "\U0001f44b I'm ready. Send anything to get started.\n\n"
            "Type /help for the guide. (Setup is limited to the bot owner and this "
            "group's admins.)"
        )
        return

    done = [d for _, d, _ in setup_summary()]
    if all(done) and not _is_owner(update):
        await update.message.reply_text(
            "✅ This bot is already set up by the owner.\n\nChange anything?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, change setup", callback_data="setup:menu"),
                InlineKeyboardButton("No, leave it", callback_data="setup:close"),
            ]]),
        )
        return
    await update.message.reply_text(_wizard_text(), parse_mode="HTML",
                                    reply_markup=_wizard_keyboard({}))


async def cmd_setup_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, what = query.data.partition(":")
    if not _may_run_setup(update):
        await query.answer("Not permitted.", show_alert=True)
        return
    if not _is_owner(update) and not await _is_group_admin(update, context):
        await query.answer("Group admins only.", show_alert=True)
        return
    await query.answer()

    if what == "close":
        await query.edit_message_text("Setup closed. Run /start any time.")
        return
    if what == "menu":
        await query.edit_message_text(_wizard_text(), parse_mode="HTML",
                                      reply_markup=_wizard_keyboard({}))
        return
    if what == "pin":
        await _begin_new_pin(update, query) if not pin_is_set() else await request_pin(
            update, "change_pin_start", {},
            "🔢 Changing the PIN. First, confirm the CURRENT one.")
        return
    if what in ("agy", "claude"):
        await _begin_cli_login(update, query, what)
        return


async def _begin_cli_login(update: Update, query, provider: str) -> None:
    """Kick off a provider's OAuth and show the URL."""
    if not tmux_available():
        await query.edit_message_text(
            "⚠️ tmux isn't installed, and it's needed to drive the sign-in screen.\n"
            "Install it (<code>apt install tmux</code>) and try again.",
            parse_mode="HTML")
        return

    if provider == "agy":
        cmd, human = [AGY_BIN], "Antigravity (Gemini)"
    else:
        cmd, human = [CLAUDE_BIN, "auth", "login"], "Claude Code"

    handle = LoginHandle(session=f"ismart-login-{provider}", command=cmd)
    await query.edit_message_text(f"⏳ Starting {human} sign-in…")
    try:
        handle.start()
        url = await asyncio.get_running_loop().run_in_executor(None, handle.wait_for_url, 45)
    except Exception as exc:
        logger.exception("login start failed for %s", provider)
        await query.edit_message_text(f"⚠️ Couldn't start the sign-in: {exc}")
        return

    if url is None:
        screen = handle.pane()
        handle.kill()
        if LoginHandle.already_done(screen):
            _mark_setup(provider, update.effective_user.id)
            await query.edit_message_text(f"✅ {human} is already signed in.")
            return
        await query.edit_message_text(
            f"⚠️ Couldn't find a sign-in URL for {human}. Last output:\n\n"
            f"<pre>{_tg_escape(screen[-600:])}</pre>", parse_mode="HTML")
        return

    _wizard[update.effective_chat.id] = {
        "step": f"await_code_{provider}",
        "handle": handle,
        "human": human,
        "expires": _dt.datetime.now().timestamp() + WIZARD_TTL_SECONDS,
    }
    await query.edit_message_text(
        f"🔗 <b>Sign in to {human}</b>\n\n"
        "1. Open this on any device:\n"
        f"{_tg_escape(url)}\n\n"
        "2. Approve it, copy the code you get back.\n"
        "3. <b>Send that code here as your next message.</b>\n\n"
        "<i>The code is single-use and expires quickly, which is why it's safe to "
        "paste in chat — unlike a password or an SSH key, which I'll never ask for.</i>\n\n"
        "Send /cancel to stop.",
        parse_mode="HTML", disable_web_page_preview=True,
    )


async def _handle_wizard_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If this chat is mid-wizard, treat the message as the OAuth code.
    Returns True when the message was consumed and must not reach the model."""
    chat_id = update.effective_chat.id
    state = _wizard.get(chat_id)
    if not state:
        return False
    if state["expires"] < _dt.datetime.now().timestamp():
        _wizard.pop(chat_id, None)
        state["handle"].kill()
        await update.message.reply_text("⌛ That sign-in expired. Run /start to try again.")
        return True

    text = (update.message.text or "").strip()
    if text.lower() in ("/cancel", "cancel", "batal"):
        _wizard.pop(chat_id, None)
        state["handle"].kill()
        await update.message.reply_text("✖️ Sign-in cancelled.")
        return True

    handle, human = state["handle"], state["human"]
    _wizard.pop(chat_id, None)
    await update.message.reply_text(f"⏳ Sending the code to {human}…")

    # The code was a chat message and is a credential, however short-lived --
    # take it out of the history now rather than leaving it sitting there.
    try:
        await update.message.delete()
    except Exception:
        logger.info("could not delete the code message (needs admin rights in groups)")

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, handle.send_code, text)
        ok, screen = await loop.run_in_executor(None, handle.wait_for_result, 120)
    except Exception as exc:
        logger.exception("login completion failed")
        handle.kill()
        await update.message.reply_text(f"⚠️ Sign-in failed: {exc}")
        return True
    handle.kill()

    if ok:
        provider = "agy" if "Antigravity" in human else "claude"
        _mark_setup(provider, update.effective_user.id)
        logger.warning("%s sign-in completed by user=%s", human, update.effective_user.id)
        await update.message.reply_text(
            f"✅ <b>{human} signed in.</b>\n\nRun /start to see what's left.",
            parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"⚠️ {human} didn't accept that code. It may have expired — codes are "
            f"short-lived, so grabbing a fresh one usually fixes it.\n\n"
            f"<pre>{_tg_escape(screen[-500:])}</pre>\n\nRun /start to retry.",
            parse_mode="HTML")
    return True


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    state = _wizard.pop(update.effective_chat.id, None)
    if state:
        state["handle"].kill()
        await update.message.reply_text("✖️ Cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


def _tier_summary() -> str:
    """The live chain, numbered, for /help. Built from TIERS so it can never
    drift from what the bot will actually do."""
    lines = []
    for i, t in enumerate(TIERS, 1):
        where = "primary" if i == 1 else ("last resort" if i == len(TIERS) else f"fallback #{i - 1}")
        lines.append(f"{i}. {t['label']} \u2014 {t['model']} ({where})")
    return "\n".join(lines)


_HELP_CREDITS = (
    "━━━━━━━━━━━━━━━━━━━\n"
    "\U0001f680 *Designed by Koko Ali & Dede*\n"
    "\U0001f4bb *Developed by Infrasoft.cloud & BSCloud.id Team*\n\n"
    "Happy smart working! ✨\U0001f929\U0001f60e"
)

HELP_TEXT_EN = f"""\U0001f4d6 *iSmart-LA — Usage Guide*

*How it works*
Every message is tried through 4 tiers, cheapest first:
{_tier_summary()}

Every reply ends with a "— by ..." tag. If it's ever NOT "{TIERS[0]['label']}", that's a signal something upstream is having trouble (rate limit, auth, etc) -- useful for keeping an eye on system health at a glance.

*Available commands*
/status — instant status check, ZERO tokens (straight from a script, not a model)
/tools — list of skills already turned into scripts, ZERO tokens
/graduate <name> — turn the case you JUST solved into a reusable script (free to reuse afterward)
/new — restart the ACTIVE session from scratch (conversation history reset, MEMORY.md untouched)
/session <name> — create/switch to a named session, for keeping different cases separate
/sessions — list all saved sessions
/remember <fact> — save a fact PERMANENTLY, read in EVERY session & EVERY tier, even after many /new
/memory — view current memory contents
/schedules — everything that runs on a timer, and what it does (0 tokens)
/unschedule <name> — remove a scheduled task
/adopt — bring pre-existing cron entries under management
/setpin — set/change the 6-digit PIN (entered on a keypad, never typed in chat)
/providers — which AI tiers are configured and which are healthy (0 tokens)
/mode — read-only right now, or able to make changes? (0 tokens)
/unlock [minutes] — owner-only, DM-only: allow real changes for a limited window
/lock — close that window early
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
{_tier_summary()}

Setiap balasan diakhiri tanda "— by ...". Kalau tandanya BUKAN "{TIERS[0]['label']}", itu sinyal ada gangguan di salah satu layanan (rate limit, auth, dll) -- gampang dipantau sekilas tanpa perlu buka log.

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


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open write mode for a fixed window.

    Deliberately stricter than _authorized(): _is_trusted_origin() means a
    private DM from a named account. A group is a place where text arrives from
    people and systems this deployment does not vet, and this is the one command
    that hands the agent the ability to change production -- so it is not
    something a group can reach, even a registered one.
    """
    if not _is_trusted_origin(update):
        logger.warning(
            "refused /unlock from chat=%s user=%s",
            getattr(update.effective_chat, "id", "?"),
            getattr(update.effective_user, "id", "?"),
        )
        await update.message.reply_text(
            "🔒 /unlock only works in a private DM from the bot owner."
        )
        return
    if not _keys_configured():
        await update.message.reply_text(
            "⚠️ Write-mode keys are not set up, so there is nothing to unlock — the agent "
            f"is using whatever `{SSH_ACTIVE_KEY.name}` already points at.\n\n"
            "See README (\"Write mode\") to enable the two-key setup.",
            parse_mode="Markdown",
        )
        return

    minutes = WRITE_MODE_DEFAULT_MINUTES
    if context.args:
        try:
            minutes = int(context.args[0])
        except ValueError:
            await update.message.reply_text(f"Usage: /unlock [minutes, max {WRITE_MODE_MAX_MINUTES}]")
            return
    # Opening write access is the most consequential thing this bot can do,
    # so it takes more than a tap from a signed-in device. The PIN is the
    # factor that does not come along with a stolen session, and it is
    # entered on a keypad so it never becomes a message in the chat.
    await request_pin(
        update, "unlock", {"minutes": minutes},
        f"🔓 Confirm opening write mode for {minutes} minute(s).",
    )


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trusted_origin(update):
        return
    was_open = write_mode_expires_at() is not None
    lock_write_mode()
    await update.message.reply_text(
        "🔒 Write mode closed. Back to read-only." if was_open
        else "🔒 Already read-only."
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Zero-token check of what the agent can currently do."""
    if not _authorized(update):
        return
    if not _keys_configured():
        await update.message.reply_text(
            "⚠️ Write-mode keys not configured — the agent uses one fixed SSH key, so it "
            "can change things at any time. See README (\"Write mode\") to gate that."
        )
        return
    until = write_mode_expires_at()
    if until:
        left = int((until - _dt.datetime.now().timestamp()) / 60) + 1
        await update.message.reply_text(f"🔓 Write mode OPEN — about {left} minute(s) left.")
    else:
        await update.message.reply_text(
            "🔒 Read-only. Investigation, audits and reports work normally.\n"
            "Need a change? /unlock [minutes]"
        )


async def request_pin(update: Update, action: str, payload: dict, prompt: str) -> None:
    """Put a PIN keypad in front of a sensitive action.

    The action is NOT performed here -- it is parked with the session and only
    runs once the PIN checks out, in _pin_verified().
    """
    if not pin_is_set():
        await update.effective_message.reply_text(
            "🔢 No PIN is set yet, so sensitive actions are blocked.\n"
            "Set one first with /setpin (owner, private DM)."
        )
        return
    left = pin_locked_out()
    if left:
        await update.effective_message.reply_text(
            f"⛔ Too many wrong PIN attempts. Locked for another {left // 60 + 1} minute(s)."
        )
        return
    token = _new_pin_session(action, payload, update.effective_chat.id)
    await update.effective_message.reply_text(
        f"{prompt}\n\n🔢 Enter your {PIN_LENGTH}-digit PIN:\n{_pin_masked(0)}",
        reply_markup=_pin_keyboard(token, 0),
    )


async def _redraw(query, header: str, token: str, filled: int, note: str = "") -> None:
    try:
        await query.edit_message_text(
            f"{header}{note}\n{_pin_masked(filled)}",
            reply_markup=_pin_keyboard(token, filled),
        )
    except BadRequest:
        pass  # unchanged text (e.g. ⌫ on an empty entry) -- nothing to redraw


async def cmd_pin_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every keypad press lands here. Digits ride in callback_data, so they are
    never a chat message and never enter the chat history."""
    global _pin_lockout_until
    query = update.callback_query
    _, token, key = query.data.split(":", 2)
    session = _pin_sessions.get(token)
    if not session:
        await query.answer("This PIN entry expired — start again.", show_alert=True)
        return
    # Only the owner may drive the keypad, wherever they are...
    if not _is_owner(update):
        await query.answer("Not permitted.", show_alert=True)
        return
    # ...but the action decides whether this is an acceptable PLACE to do it.
    if (session["action"] not in PIN_ACTIONS_ALLOWED_IN_GROUP
            and not _is_trusted_origin(update)):
        await query.answer("This action can only be confirmed in a private DM.",
                           show_alert=True)
        return
    if session["expires"] < _dt.datetime.now().timestamp():
        _pin_sessions.pop(token, None)
        await query.answer("Expired.", show_alert=True)
        await query.edit_message_text("🔢 PIN entry expired.")
        return

    if key == "cancel":
        _pin_sessions.pop(token, None)
        await query.answer()
        await query.edit_message_text("✖️ Cancelled.")
        return
    if key == "del":
        session["digits"] = session["digits"][:-1]
    elif key.isdigit():
        session["digits"] += key
    await query.answer()

    header = (query.message.text or "").split("\n🔢")[0].split("\n●")[0].split("\n○")[0]
    header = header.split("\n❌")[0].rstrip() + f"\n\n🔢 {PIN_LENGTH}-digit PIN:"
    filled = len(session["digits"])
    if filled < PIN_LENGTH:
        await _redraw(query, header, token, filled)
        return

    entered = session["digits"]
    session["digits"] = ""

    # Choosing a NEW pin has nothing to verify against yet.
    if session["action"] in ("new_pin_capture", "new_pin_confirm"):
        await _pin_capture(update, query, session, token, entered)
        return

    if not verify_pin(entered):
        session["attempts"] = session.get("attempts", 0) + 1
        if session["attempts"] >= PIN_MAX_ATTEMPTS:
            _pin_sessions.pop(token, None)
            _pin_lockout_until = _dt.datetime.now().timestamp() + PIN_LOCKOUT_SECONDS
            logger.error("PIN lockout triggered after %d failed attempts", PIN_MAX_ATTEMPTS)
            await query.edit_message_text(
                f"⛔ Wrong PIN {PIN_MAX_ATTEMPTS} times. Locked for "
                f"{PIN_LOCKOUT_SECONDS // 60} minutes."
            )
            return
        remaining = PIN_MAX_ATTEMPTS - session["attempts"]
        logger.warning("wrong PIN (%d attempt(s) left)", remaining)
        await _redraw(query, header, token, 0, f"\n❌ Wrong PIN — {remaining} attempt(s) left.")
        return

    _pin_sessions.pop(token, None)
    await _pin_verified(update, query, session)


async def _pin_capture(update: Update, query, session: dict, token: str, entered: str) -> None:
    """Two-step entry for a new PIN: type it, then type it again. A mistyped PIN
    that locks you out of your own production changes is a bad afternoon."""
    if session["action"] == "new_pin_capture":
        _pin_sessions.pop(token, None)
        confirm_token = _new_pin_session("new_pin_confirm", {"first": entered},
                                         update.effective_chat.id)
        await query.edit_message_text(
            f"🔢 Enter the same {PIN_LENGTH} digits again to confirm:\n{_pin_masked(0)}",
            reply_markup=_pin_keyboard(confirm_token, 0),
        )
        return

    _pin_sessions.pop(token, None)
    if entered != session["payload"]["first"]:
        await query.edit_message_text("❌ The two entries didn't match. Run /setpin again.")
        return
    set_pin(entered)
    await query.edit_message_text(
        "✅ PIN set. It now guards scheduled tasks and /unlock.\n"
        "It is stored only as a salted hash, and it is never typed into the chat."
    )


async def _pin_verified(update: Update, query, session: dict) -> None:
    """PIN checked out -- carry out the action that was waiting on it."""
    action, payload = session["action"], session["payload"]

    if action == "change_pin_start":
        await _begin_new_pin(update, query)
        return

    if action == "schedule_install":
        item = payload["item"]
        try:
            install_schedule(item, update.effective_user.id)
        except Exception as exc:
            logger.exception("schedule install failed")
            await query.edit_message_text(f"⚠️ Could not install: {exc}")
            return
        await query.edit_message_text(
            f"✅ Installed <b>{_tg_escape(item['name'])}</b> — runs "
            f"<code>{_tg_escape(item['when'])}</code>.\n"
            f"See /schedules, remove with /unschedule {_tg_escape(item['name'])}.",
            parse_mode="HTML",
        )
        return

    if action == "unlock":
        try:
            until = unlock_write_mode(payload["minutes"])
        except OSError as exc:
            logger.exception("unlock failed")
            await query.edit_message_text(f"⚠️ Could not unlock: {exc}")
            return
        left = int((until - _dt.datetime.now().timestamp()) / 60) + 1
        await query.edit_message_text(
            f"🔓 Write mode open for {left} minute(s). It re-locks by itself; "
            "/lock closes it sooner. Hard boundaries still apply."
        )
        return

    logger.error("unknown PIN action: %s", action)
    await query.edit_message_text("⚠️ Internal error: unknown action.")


async def _begin_new_pin(update: Update, query=None) -> None:
    token = _new_pin_session("new_pin_capture", {}, update.effective_chat.id)
    chat = update.effective_chat
    in_group = bool(chat and chat.type != "private")
    text = (
        f"🔢 Choose a new {PIN_LENGTH}-digit PIN.\n"
        "Avoid birthdays and 123456 — this is the last gate in front of "
        f"production changes.\n"
        + ("\n\u2139\ufe0f You're doing this in a group. Your digits stay private "
           "(they never become a message), but the group can see that you're "
           "setting a PIN right now.\n" if in_group else "")
        + f"{_pin_masked(0)}"
    )
    kb = _pin_keyboard(token, 0)
    if query is not None:
        await query.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


async def cmd_setpin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or change the PIN.

    Works in a group as well as a DM: the digits ride in callback_data, so a
    group sees a keypad and a row of dots, never the PIN. Changing an existing
    PIN always requires entering the CURRENT one first, so a stolen Telegram
    session cannot quietly replace it -- which is what makes the group case
    acceptable in the first place.
    """
    if not _is_owner(update):
        # Silent for non-owners: the same non-response whether the command
        # doesn't exist or they simply aren't allowed to use it.
        return
    if pin_is_set():
        await request_pin(update, "change_pin_start", {},
                          "🔢 Changing your PIN. First, confirm the CURRENT one.")
        return
    await _begin_new_pin(update)


async def offer_schedules(update: Update, proposals: list[dict]) -> None:
    """Ask for a human tap before anything is installed.

    Nothing is written to cron here -- the proposal is only parked in memory
    until somebody presses the button. If the bot restarts first, the proposal
    is gone, which is right: a pending change nobody confirmed should not
    survive to be confirmed by accident later.
    """
    for item in proposals:
        token = hashlib.sha256(
            f"{item['name']}{_dt.datetime.now().timestamp()}".encode()
        ).hexdigest()[:16]
        _pending_schedules[token] = item
        warn = ""
        if item["needs_write"]:
            warn = (
                "\n\n⚠️ This task asks for WRITE access, so it can change things "
                "with nobody watching. Only approve that if it genuinely needs to."
            )
        await update.message.reply_text(
            f"🗓 <b>Install this scheduled task?</b>\n\n"
            f"<b>Name:</b> <code>{_tg_escape(item['name'])}</code>\n"
            f"<b>When:</b> <code>{_tg_escape(item['when'])}</code>\n"
            f"<b>Runs:</b> <code>{_tg_escape(item['run'][:300])}</code>\n"
            f"<b>Write access:</b> {'YES' if item['needs_write'] else 'no'}"
            f"{warn}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Install", callback_data=f"sched_ok:{token}"),
                InlineKeyboardButton("✖️ Cancel", callback_data=f"sched_no:{token}"),
            ]]),
        )


async def cmd_schedule_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, token = query.data.partition(":")
    # Same gate as /unlock: installing an unattended job is not something a
    # group should be able to do, even a registered one.
    if not _is_trusted_origin(update):
        await query.answer("Only the bot owner, in a private DM.", show_alert=True)
        return
    item = _pending_schedules.pop(token, None)
    if not item:
        await query.answer()
        await query.edit_message_text("That proposal has expired — ask again if you still want it.")
        return
    if action == "sched_no":
        await query.answer()
        await query.edit_message_text(f"✖️ Not installed: {item['name']}")
        return
    # The tap alone is not the authorisation. It only says WHICH proposal; the
    # PIN says a person -- not just a logged-in device -- actually wants it.
    await query.answer()
    await query.edit_message_text(
        f"🗓 Installing <b>{_tg_escape(item['name'])}</b> — confirm with your PIN.",
        parse_mode="HTML",
    )
    await request_pin(
        update, "schedule_install", {"item": item},
        f"🗓 Confirm installing scheduled task <b>{_tg_escape(item['name'])}</b>.",
    )


async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everything that runs on a timer. Zero tokens -- read straight from the
    registry and the crontab, no model involved."""
    if not _authorized(update):
        return
    items = _read_schedules()
    lines: list[str] = []
    if items:
        lines.append(f"🗓 <b>Scheduled tasks ({len(items)})</b>\n")
        for it in items:
            flag = " ⚠️ <i>write access</i>" if it.get("needs_write") else ""
            lines.append(
                f"• <b>{_tg_escape(it['name'])}</b>{flag}\n"
                f"  <code>{_tg_escape(it['when'])}</code> — since {it.get('created_at','?')}\n"
                f"  <code>{_tg_escape(it['run'][:160])}</code>"
            )
    else:
        lines.append("🗓 No scheduled tasks registered.")

    orphans = unmanaged_cron_lines()
    if orphans:
        lines.append(
            "\n⚠️ <b>Unmanaged cron entries</b> — these run on a timer but were not "
            "installed through this bot, so they cannot be removed with /unschedule:"
        )
        for o in orphans[:10]:
            lines.append(f"  <code>{_tg_escape(o[:160])}</code>")
        lines.append("<i>Use /adopt to bring them under management.</i>")

    if items:
        lines.append("\nRemove one: /unschedule &lt;name&gt;")
    await _reply_chunked(update, "\n".join(lines))


async def cmd_unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trusted_origin(update):
        await update.message.reply_text("🔒 Only the bot owner, in a private DM.")
        return
    name = (context.args[0] if context.args else "").strip()
    if not name:
        await update.message.reply_text("Usage: /unschedule <name>\nSee names with /schedules")
        return
    if remove_schedule(name):
        await update.message.reply_text(f"🗑 Removed scheduled task: {name}")
    else:
        await update.message.reply_text(f"No scheduled task named '{name}'. See /schedules")


async def cmd_adopt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pull pre-existing cron entries into the registry so they become visible
    and removable. Written for a real case: the agent installed a daily report
    before this feature existed, and it was invisible from Telegram."""
    if not _is_trusted_origin(update):
        await update.message.reply_text("🔒 Only the bot owner, in a private DM.")
        return
    orphans = unmanaged_cron_lines()
    if not orphans:
        await update.message.reply_text("Nothing to adopt — every cron entry is already managed.")
        return
    items = _read_schedules()
    known = {s["name"] for s in items}
    adopted: list[str] = []
    for idx, line in enumerate(orphans, 1):
        parts = line.split()
        if len(parts) < 6 or not _valid_cron(" ".join(parts[:5])):
            continue
        name = f"adopted-{idx}"
        while name in known:
            idx += 1
            name = f"adopted-{idx}"
        known.add(name)
        items.append({
            "name": name,
            "when": " ".join(parts[:5]),
            "run": " ".join(parts[5:]).split(">>")[0].strip(),
            "needs_write": False,
            "created_by": update.effective_user.id,
            "created_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M") + " (adopted)",
        })
        adopted.append(name)
    if not adopted:
        await update.message.reply_text("Found cron entries but could not parse any of them.")
        return
    _rebuild_crontab(items)
    _write_schedules(items)
    await update.message.reply_text(
        f"✅ Adopted {len(adopted)} entry/entries: {', '.join(adopted)}\n"
        "They now show in /schedules and can be removed with /unschedule."
    )


async def cmd_providers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The live fallback chain and each tier's current health. Zero tokens --
    read straight from config and the in-memory cooldown table."""
    if not _authorized(update):
        return
    lines = [f"\U0001f9e9 <b>Fallback chain ({len(TIERS)} tier(s))</b>", ""]
    for i, t in enumerate(TIERS, 1):
        if _tier_available(t["model"]):
            state = "✅ ready"
        else:
            until, fails = _tier_cooldown[t["model"]]
            mins = int((until - _dt.datetime.now().timestamp()) / 60) + 1
            state = f"⏸ cooling down ~{mins}m (after {fails} failure(s))"
        role = "primary" if i == 1 else ("last resort" if i == len(TIERS) else f"fallback #{i - 1}")
        lines.append(
            f"{i}. <b>{_tg_escape(t['label'])}</b> — {role}\n"
            f"   <code>{_tg_escape(t['provider'])}</code> / <code>{_tg_escape(t['model'])}</code>\n"
            f"   {state}"
        )
    lines.append(
        "\nReplies are tagged with whichever tier answered. Anything other than "
        f"<b>{_tg_escape(TIERS[0]['label'])}</b> means the ones above it were unavailable."
    )
    lines.append("\n<i>Change the chain by editing TIERS in .env, then restart.</i>")
    await _reply_chunked(update, "\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if await _handle_wizard_input(update, context):
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
    reply_text, schedule_proposals = extract_schedules(reply_text)
    clean_text, media_paths = extract_media_paths(reply_text)

    # Persist BEFORE delivery: if Telegram is having a bad moment, the thing the
    # agent worked out about this environment shouldn't be lost with the message.
    #
    # But only from a trusted origin. A turn driven by a group member is a turn
    # driven by input we don't control, and a "fact" learned from it outlives
    # the conversation -- it gets re-read at the start of every future one. That
    # turns a single bad message into permanent context poisoning. Reading and
    # answering from a group is fine; writing to long-term memory from one is
    # not, so the learned zone only accepts facts from a trusted DM.
    if learned_facts and not _is_trusted_origin(update):
        logger.warning(
            "ignored %d LEARN: line(s) from an untrusted origin (chat=%s)",
            len(learned_facts), chat_id,
        )
        learned_facts = []
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
            if schedule_proposals and _is_trusted_origin(update):
                await offer_schedules(update, schedule_proposals)
            elif schedule_proposals:
                logger.warning(
                    "ignored %d SCHEDULE: proposal(s) from an untrusted origin (chat=%s)",
                    len(schedule_proposals), chat_id,
                )
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
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(cmd_setup_button, pattern="^setup:"))
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
    app.add_handler(CommandHandler("setpin", cmd_setpin))
    app.add_handler(CallbackQueryHandler(cmd_pin_key, pattern="^pin:"))
    app.add_handler(CommandHandler("providers", cmd_providers))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("unschedule", cmd_unschedule))
    app.add_handler(CommandHandler("adopt", cmd_adopt))
    app.add_handler(CallbackQueryHandler(cmd_schedule_decision, pattern="^sched_(ok|no):"))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("mode", cmd_mode))
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
