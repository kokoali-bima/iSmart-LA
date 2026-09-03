#!/usr/bin/env python3
# ------------------------------------------------------------------------------
# iSmart-LA -- Copyright (c) 2026 Infrasoft.cloud & BSCloud.id Team.
# See LICENSE. Any deployment or redistribution of this software must retain
# the "Designed by Koko Ali & Dede - Developed by Infrasoft.cloud & BSCloud.id
# Team" credit as it appears in /start and /help below -- do not remove it.
# ------------------------------------------------------------------------------
"""
iSmart-LA (Lite Agent) -- a lightweight Telegram bridge to Claude Code and
Antigravity CLI. Both CLIs sign in to their own fixed-price subscriptions
directly (no gateway required); see README "Using a gateway instead" for the
optional path if one is ever needed.

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
    Pro/Ultra plan, Claude via Claude Code's own OAuth sign-in on a Claude
    Pro (or higher) plan. Gemini is tried first anyway -- not to dodge
    per-token cost, but to spread routine load across a SEPARATE subscription
    and keep the Claude plan's own usage quota in reserve for when it's
    genuinely needed.
"""

from __future__ import annotations

import asyncio
import functools
import datetime as _dt
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import tempfile
import shutil
import getpass
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.request import HTTPXRequest

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
OWNER_SCOPE_FILE = BASE_DIR / "OWNER_SCOPE.md"
# BOTH CLIs already speak MCP natively, so this inherits the whole MCP tool
# ecosystem without a single line of new agent code. Verified live before
# being wired in, not assumed from docs -- and the first assumption made here
# was wrong, which is why it was checked:
#
#   claude -- takes --mcp-config per invocation. A hand-rolled stdio probe
#             server plus a real tools/call round-trip returned the exact
#             marker string, with "permission_denials":[] confirming that
#             --allowedTools "mcp__<server>" genuinely grants access.
#   agy    -- was assumed to have no MCP support at all. It does: an `agy mcp`
#             subcommand with add/remove/list, writing to agy's OWN persistent
#             config rather than taking a flag. Confirmed with a real Gemini
#             turn that used a registered server's tools and read back the
#             right value from a real file.
#
# That difference matters for cost: agy is the DEFAULT, cheapest tier, so
# without the agy half, MCP tools would only ever be reachable on the
# expensive escalation path -- exactly backwards.
MCP_CONFIG_FILE = BASE_DIR / "mcp_servers.json"
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
LOG_FILE = BASE_DIR / "lite-agent.log"
# One JSON line per completed turn. This project's whole claim is about cost,
# but until now that cost only ever existed as a sentence inside a log line --
# readable by a human scrolling, useless for adding up. The numbers were
# already in hand (run_combo collects a usage block from both providers);
# what was missing was writing them down as data. /spend then costs 0 tokens.
LEDGER_FILE = BASE_DIR / "spend.jsonl"
# Hard cap on what ONE turn may burn across the whole fallback chain, counting
# tokens spent by tiers that failed. 0 = off, and that is the deliberate
# default: this deployment's own ledger shows legitimate work reaching 110k on
# a 7-node report and 750k on a long conversation, while yesterday's waste
# events were ~120k each -- the ranges overlap, so a number picked before
# measuring would either never fire or would kill real work. Measure with
# /spend first, then set this. Checked BETWEEN tiers (see run_combo), because
# that is the only point where stopping is still possible: tokens inside a
# single CLI call are already spent by the time it returns.
TURN_TOKEN_CEILING = int(os.environ.get("TURN_TOKEN_CEILING", "0") or 0)
# Deterministic collectors: known questions answered by a script, no LLM
# involved, so a repeat of an already-solved case costs zero tokens. See /status.
RUN_SCHEDULED = BASE_DIR / "tools" / "run_scheduled.py"
SNAPSHOT_SCRIPT = BASE_DIR / "tools" / "cluster_snapshot.py"
# Google Drive delivery -- connecting the account is a one-time step done
# directly on the host (like the SSH keypair), not through Telegram; this is
# just what runs once it already is connected. drive.file OAuth scope means
# the connected account only ever exposes files rclone itself created.
RCLONE_BIN = os.environ.get("RCLONE_BIN", "rclone")
GDRIVE_ROOT = os.environ.get("GDRIVE_ROOT", "iSmart-LA Data")
RCLONE_CONF = Path.home() / ".config" / "rclone" / "rclone.conf"
# Which connected Drive account each chat uploads to -- {chat_id: remote}.
# Deliberately no fallback default: a room must explicitly pick one via
# /gdrive before anything uploads, so a file never lands in an account
# nobody meant to use for that room.
GDRIVE_ROOM_ACCOUNTS_FILE = BASE_DIR / "gdrive_room_accounts.json"
# The OAuth client the Drive device-flow sign-in speaks to. Deliberately NOT
# shipped in the repo and never committed: it belongs to the operator's own
# Google Cloud project, the same way the bot token does. One client serves the
# whole deployment -- each person still authorises their OWN Google account
# through it.
GDRIVE_CLIENT_FILE = BASE_DIR / "gdrive_oauth_client.json"
GDRIVE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Verified against Google's own documentation before being chosen: the device
# flow supports a LIMITED scope list, and of the Drive scopes only
# drive.appdata and drive.file are on it -- full "drive" is not. drive.file is
# also classed non-sensitive, so publishing the client needs no Google review.
# It is what connect_gdrive_account() already used anyway, so nothing about
# what the bot can reach changes here: only how the token is obtained.
GDRIVE_DEVICE_SCOPE = "https://www.googleapis.com/auth/drive.file"

# --------------------------------------------------------------------------
# Per-chat language for the bot's OWN fixed text (command replies) -- separate
# from /help's own EN/ID choice (picked per-message) and separate from the
# agent's actual answers, which already mirror whatever language the prompt
# was written in on their own, being an LLM. This only covers the handful of
# commands whose text is hardcoded Python, not model output.
# --------------------------------------------------------------------------
LANGUAGE_FILE = BASE_DIR / "chat_language.json"  # {chat_id: "en"|"id"}
DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "id")


def _read_chat_languages() -> dict:
    if not LANGUAGE_FILE.exists():
        return {}
    try:
        return json.loads(LANGUAGE_FILE.read_text())
    except Exception:
        logger.warning("chat_language.json unreadable", exc_info=True)
        return {}


def _write_chat_languages(items: dict) -> None:
    LANGUAGE_FILE.write_text(json.dumps(items, indent=2))


def _chat_lang(update: Update) -> str:
    """This chat's language for command replies. Falls back to
    DEFAULT_LANGUAGE until set via /lang or /start's first-run prompt."""
    chat = update.effective_chat
    if chat is None:
        return DEFAULT_LANGUAGE
    return _read_chat_languages().get(str(chat.id), DEFAULT_LANGUAGE)


def _t(lang: str, en: str, id_: str) -> str:
    return id_ if lang == "id" else en


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set (or show) this chat's language for the bot's own fixed replies.
    The agent's actual answers already mirror whatever language you write in
    -- this only covers commands like /usemodel, /gdrive, /mode. /help has its
    own separate EN/ID choice, picked per-message, untouched by this."""
    if not await _may_authorize_group_action(update, context):
        return
    chat_id = str(update.effective_chat.id)
    prefs = _read_chat_languages()
    current = prefs.get(chat_id, DEFAULT_LANGUAGE)
    if not context.args:
        await update.message.reply_text(_t(current,
            f"\U0001f310 This chat's command language: <b>{current.upper()}</b>\n\n"
            "<code>/lang en</code> or <code>/lang id</code> to change.",
            f"\U0001f310 Bahasa command chat ini: <b>{current.upper()}</b>\n\n"
            "<code>/lang en</code> atau <code>/lang id</code> untuk ganti.",
        ), parse_mode="HTML")
        return
    choice = context.args[0].lower()
    if choice not in ("en", "id"):
        await update.message.reply_text(_t(current, "Usage: /lang en|id", "Pakai: /lang en|id"))
        return
    prefs[chat_id] = choice
    _write_chat_languages(prefs)
    await update.message.reply_text(_t(choice,
        "\U0001f310 This chat now replies to commands in English.",
        "\U0001f310 Chat ini sekarang balas command pakai Bahasa Indonesia.",
    ))
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
4. Register it in `tools/registry.json` -- create the file if it does not exist yet (see `tools/registry.json.example` for the shape), then add one entry to the "tools" array with name, script, usage, description, and "answers" (a list of question phrasings it covers).
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
# about dodging per-token spend. Claude is the fallback. Two
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
# Ceiling when /unlock is opened from a group rather than a DM -- open-ended
# write access for the whole window, not one pre-approved action, so it gets
# a shorter leash than the 60-minute DM ceiling regardless of what is asked for.
WRITE_MODE_GROUP_MAX_MINUTES = int(os.environ.get("WRITE_MODE_GROUP_MAX_MINUTES", "10"))


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


def unlock_write_mode(minutes: int, max_minutes: Optional[int] = None) -> float:
    minutes = max(1, min(minutes, max_minutes or WRITE_MODE_MAX_MINUTES))
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
        "right now -- investigate, audit and report freely, but do not attempt a change; it "
        "would just be refused. If the user is asking for a change, do NOT tell them to run "
        "/unlock themselves -- that skips the button flow entirely. Instead: describe what "
        "you found and what change is needed, then add a line by itself: NEEDS_WRITE: <the "
        "action and the machine, one short phrase>. The user gets a button to open write "
        "access (with an automatic snapshot first, for a VM), and your request continues "
        "automatically once they do -- you do not need them to type anything else. Do not "
        "try to work around the lock, and do not attempt to modify SSH keys or this bot's "
        "own files.]"
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
# so a group only ever sees "somebody is entering a PIN", never the PIN. Kept
# in step with each action's OWN entry gate (_may_run_setup /
# _may_authorize_group_action) -- if that gate already trusts a registered
# group's admin (or the owner, from anywhere) to REACH the PIN prompt for an
# action, excluding it here only dead-ends someone already vetted moments
# earlier, with no security gained: they saw the prompt, they know the action
# exists, and the digits are still hidden in callback_data either way. A
# FUTURE action with a narrower gate (owner-only, full stop) belongs left out
# of this set until that is deliberately decided -- being unlisted here falls
# back to owner-AND-private-DM, the strict default (see cmd_pin_key).
PIN_ACTIONS_ALLOWED_IN_GROUP = frozenset({
    "new_pin_capture", "new_pin_confirm", "change_pin_start", "schedule_install",
    "unlock", "unlock_and_resume", "addserver", "update", "rmboundary", "addmcp",
    "new_group_pin_capture", "new_group_pin_confirm", "change_group_pin_start",
})
_pin_lockout_until: float = 0.0


# --------------------------------------------------------------------------
# PIN storage: one owner PIN (works everywhere -- DM, and every group, as a
# master credential), plus an optional PIN per registered group that only
# applies inside that specific group. A group with none of its own falls back
# to the owner's, exactly like before this existed -- nothing regresses for a
# group that hasn't set one up.
#
# pin.json holds {"owner": {salt,hash}|None, "groups": {chat_id: {salt,hash,
# set_by,set_at}}}. A file from before this feature existed is the flat
# {"salt":...,"hash":...} shape -- read transparently as the owner's PIN, no
# migration step required.
# --------------------------------------------------------------------------

def _pin_store() -> dict:
    if not PIN_FILE.exists():
        return {"owner": None, "groups": {}}
    try:
        data = json.loads(PIN_FILE.read_text())
    except Exception:
        logger.error("pin.json unreadable -- treating as empty", exc_info=True)
        return {"owner": None, "groups": {}}
    if "owner" in data or "groups" in data:
        data.setdefault("owner", None)
        data.setdefault("groups", {})
        return data
    return {"owner": data, "groups": {}}  # pre-group-PIN flat format


def _save_pin_store(store: dict) -> None:
    PIN_FILE.write_text(json.dumps(store, indent=2))
    try:
        PIN_FILE.chmod(0o600)
    except OSError:
        pass


def _hash_pin(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)


def pin_is_set(chat_id: Optional[int] = None) -> bool:
    """Whether a PIN exists that would gate an action from this chat: that
    group's own, if it has one, or (always) the owner's."""
    store = _pin_store()
    if chat_id is not None and str(chat_id) in store["groups"]:
        return True
    return store["owner"] is not None


def group_pin_is_set(chat_id: int) -> bool:
    return str(chat_id) in _pin_store()["groups"]


def set_pin(pin: str) -> None:
    """The owner's PIN -- works everywhere, as the master credential."""
    store = _pin_store()
    salt = os.urandom(16)
    store["owner"] = {"salt": salt.hex(), "hash": _hash_pin(pin, salt).hex()}
    _save_pin_store(store)
    logger.warning("owner PIN was set/changed")


def set_group_pin(chat_id: int, pin: str, by: int) -> None:
    """A specific group's own PIN -- applies only inside that group, on top
    of (never instead of) the owner's, which still works there too."""
    store = _pin_store()
    salt = os.urandom(16)
    store["groups"][str(chat_id)] = {
        "salt": salt.hex(), "hash": _hash_pin(pin, salt).hex(),
        "set_by": by, "set_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _save_pin_store(store)
    logger.warning("group PIN set/changed for chat=%s by=%s", chat_id, by)


def remove_group_pin(chat_id: int) -> bool:
    """Drop a group's own PIN. That group falls back to the owner's -- it is
    never left with no PIN at all as long as the owner has one."""
    store = _pin_store()
    if str(chat_id) not in store["groups"]:
        return False
    del store["groups"][str(chat_id)]
    _save_pin_store(store)
    logger.warning("group PIN removed for chat=%s", chat_id)
    return True


def verify_pin(pin: str, chat_id: Optional[int] = None) -> bool:
    """True if `pin` matches whatever legitimately applies here: that group's
    own PIN if this chat has one, OR (always, everywhere) the owner's -- a
    group with none of its own simply has only the owner's to match, same as
    before per-group PINs existed."""
    store = _pin_store()
    candidates = []
    if chat_id is not None:
        entry = store["groups"].get(str(chat_id))
        if entry is not None:
            candidates.append(entry)
    if store["owner"] is not None:
        candidates.append(store["owner"])
    if not candidates:
        return False
    # Constant-time per candidate: a length/short-circuit difference is a
    # timing oracle, and six digits is a small enough space that it matters.
    # Checked against every candidate rather than short-circuiting the loop
    # on the exception path so the two-PIN case doesn't leak which slot (if
    # either) is even configured via a timing difference of its own.
    ok = False
    for entry in candidates:
        try:
            salt = bytes.fromhex(entry["salt"])
            expected = bytes.fromhex(entry["hash"])
        except Exception:
            logger.error("pin.json entry unreadable -- refusing to verify", exc_info=True)
            continue
        digest = _hash_pin(pin, salt)
        ok = ok or hmac.compare_digest(digest, expected)
    return ok


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
MODEL_OVERRIDE_FILE = BASE_DIR / "model_overrides.json"  # {chat_id: model}
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


def _read_model_overrides() -> dict:
    if not MODEL_OVERRIDE_FILE.exists():
        return {}
    try:
        return json.loads(MODEL_OVERRIDE_FILE.read_text())
    except Exception:
        logger.warning("model_overrides.json unreadable", exc_info=True)
        return {}


def _write_model_overrides(items: dict) -> None:
    MODEL_OVERRIDE_FILE.write_text(json.dumps(items, indent=2))


def _find_tier_by_alias(text: str) -> Optional[dict]:
    """Match free-typed text against a known tier's label or model id,
    case-insensitively -- spans both the default chain and the extras."""
    needle = text.strip().lower()
    if not needle:
        return None
    for t in ALL_TIERS:
        if needle in (t["label"].lower(), t["model"].lower()):
            return t
    return None


def _valid_cron(expr: str) -> bool:
    fields = expr.split()
    return len(fields) == 5 and all(_CRON_FIELD_RE.match(f) for f in fields)


def _current_crontab() -> str:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def _rebuild_crontab(items: list[dict], strip_lines: set[str] | None = None) -> None:
    """Regenerate ONLY our managed block, leaving any other crontab entry the
    operator has alone -- clobbering somebody's unrelated cron job would be a
    far worse bug than anything this feature is meant to fix."""
    existing = _current_crontab()
    if CRON_BEGIN in existing and CRON_END in existing:
        head = existing.split(CRON_BEGIN)[0].rstrip("\n")
        tail = existing.split(CRON_END, 1)[1].lstrip("\n")
    else:
        head, tail = existing.rstrip("\n"), ""

    if strip_lines:
        # A line being adopted INTO management must not also survive outside
        # it -- otherwise it runs twice, once raw and once managed.
        head = "\n".join(ln for ln in head.split("\n") if ln.strip() not in strip_lines)
        tail = "\n".join(ln for ln in tail.split("\n") if ln.strip() not in strip_lines)

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

# --------------------------------------------------------------------------
# Server inventory (/addserver, /servers, /removeserver)
#
# Adding a machine used to mean SSH-ing to this box and hand-editing ~/.ssh/config
# plus the brief. Now it is a conversation, because that is where the operator
# already is.
#
# NO PRIVATE KEY EVER PASSES THROUGH TELEGRAM. The bot generates the keypair here
# and shows only the PUBLIC half, which you paste into the target's
# authorized_keys. A private key sent as a chat message would be a permanent
# credential to production sitting in cleartext on servers we don't control --
# strictly worse than the password we already decided against, because a leaked
# key opens every node until someone notices and revokes it.
#
# It also means fewer things to type: host, user, port. Nothing secret.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Hard boundaries, editable from Telegram
#
# These were only settable at install time by bootstrap.py, which meant the most
# safety-critical setting in the system was also the one you could not revisit
# without editing a file over SSH. Anything that hard to change stops matching
# reality, and a boundary list that no longer matches reality is worse than none
# -- it reads as protection that isn't there.
#
# They live in the PROTECTED zone of both briefs. The agent still cannot write
# there: the bot edits on the human's behalf, exactly as bootstrap.py does.
# --------------------------------------------------------------------------

BOUNDARY_HEADER = "## HARD BOUNDARIES"


def read_boundaries() -> list[str]:
    for path in _brief_files():
        protected, _ = _split_zones(path.read_text())
        if BOUNDARY_HEADER not in protected:
            continue
        section = protected.split(BOUNDARY_HEADER, 1)[1]
        # up to the next heading
        section = re.split(r"\n## ", section, maxsplit=1)[0]
        items = [ln.strip()[2:].strip() for ln in section.split("\n")
                 if ln.strip().startswith("- ")]
        if items:
            return items
    return []


def write_boundaries(items: list[str]) -> None:
    """Rewrite ONLY the boundary bullet list, in both briefs. Everything else in
    the protected zone -- persona, access notes, report rules -- is untouched."""
    body = "\n".join(f"- {i}" for i in items) if items else \
        "- (none recorded yet -- add them with /boundaries, they are what stops " \
        "a mistake from becoming an incident)"
    for path in _brief_files():
        text = path.read_text()
        protected, learned = _split_zones(text)
        if BOUNDARY_HEADER not in protected:
            protected = protected.rstrip() + (
                f"\n\n{BOUNDARY_HEADER} -- never do these without explicit human "
                f"confirmation in the moment, even if asked generically:\n{body}\n")
        else:
            head, _, rest = protected.partition(BOUNDARY_HEADER)
            first_line, _, after = rest.partition("\n")
            tail = ""
            if "\n## " in after:
                tail = "\n## " + after.split("\n## ", 1)[1]
            protected = f"{head}{BOUNDARY_HEADER}{first_line}\n{body}\n{tail}"
        rebuilt = protected.rstrip() + "\n"
        if learned or LEARNED_ZONE_MARKER in text:
            rebuilt += (f"\n{LEARNED_ZONE_MARKER}\n## Learned about this environment\n\n"
                        "Facts the agent discovered and recorded itself. Safe to edit or "
                        "delete by hand -- nothing above the marker line is ever touched "
                        f"automatically.\n\n{learned}\n")
        path.write_text(rebuilt)
    logger.warning("hard boundaries rewritten: %d entr(ies)", len(items))


# --------------------------------------------------------------------------
# VM snapshots taken before a change
#
# Registered here so they can be listed and cleaned up later, rather than
# accumulating invisibly on the cluster until someone notices storage is full.
#
# HONEST LIMIT: this is a convention the agent is told to follow, not a lock.
# Every command it runs on a node goes out as `ssh <node> "qm ..."`, and this
# process never sees those individually -- so "always snapshot first" is an
# instruction in the brief plus a tool that makes doing it easy, not something
# the code can force. It is still worth having: the right thing becomes the
# easy thing, and every snapshot taken is visible in /snapshots afterwards.
# --------------------------------------------------------------------------

SNAPSHOTS_FILE = BASE_DIR / "snapshots.json"


def read_snapshots() -> list[dict]:
    if not SNAPSHOTS_FILE.exists():
        return []
    try:
        return json.loads(SNAPSHOTS_FILE.read_text())
    except Exception:
        logger.warning("snapshots.json unreadable", exc_info=True)
        return []


def register_snapshot(entry: dict) -> None:
    items = read_snapshots()
    items.append({**entry, "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
    SNAPSHOTS_FILE.write_text(json.dumps(items[-200:], indent=2))
    logger.warning("SNAPSHOT registered: vm=%s name=%s", entry.get("vmid"), entry.get("snapname"))


SNAPSHOT_LINE_RE = re.compile(r"^\s*SNAPSHOT:\s*(.+?)\s*$", re.MULTILINE)


def extract_snapshots(text: str) -> tuple[str, list[dict]]:
    """SNAPSHOT: vmid=105 | node=pm4 | name=pre-change-1230 | reason=resize RAM"""
    out = []
    for raw in SNAPSHOT_LINE_RE.findall(text):
        fields = {}
        for chunk in raw.split("|"):
            k, _, v = chunk.partition("=")
            fields[k.strip().lower()] = v.strip()
        if fields.get("vmid") and fields.get("name"):
            out.append({"vmid": fields["vmid"], "node": fields.get("node", "?"),
                        "snapname": fields["name"], "reason": fields.get("reason", "")})
    return SNAPSHOT_LINE_RE.sub("", text).strip(), out


GDRIVE_LINE_RE = re.compile(r"^\s*GDRIVE:\s*(.+?)\s*$", re.MULTILINE)


def extract_gdrive(text: str) -> tuple[str, list[dict]]:
    """GDRIVE: file=/tmp/report.md | to=ikan/A/report.md -- destination is
    always relative to GDRIVE_ROOT, filename included."""
    out = []
    for raw in GDRIVE_LINE_RE.findall(text):
        fields = {}
        for chunk in raw.split("|"):
            k, _, v = chunk.partition("=")
            fields[k.strip().lower()] = v.strip()
        if fields.get("file") and fields.get("to"):
            out.append({"file": fields["file"], "to": fields["to"]})
    return GDRIVE_LINE_RE.sub("", text).strip(), out


SERVERS_FILE = BASE_DIR / "servers.json"
SSH_CONFIG_FILE = Path.home() / ".ssh" / "config"
SSH_CONFIG_BEGIN = "# BEGIN iSmart-LA managed -- edited by the bot, do not hand-edit"
SSH_CONFIG_END = "# END iSmart-LA managed"
SERVER_WIZARD_TTL = 900
_server_wizard: dict[int, dict] = {}

SERVER_KINDS = {
    "hypervisor": "Hypervisor / cluster",
    "vm": "Single VM or server",
    "other": "Something else",
}
HYPERVISOR_FLAVOURS = {
    "proxmox": "Proxmox VE",
    "other_hv": "Other hypervisor",
}
_HOST_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


def _read_servers() -> list[dict]:
    if not SERVERS_FILE.exists():
        return []
    try:
        return json.loads(SERVERS_FILE.read_text())
    except Exception:
        logger.warning("servers.json unreadable", exc_info=True)
        return []


def _write_servers(items: list[dict]) -> None:
    SERVERS_FILE.write_text(json.dumps(items, indent=2))


def agent_keypair() -> tuple[Path, Path]:
    """The key the agent presents to managed hosts.

    Reuses the read-only key from write mode when that is set up, so a host
    added here is reachable under the same lock/unlock rules as everything else
    rather than quietly getting its own always-on access.
    """
    if SSH_RO_KEY.exists():
        return SSH_RO_KEY, SSH_RO_KEY.with_suffix(".pub")
    key = Path.home() / ".ssh" / "ismart_agent"
    if not key.exists():
        key.parent.mkdir(mode=0o700, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", "ismart-la-agent"],
            capture_output=True, text=True, check=True,
        )
        logger.warning("generated a new agent SSH keypair at %s", key)
    return key, key.with_suffix(".pub")


def _rebuild_ssh_config(items: list[dict]) -> None:
    """Regenerate ONLY our managed block. Anything else in ~/.ssh/config is the
    operator's and is copied through untouched."""
    existing = SSH_CONFIG_FILE.read_text() if SSH_CONFIG_FILE.exists() else ""
    if SSH_CONFIG_BEGIN in existing and SSH_CONFIG_END in existing:
        head = existing.split(SSH_CONFIG_BEGIN)[0].rstrip("\n")
        tail = existing.split(SSH_CONFIG_END, 1)[1].lstrip("\n")
    else:
        head, tail = existing.rstrip("\n"), ""

    default_key = SSH_ACTIVE_KEY if SSH_ACTIVE_KEY.exists() else agent_keypair()[0]
    block = [SSH_CONFIG_BEGIN]
    for it in items:
        # A per-server key wins, so what was tested is what gets used. Without
        # its own key a host follows agent_active, and therefore lock/unlock.
        key = it.get("key") or default_key
        for host in [it["host"], *it.get("cluster_hosts", [])]:
            block += [
                f"Host {host}",
                f"    User {it['user']}",
                f"    Port {it.get('port', 22)}",
                f"    IdentityFile {key}",
                "    StrictHostKeyChecking no",
                "    ConnectTimeout 10",
            ]
    block.append(SSH_CONFIG_END)

    SSH_CONFIG_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    SSH_CONFIG_FILE.write_text(
        "\n".join(x for x in (head, "\n".join(block), tail) if x).rstrip("\n") + "\n"
    )
    SSH_CONFIG_FILE.chmod(0o600)


def test_server_ssh(host: str, user: str, port: int, timeout: int = 20,
                    key_path: Optional[str] = None) -> tuple[bool, str]:
    key = Path(key_path) if key_path else agent_keypair()[0]
    proc = subprocess.run(
        ["ssh", "-i", str(key), "-p", str(port),
         "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
         "-o", f"ConnectTimeout={timeout}", f"{user}@{host}",
         "echo ISMART_OK && uname -sr"],
        capture_output=True, text=True, timeout=timeout + 10,
    )
    if "ISMART_OK" in proc.stdout:
        detail = proc.stdout.replace("ISMART_OK", "").strip()
        return True, detail or "connected"
    return False, (proc.stderr or proc.stdout or "no response").strip()[-400:]


def discover_proxmox(host: str, user: str, port: int) -> tuple[bool, str, list[str]]:
    """Ask a Proxmox node what else is in its cluster, and how many guests.

    Read-only (`pvesh get`), so it is safe to run without unlocking write mode.
    """
    key, _ = agent_keypair()

    def run(cmd: str) -> str:
        return subprocess.run(
            ["ssh", "-i", str(key), "-p", str(port), "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=90,
        ).stdout

    try:
        # /cluster/status, not /nodes: it carries each node's real IP. Node
        # NAMES are not safe to use as SSH hosts -- a wildcard DNS record can
        # point them at a proxy, and then you are talking to the wrong machine
        # without any error to tell you so.
        status_raw = run("pvesh get /cluster/status --output-format json 2>/dev/null")
        status = json.loads(status_raw) if status_raw.strip().startswith("[") else []
        nodes = [n for n in status if n.get("type") == "node"]
        guests_raw = run("pvesh get /cluster/resources --type vm --output-format json 2>/dev/null")
        guests = json.loads(guests_raw) if guests_raw.strip().startswith("[") else []
    except Exception as exc:
        return False, f"discovery failed: {exc}", []

    if not nodes:
        return False, "no Proxmox nodes reported -- is pvesh available for this user?", []

    names = [n.get("name", "?") for n in nodes]
    # Addresses, not names -- these are what end up in ~/.ssh/config.
    addrs = [n["ip"] for n in nodes if n.get("ip")]
    running = sum(1 for g in guests if g.get("status") == "running")
    lines = [f"Found {len(nodes)} node(s)",
             f"Guests: {len(guests)} total, {running} running"]
    for n in nodes:
        lines.append(f"  • {n.get('name')} — {n.get('ip', 'no ip reported')}")
    if len(addrs) < len(nodes):
        lines.append("\n⚠️ Some nodes reported no IP and were left out.")
    return True, "\n".join(lines), addrs


# --------------------------------------------------------------------------
# "The agent hit the read-only wall" -> offer to unlock, snapshot, and resume
#
# Guessing from the wording of a request whether it needs write access does not
# work. "check why vm150 is throwing 503" is read-only right up until the answer
# turns out to be "restart nginx", and no keyword list separates those. Asking a
# model to classify first would add cost and latency to every message and still
# be wrong sometimes, in both directions.
#
# So nothing is guessed. The node-side guard already decides read-vs-write
# exactly, because it is the thing doing the refusing. We just notice that it
# refused, and offer the next step.
#
# Detection is two-layered on purpose:
#   1. the guard's own refusal text, which appears in the agent's tool output
#      and cannot be suppressed by the model
#   2. a NEEDS_WRITE: line the agent is asked to emit, which gives a clean
#      description and the VM id
# Layer 1 is the one that cannot be dodged; layer 2 makes the message readable.
# --------------------------------------------------------------------------

_POWER_ONLY_RE = re.compile(
    r"^\s*(?:please\s+|tolong\s+)?(?:re)?(boot|start|stop|restart|shutdown|"
    r"power[\s-]?(?:on|off|cycle)?)\b", re.IGNORECASE)
_MODIFIES_STATE_RE = re.compile(
    r"resize|expand|shrink|disk|storage|memory|\bram\b|\bcpu\b|config|"
    r"install|upgrade|update|migrate|\bedit\b|modify|repair|\bfix\b|"
    r"delete|remove|destroy|attach|detach|network|firewall|password|rename",
    re.IGNORECASE)


def needs_snapshot_offer(reason: str) -> bool:
    """False only when `reason` clearly names a plain power-cycle and nothing
    that could touch disk state. True (offer it) for everything else,
    including anything ambiguous -- restarting is reversible by definition;
    losing data to a skipped snapshot is not, so ties go to offering it."""
    reason = reason.strip()
    if not reason:
        return True
    if _MODIFIES_STATE_RE.search(reason):
        return True
    return not _POWER_ONLY_RE.match(reason)

NEEDS_WRITE_RE = re.compile(r"^\s*NEEDS_WRITE:\s*(.+?)\s*$", re.MULTILINE)
GUARD_REFUSAL = "refused -- this key is read-only"
_VMID_RE = re.compile(r"\b(?:vm[\s-]?|vmid[\s:=-]*)(\d{2,6})\b", re.I)
# chat_id -> {"prompt", "reason", "vmid", "expires"}
_pending_write: dict[int, dict] = {}
PENDING_WRITE_TTL = 900


def extract_needs_write(text: str) -> tuple[str, Optional[str]]:
    """Pull a NEEDS_WRITE: line out and strip it from what the user sees."""
    hits = NEEDS_WRITE_RE.findall(text)
    return NEEDS_WRITE_RE.sub("", text).strip(), (hits[0] if hits else None)


# --------------------------------------------------------------------------
# Making the write gate REAL on a fresh deployment
#
# The gate above (locked/unlocked, PIN, snapshot offer) is only a boundary if
# two things exist that nothing used to create:
#
#   1. the two keys themselves -- _keys_configured() returns False without
#      both, and a False there makes the ENTIRE gate inert: no approval button
#      is ever offered, and lock_write_mode() has nothing to swap.
#   2. the guard on the node, which is what actually refuses a write. Without
#      it the "read-only" key is ordinary unrestricted root.
#
# Both were documented as manual README steps while /addserver -- the path the
# bot itself tells you to use -- generated ONE unrestricted key and appended it
# plainly. So the default install produced a deployment where the safety UI
# could never fire AND nothing blocked writes: exactly the combination that let
# a "delete this VM" request run with no approval button and no PIN on a live
# cluster.
#
# Everything below exists so that path is automatic, verified, and loud when it
# fails -- never silently absent again.
# --------------------------------------------------------------------------

NODE_GUARD_SCRIPT = BASE_DIR / "node-guard" / "pve-ro-guard"
NODE_GUARD_REMOTE = "/usr/local/bin/pve-ro-guard"
RO_KEY_COMMENT = "ismart-la-readonly"
RW_KEY_COMMENT = "ismart-la-write"
LEGACY_KEY_COMMENT = "ismart-la-agent"
_GUARD_KEY_OPTS = (
    'command="' + NODE_GUARD_REMOTE + '",no-port-forwarding,'
    'no-agent-forwarding,no-X11-forwarding,no-pty'
)


def ensure_write_mode_keys() -> bool:
    """Create the read-only/write keypair and the active symlink if missing.

    Idempotent, and called at startup, so a deployment cloned from GitHub has a
    live write gate without anyone having to find a README section first.
    Returns True if the pair is present afterwards.

    The symlink starts pointed at the READ-ONLY key: locked is the default, and
    /unlock is what moves it, exactly as write_mode_expires_at() assumes.
    """
    try:
        SSH_RO_KEY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for key, comment in ((SSH_RO_KEY, RO_KEY_COMMENT), (SSH_RW_KEY, RW_KEY_COMMENT)):
            if not key.exists():
                subprocess.run(
                    ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", comment],
                    capture_output=True, text=True, check=True,
                )
                logger.warning("generated %s", key)
        if not SSH_ACTIVE_KEY.exists() and not SSH_ACTIVE_KEY.is_symlink():
            _point_active_key_at(SSH_RO_KEY)
            logger.warning("active key symlink created, pointing at the read-only key")
        return _keys_configured()
    except Exception:
        logger.exception("could not set up the write-mode keypair")
        return False


def _ssh_as(key: Path, host: str, user: str, port: int, cmd: str,
            timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", str(key), "-p", str(port),
         "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=15", f"{user}@{host}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _admin_key_for(host: str, user: str, port: int) -> Optional[Path]:
    """A key that can currently WRITE on this host, or None.

    Prefers the proper write key. Falls back to the single unrestricted key
    older deployments were given by /addserver, which is what makes migrating
    an existing, unprotected host automatic instead of a manual re-setup --
    the whole point of this being permanent rather than a one-off tweak.
    """
    legacy = Path.home() / ".ssh" / "ismart_agent"
    for key in (SSH_RW_KEY, legacy):
        if not key.exists():
            continue
        try:
            if _ssh_as(key, host, user, port, "echo ISMART_ADMIN_OK", timeout=25).stdout.count(
                    "ISMART_ADMIN_OK"):
                return key
        except Exception:
            continue
    return None


def install_node_guard(host: str, user: str, port: int) -> tuple[bool, str]:
    """Put the guard on the node and authorise the read-only key behind it.

    Runs over the WRITE key -- the one the operator authorised by hand -- and
    the bot then installs its own restricted key, rather than asking a human to
    paste a 7KB script into a terminal (which is why this was a README step
    nobody performed).

    Idempotent: re-running refreshes the guard script, so a fixed guard can be
    rolled out, but never duplicates an authorized_keys line.
    """
    if not NODE_GUARD_SCRIPT.exists():
        return False, f"guard script missing from this deployment: {NODE_GUARD_SCRIPT}"
    ro_pub_path = SSH_RO_KEY.with_suffix(".pub")
    if not ro_pub_path.exists():
        return False, "no read-only public key to install"
    admin_key = _admin_key_for(host, user, port)
    if admin_key is None:
        return False, ("no key can reach this host with write access yet -- authorise "
                       "the write key first")
    guard_src = NODE_GUARD_SCRIPT.read_text()
    ro_pub = ro_pub_path.read_text().strip()
    rw_pub = SSH_RW_KEY.with_suffix(".pub").read_text().strip() \
        if SSH_RW_KEY.with_suffix(".pub").exists() else ""
    ro_line = _GUARD_KEY_OPTS + " " + ro_pub
    # Match on the key material itself, not the comment: comments are editable
    # by anyone with the file open, the base64 blob is what actually grants.
    fingerprint_bit = ro_pub.split()[1][:40]

    script = "\n".join([
        "set -e",
        "umask 077",
        "mkdir -p ~/.ssh",
        "touch ~/.ssh/authorized_keys",
        # Quoted heredoc: nothing inside the guard is expanded by the remote
        # shell, and it only moves into place once it has landed complete.
        "cat > /tmp/.pve-ro-guard.new <<'ISMART_GUARD_EOF'",
        guard_src,
        "ISMART_GUARD_EOF",
        f"install -m 755 /tmp/.pve-ro-guard.new {NODE_GUARD_REMOTE}",
        "rm -f /tmp/.pve-ro-guard.new",
        f"grep -qF '{fingerprint_bit}' ~/.ssh/authorized_keys || "
        f"printf '%s\\n' '{ro_line}' >> ~/.ssh/authorized_keys",
        # Bootstrapping off the legacy key? Authorise the write key too, so the
        # next run has a proper admin key and the legacy one can be retired.
        (f"grep -qF '{rw_pub.split()[1][:40]}' ~/.ssh/authorized_keys || "
         f"printf '%s\\n' '{rw_pub}' >> ~/.ssh/authorized_keys") if rw_pub else "true",
        "chmod 600 ~/.ssh/authorized_keys",
        "echo ISMART_GUARD_INSTALLED",
    ])
    try:
        proc = _ssh_as(admin_key, host, user, port, script, timeout=60)
    except Exception as exc:
        return False, str(exc)
    if "ISMART_GUARD_INSTALLED" in proc.stdout:
        return True, "guard installed"
    return False, (proc.stderr or proc.stdout or "no response").strip()[-400:]


def verify_node_guard(host: str, user: str, port: int) -> tuple[bool, str]:
    """Prove the guard actually refuses a write -- never take it on trust.

    This is the entire point of the feature. The incident that prompted it
    looked fine from every angle except the one nobody checked: a write,
    attempted with the key that is supposed to be read-only, SUCCEEDING. So
    that exact thing is what gets tested here, with a harmless touch/rm rather
    than anything that matters.
    """
    try:
        read = _ssh_as(SSH_RO_KEY, host, user, port, "hostname")
        if read.returncode != 0:
            detail = (read.stderr or read.stdout or "no response").strip()[-200:]
            return False, "read-only key cannot even read: " + detail
        probe = "/tmp/.ismart_guard_probe"
        write = _ssh_as(
            SSH_RO_KEY, host, user, port,
            f"touch {probe} && rm -f {probe} && echo WRITE_WENT_THROUGH")
    except Exception as exc:
        return False, str(exc)
    if "WRITE_WENT_THROUGH" in write.stdout:
        return False, ("the read-only key can still WRITE -- the guard is not in "
                       "force. This node is unprotected.")
    if GUARD_REFUSAL in (write.stderr or "") or GUARD_REFUSAL in (write.stdout or ""):
        return True, "guard verified: reads work, writes refused"
    # Refused, but not recognisably by our guard. Say exactly that rather than
    # claim a boundary that was not actually observed.
    detail = (write.stderr or write.stdout or "").strip()[-200:]
    return True, "write refused (not by pve-ro-guard): " + detail


def secure_server(host: str, user: str, port: int) -> tuple[bool, str]:
    """Install the guard and prove it refuses a write. Nothing is retired here.

    Retiring the old unrestricted key is deliberately NOT part of this: until
    ~/.ssh/config points at the active-key symlink, that legacy key is still
    the credential the agent actually uses, and deleting it from a node would
    lock the agent out of the very host it just secured. cmd_secure() does the
    retirement pass afterwards, once the config has been flipped.
    """
    ok, detail = install_node_guard(host, user, port)
    if not ok:
        return False, "install failed: " + detail
    ok, detail = verify_node_guard(host, user, port)
    if not ok:
        return False, "verification failed: " + detail
    return True, detail


def retire_legacy_key(host: str, user: str, port: int) -> bool:
    """Drop the pre-guard unrestricted key from a host's authorized_keys.

    Only safe once BOTH are true: the replacement keys verify on that host,
    and ~/.ssh/config here points at the active-key symlink rather than the
    legacy key. cmd_secure() enforces that order.
    """
    try:
        cleanup = _ssh_as(
            SSH_RW_KEY, host, user, port,
            f"if grep -q '{LEGACY_KEY_COMMENT}' ~/.ssh/authorized_keys 2>/dev/null; then "
            f"sed -i '/{LEGACY_KEY_COMMENT}/d' ~/.ssh/authorized_keys && "
            "echo LEGACY_KEY_REMOVED; fi")
        if "LEGACY_KEY_REMOVED" in cleanup.stdout:
            logger.warning("retired the legacy unrestricted agent key on %s", host)
            return True
    except Exception:
        logger.warning("could not retire the legacy key on %s", host, exc_info=True)
    return False


def blocked_by_readonly(result_text: str, attempts: list[str]) -> bool:
    return GUARD_REFUSAL in result_text or any(GUARD_REFUSAL in a for a in attempts)


def guess_vmid(*texts: str) -> Optional[str]:
    """Best-effort VM id, for pre-filling the snapshot offer. Only ever used to
    suggest -- never to act on without the operator seeing it."""
    for t in texts:
        if not t:
            continue
        m = _VMID_RE.search(t)
        if m:
            return m.group(1)
    return None


def find_vm_node(vmid: str) -> Optional[str]:
    """Which node hosts this VM. A read, so it works while still locked."""
    for srv in _read_servers():
        hosts = [srv["host"], *srv.get("cluster_hosts", [])]
        for host in hosts[:1]:
            try:
                out = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
                     "pvesh get /cluster/resources --type vm --output-format json 2>/dev/null"],
                    capture_output=True, text=True, timeout=45).stdout
                for v in json.loads(out):
                    if str(v.get("vmid")) == str(vmid):
                        return v.get("node")
            except Exception:
                logger.debug("vm node lookup failed via %s", host, exc_info=True)
    return None


def take_snapshot(vmid: str, node: str, reason: str) -> tuple[bool, str]:
    """Snapshot a VM, using the WRITE key directly.

    This is the bot acting, not the agent -- which is the whole point. The agent
    cannot be relied on to snapshot before changing something, because this
    process never sees its individual commands. Here we do it ourselves, before
    handing over write access at all, so by the time the agent can change
    anything the rollback point already exists.
    """
    name = f"ismart-{_dt.datetime.now():%m%d-%H%M}"
    desc = f"iSmart-LA before: {reason[:120]}"
    for host in _snapshot_hosts(node):
        try:
            proc = subprocess.run(
                ["ssh", "-i", str(SSH_RW_KEY), "-o", "BatchMode=yes",
                 "-o", "ConnectTimeout=15", host,
                 f"qm snapshot {int(vmid)} {name} --description {json.dumps(desc)}"],
                capture_output=True, text=True, timeout=180)
        except Exception as exc:
            logger.exception("snapshot call failed")
            return False, str(exc)[:300]
        err = (proc.stderr or "").strip()
        if proc.returncode == 0:
            register_snapshot({"vmid": vmid, "node": node or host,
                               "snapname": name, "reason": reason[:200]})
            return True, name
        logger.warning("snapshot on %s failed: %s", host, err[-200:])
    return False, err[-300:] if err else "no reachable node"


def _snapshot_hosts(node: Optional[str]) -> list[str]:
    """Nodes to try. qm only works on the node actually hosting the VM, so the
    named one goes first and the rest are a fallback for a stale lookup."""
    hosts: list[str] = []
    for srv in _read_servers():
        hosts += [srv["host"], *srv.get("cluster_hosts", [])]
    if node:
        hosts = [h for h in hosts if h == node] + [h for h in hosts if h != node]
    return hosts or ([node] if node else [])


# The shipped templates carry these until somebody says what this deployment
# actually looks after. Their presence is what /start reports as "not set up
# yet" -- no extra state file needed, the brief itself is the source of truth.
BRIEF_PLACEHOLDER = "[YOUR ORGANIZATION / PROJECT NAME]"
# Matches the role wherever it already sits, so the brief can be CHANGED and not
# just filled in once -- the setup card offers "Change" on a completed item, and
# a button that silently does nothing is worse than no button.
_BRIEF_ROLE_RE = re.compile(r"^(.*?assistant for )(.+?)(\.)", re.MULTILINE)
_BRIEF_ENV_RE = re.compile(r"^## Environment:.*$", re.MULTILINE)


def brief_configured() -> bool:
    """Has anyone said what this agent is looking after yet?"""
    for path in (SYSTEM_PROMPT_FILE, GEMINI_PROMPT_FILE):
        try:
            if BRIEF_PLACEHOLDER in path.read_text(encoding="utf-8"):
                return False
        except OSError:
            return False
    return True


def set_brief_role(role: str) -> None:
    """Set (or change) what this deployment looks after, in both briefs.

    Rewrites two things only: the role in the opening sentence and the
    "## Environment:" heading. Everything else in the protected zone -- the
    hard boundaries above all -- is passed through byte for byte, the same rule
    append_learned() follows for the half it does not own.
    """
    role = " ".join(role.split()).rstrip(".")
    for path in (SYSTEM_PROMPT_FILE, GEMINI_PROMPT_FILE):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if BRIEF_PLACEHOLDER in text:
            text = text.replace(BRIEF_PLACEHOLDER, role)
        else:
            text, n = _BRIEF_ROLE_RE.subn(
                lambda m: f"{m.group(1)}{role}{m.group(3)}", text, count=1)
            if not n:
                logger.warning(
                    "%s has no recognisable role sentence -- only the Environment "
                    "heading was updated", path.name)
        text = _BRIEF_ENV_RE.sub(f"## Environment: {role}", text, count=1)
        path.write_text(text, encoding="utf-8")
    logger.warning("environment brief role set: %s", role)


# Separate from BRIEF_PLACEHOLDER/set_brief_role above: that controls WHAT this
# deployment looks after ("...assistant for <role>"); this controls what KIND
# of assistant it is at all -- the "a(n) <scope>" between "You are" and
# "assistant". Anchored on that structural phrase, not on the word
# "infrastructure" itself, so it keeps matching after being changed once
# already, the same way _BRIEF_ROLE_RE does for the role.
_BRIEF_SCOPE_RE = re.compile(r"^You are (?:a|an) (.+?) assistant for ", re.MULTILINE)


def brief_scope() -> Optional[str]:
    """The current scope phrase (e.g. "infrastructure"), or None if it can't
    be found -- an unusual brief that doesn't follow the template's opening
    sentence at all, not something /setscope can safely touch."""
    try:
        text = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _BRIEF_SCOPE_RE.search(text)
    return m.group(1) if m else None


def set_brief_scope(scope: str) -> bool:
    """Rewrite the "You are a(n) <scope> assistant for ..." opening in both
    briefs. Only that phrase changes -- the role /setbrief sets, the hard
    boundaries, everything else is untouched. Returns False (writes nothing)
    if the opening sentence isn't in the expected shape to begin with.
    """
    scope = " ".join(scope.split()).rstrip(".")
    article = "an" if scope[:1].lower() in "aeiou" else "a"
    replacement = f"You are {article} {scope} assistant for "
    touched = False
    for path in (SYSTEM_PROMPT_FILE, GEMINI_PROMPT_FILE):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        text, n = _BRIEF_SCOPE_RE.subn(replacement, text, count=1)
        if n:
            path.write_text(text, encoding="utf-8")
            touched = True
    if touched:
        logger.warning("brief scope set: %s", scope)
    return touched


# Extra scope granted ONLY when the CURRENT message is confirmed from the bot
# owner (_is_owner(update), checked fresh every turn -- not baked into a
# brief that's shared by everyone). Deliberately separate from /setscope:
# /setscope changes what the agent is FOR, seen by every chat identically;
# this changes what it's ADDITIONALLY willing to do, and only when the sender
# really is the owner, so a group's shared scope can stay narrow (or broad)
# for everyone while the owner alone gets more without a second brief to keep
# in sync, and without a non-owner's turn in the same conversation history
# ever inheriting it by accident.
def owner_scope_text() -> str:
    if not OWNER_SCOPE_FILE.exists():
        return ""
    try:
        return OWNER_SCOPE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_owner_scope(text: str) -> None:
    OWNER_SCOPE_FILE.write_text(text.strip() + "\n", encoding="utf-8")
    logger.warning("owner-only extra scope set (%d chars)", len(text.strip()))


def clear_owner_scope() -> bool:
    if not OWNER_SCOPE_FILE.exists():
        return False
    OWNER_SCOPE_FILE.unlink()
    logger.warning("owner-only extra scope cleared")
    return True


# --------------------------------------------------------------------------
# MCP servers -- registered here, loaded by Claude Code via --mcp-config
# --------------------------------------------------------------------------

def read_mcp_servers() -> dict:
    """{"name": {"command": ..., "args": [...]}}. The file on disk is kept in
    the EXACT shape --mcp-config expects (a top-level {"mcpServers": {...}}
    object) so it can be handed to the CLI unmodified -- this just unwraps it
    for callers here."""
    if not MCP_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(MCP_CONFIG_FILE.read_text()).get("mcpServers", {})
    except Exception:
        logger.warning("mcp_servers.json unreadable, ignoring", exc_info=True)
        return {}


def _write_mcp_servers(servers: dict) -> None:
    MCP_CONFIG_FILE.write_text(json.dumps({"mcpServers": servers}, indent=2))


def _sync_agy_mcp(argv: list[str]) -> None:
    """agy keeps MCP servers in its OWN persistent config (`agy mcp add`),
    with no per-invocation flag like claude's --mcp-config -- so the registry
    has to be pushed to it rather than handed over at call time.

    Best-effort on purpose: agy may not be installed, may not be signed in,
    or may be an older build without the subcommand. None of that should stop
    a registration the Claude side will honour perfectly well, so a failure is
    logged and swallowed rather than raised."""
    try:
        proc = subprocess.run([AGY_BIN, "mcp", *argv], capture_output=True,
                              text=True, timeout=30)
        if proc.returncode != 0:
            logger.warning("agy mcp %s failed (%s): %s", argv[0], proc.returncode,
                           (proc.stderr or proc.stdout or "")[-200:])
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not reach agy to sync MCP config", exc_info=True)


def register_mcp_server(name: str, command: str, args: list[str]) -> None:
    servers = read_mcp_servers()
    servers[name] = {"command": command, "args": args}
    _write_mcp_servers(servers)
    logger.warning("MCP server registered: %s -> %s %s", name, command, " ".join(args))
    _sync_agy_mcp(["add", name, command, *args])


def remove_mcp_server(name: str) -> bool:
    servers = read_mcp_servers()
    if name not in servers:
        return False
    del servers[name]
    _write_mcp_servers(servers)
    logger.warning("MCP server removed: %s", name)
    _sync_agy_mcp(["remove", name])
    return True


def _mcp_allowed_tools_suffix() -> str:
    """"mcp__<name>" per registered server -- grants the WHOLE server's tools,
    the same granularity /addserver already grants for a whole machine, not
    a per-tool allowlist. Empty string when nothing is registered, so callers
    can always just concatenate it onto ALLOWED_TOOLS."""
    names = sorted(read_mcp_servers())
    return ",".join(f"mcp__{n}" for n in names)


# --------------------------------------------------------------------------
# Self-update
# --------------------------------------------------------------------------
SERVICE_NAME = os.environ.get("SERVICE_NAME", "lite-agent")
UPDATE_BRANCH = os.environ.get("UPDATE_BRANCH", "master")
UPDATE_CHECK_INTERVAL_HOURS = int(os.environ.get("UPDATE_CHECK_INTERVAL_HOURS", "6"))
UPDATE_STATE_FILE = BASE_DIR / "update_state.json"
# Written just before the restart, read once by the process that comes back --
# the only honest way to report "the new version is running".
UPDATE_ANNOUNCE_FILE = BASE_DIR / "update_announce.json"


def _git(*args: str, timeout: int = 60) -> tuple[bool, str]:
    """Run git inside the install dir. Never raises."""
    try:
        r = subprocess.run(["git", "-C", str(BASE_DIR), *args],
                           capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # git missing, timeout, anything
        return False, str(exc)
    return r.returncode == 0, (r.stdout.strip() or r.stderr.strip())


def is_git_checkout() -> bool:
    ok, out = _git("rev-parse", "--is-inside-work-tree")
    return ok and out == "true"


def current_version() -> str:
    ok, out = _git("describe", "--tags", "--always")
    return out if ok else "unknown"


def check_for_update() -> dict:
    """Ask the remote what it has. Costs no model tokens -- pure git.

    Returns a dict rather than a tuple because callers care about different
    parts of it: {"ok", "has_update", "current", "latest", "behind", "detail"}
    """
    if not is_git_checkout():
        return {"ok": False, "has_update": False, "current": current_version(),
                "latest": "", "behind": 0, "detail": "not-a-checkout"}
    ok, err = _git("fetch", "--tags", "--quiet", "origin", UPDATE_BRANCH, timeout=120)
    if not ok:
        return {"ok": False, "has_update": False, "current": current_version(),
                "latest": "", "behind": 0, "detail": err[:300]}
    _, local = _git("rev-parse", "HEAD")
    remote_ok, remote = _git("rev-parse", f"origin/{UPDATE_BRANCH}")
    if not remote_ok:
        return {"ok": False, "has_update": False, "current": current_version(),
                "latest": "", "behind": 0, "detail": remote[:300]}
    if local == remote:
        cur = current_version()
        return {"ok": True, "has_update": False, "current": cur, "latest": cur,
                "behind": 0, "detail": ""}
    # Only a fast-forward is offered. Local commits on top of the deployment
    # are somebody's deliberate change; silently discarding them would be the
    # worst thing this feature could do.
    ff, _ = _git("merge-base", "--is-ancestor", "HEAD", f"origin/{UPDATE_BRANCH}")
    _, latest = _git("describe", "--tags", "--always", f"origin/{UPDATE_BRANCH}")
    _, count = _git("rev-list", "--count", f"HEAD..origin/{UPDATE_BRANCH}")
    return {"ok": True, "has_update": True, "current": current_version(),
            "latest": latest, "behind": int(count) if count.isdigit() else 0,
            "detail": "" if ff else "diverged"}


def update_changelog(limit: int = 12) -> str:
    """One line per incoming commit, so the operator can see what they'd get."""
    ok, out = _git("log", "--oneline", "--no-decorate", f"-{limit}",
                   f"HEAD..origin/{UPDATE_BRANCH}")
    return out if ok else ""


def apply_update() -> tuple[bool, str, str]:
    """Fast-forward, then prove the result at least parses.

    Returns (ok, previous_commit, detail). On a build that does not compile the
    checkout is rolled straight back, because the alternative is a service that
    systemd restarts into the same crash every five seconds.

    Fetches for itself rather than trusting `origin/<branch>` to already be
    current -- the /update card is built from a fetch in check_for_update(),
    but time passes between the operator seeing that card and tapping the
    button, and this is also callable on its own (e.g. scripted). Without its
    own fetch, a stale local origin/<branch> ref would silently fast-forward
    to whatever was fetched last, not what is actually latest right now.
    """
    _git("fetch", "--tags", "--quiet", "origin", UPDATE_BRANCH, timeout=120)
    _, before = _git("rev-parse", "HEAD")
    ok, out = _git("merge", "--ff-only", f"origin/{UPDATE_BRANCH}", timeout=180)
    if not ok:
        return False, before, out[:400]
    check = subprocess.run(
        [sys.executable, "-m", "py_compile", str(Path(__file__).resolve())],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        _git("reset", "--hard", before)
        logger.error("update rolled back -- new code does not compile: %s", check.stderr[:400])
        return False, before, "the new version does not compile; rolled back"
    return True, before, out[:400]


SERVICE_UNIT_PATH = Path("/etc/systemd/system/lite-agent.service")
SERVICE_TEMPLATE = BASE_DIR / "systemd" / "lite-agent.service.template"


def refresh_systemd_unit() -> str:
    """Re-render the installed systemd unit from the template in the repo.

    /update fast-forwards the checkout, but the unit systemd actually runs was
    copied to /etc at install time and is never touched again -- so a release
    that hardens the unit (v0.2b.52 did: 9.6 UNSAFE -> 5.8 MEDIUM) would land
    the new template in the repo while the service kept running unhardened,
    and the operator would reasonably believe otherwise. That gap is the whole
    reason this exists.

    Returns a short status string for the log/reply. Best-effort throughout: a
    failure here must never block an update that has already succeeded, so
    every path returns rather than raises.

    Safety: the rendered unit is checked BEFORE it replaces anything, and the
    previous unit is kept and restored if the install goes wrong -- a broken
    unit means a bot that cannot come back up at all, which is far worse than
    an unhardened one.
    """
    if not SERVICE_TEMPLATE.exists():
        return "no template in this checkout"
    try:
        rendered = (SERVICE_TEMPLATE.read_text()
                    .replace("__INSTALL_USER__", getpass.getuser())
                    .replace("__INSTALL_DIR__", str(BASE_DIR))
                    .replace("__INSTALL_HOME__", str(Path.home())))
    except OSError:
        logger.warning("could not read the unit template", exc_info=True)
        return "template unreadable"

    try:
        current = SERVICE_UNIT_PATH.read_text()
    except OSError:
        current = ""
    if current.strip() == rendered.strip():
        return "unchanged"

    # systemd-analyze verify refuses a path that isn't a valid unit FILENAME
    # ("Failed to prepare filename ...: Invalid argument"), so the candidate
    # cannot just be a dotfile next to the code -- it needs the real unit name
    # in a directory of its own. Found by testing: with a dotfile name the
    # verification failed every time, which would have made this whole
    # refresh a silent no-op.
    staging = Path(tempfile.mkdtemp(prefix="isla_unit_"))
    tmp = staging / SERVICE_UNIT_PATH.name
    try:
        tmp.write_text(rendered)
        # Two checks, because neither alone is enough -- both measured, not
        # assumed:
        #
        # Our own structural check first. `systemd-analyze verify` does NOT
        # catch a malformed unit: fed a file containing an invented directive
        # it exits 0 quite happily. So it cannot be the thing standing between
        # a bad render and a bot that never comes back.
        if "[Service]" not in rendered or "\nExecStart=" not in rendered:
            logger.error("refusing to install a unit missing [Service]/ExecStart")
            return "new unit looks malformed, kept the old one"

        # Then verify, for the one thing it IS good at here: it exits 1 when
        # ExecStart names a binary that does not exist. That is exactly the
        # failure mode a path-substituting renderer like this one can create,
        # so it earns its place -- just not under the name "parse check".
        verify = subprocess.run(["systemd-analyze", "verify", str(tmp)],
                                capture_output=True, text=True, timeout=30)
        if verify.returncode != 0:
            logger.error("refusing to install a unit whose ExecStart does not "
                         "resolve: %s", (verify.stderr or "")[:300])
            return "new unit failed verification, kept the old one"

        backup = rendered_backup = None
        if current:
            backup = BASE_DIR / ".lite-agent.service.bak"
            backup.write_text(current)
            rendered_backup = str(backup)

        install = subprocess.run(
            ["sudo", "-n", "cp", str(tmp), str(SERVICE_UNIT_PATH)],
            capture_output=True, text=True, timeout=30)
        if install.returncode != 0:
            logger.warning("could not install the refreshed unit: %s",
                           (install.stderr or "")[:200])
            return "could not write the unit (needs sudo)"

        reload_ = subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"],
                                 capture_output=True, text=True, timeout=30)
        if reload_.returncode != 0 and rendered_backup:
            subprocess.run(["sudo", "-n", "cp", rendered_backup, str(SERVICE_UNIT_PATH)],
                           capture_output=True, timeout=30)
            logger.error("daemon-reload failed, restored the previous unit")
            return "daemon-reload failed, previous unit restored"
        logger.warning("systemd unit refreshed from template")
        return "refreshed"
    except (OSError, subprocess.SubprocessError):
        logger.warning("unit refresh failed", exc_info=True)
        return "unit refresh failed"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _read_update_state() -> dict:
    if UPDATE_STATE_FILE.exists():
        try:
            return json.loads(UPDATE_STATE_FILE.read_text())
        except Exception:
            logger.warning("update_state.json unreadable", exc_info=True)
    return {}


def _write_update_state(d: dict) -> None:
    UPDATE_STATE_FILE.write_text(json.dumps(d, indent=2))


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


def _unmark_setup(key: str) -> None:
    """Undo _mark_setup -- for when something PROVES a prior success no
    longer holds (a live re-auth failure, not just the absence of evidence).
    Without this, /start's card keeps showing a green check forever once one
    sign-in ever succeeded once, since agy_signed_in() falls back to this
    exact flag when the filesystem check finds nothing (see its own
    docstring) -- the operator has no way to tell a real re-login is needed."""
    st = _setup_state()
    if key in st:
        del st[key]
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


def logout_agy() -> bool:
    """Remove agy's stored OAuth token, forcing a genuinely fresh sign-in next
    time. Only that one file -- settings.json (the permissions written at
    install), conversation history, and everything else under
    ~/.gemini/antigravity-cli is left alone.

    The stale setup_state.json flag is cleared UNCONDITIONALLY, not only when
    a token file was actually found -- found live: a token file can already
    be gone (the session died on its own, which is exactly the scenario
    /logout exists to recover from) while the flag is still set, and an
    earlier version of this function returned before ever reaching
    _unmark_setup() in precisely that case, leaving /start showing green no
    matter how many times /logout was run.

    Returns whether a token file was actually removed -- purely informational
    for the caller's wording ("logged out" vs "already signed out"), not a
    signal of whether anything needed fixing.
    """
    token_file = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    had_token = token_file.exists()
    if had_token:
        token_file.unlink()
        logger.warning("agy logged out (OAuth token removed) by request")
    _unmark_setup("agy")
    return had_token


async def logout_claude() -> tuple[bool, str]:
    """`claude auth logout` is a real, documented subcommand -- unlike login,
    it needs no TTY, so a plain subprocess call is enough.

    The stale setup_state.json flag is cleared even when claude_signed_in()
    already says signed out -- same reasoning as logout_agy(): the flag can
    outlive the actual session, and clearing it is the one thing /logout must
    always do regardless of what else there was (or wasn't) to clean up.
    """
    if not claude_signed_in():
        _unmark_setup("claude")
        return False, ""
    try:
        proc = await asyncio.get_running_loop().run_in_executor(
            None, lambda: subprocess.run([CLAUDE_BIN, "auth", "logout"],
                                         capture_output=True, text=True, timeout=30))
    except Exception as exc:
        logger.exception("claude auth logout failed to run")
        return False, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        logger.warning("claude auth logout exited %d: %s", proc.returncode, detail)
        return False, detail
    _unmark_setup("claude")
    logger.warning("claude logged out by request")
    return True, ""


def setup_summary() -> list[tuple[str, bool, str]]:
    """(label, done, hint) for each thing /start can set up."""
    return [
        ("Gemini (Antigravity)", agy_signed_in(), "the primary tiers -- mini / mini pro"),
        ("Claude Code", claude_signed_in(),
         "gateway configured" if USE_GATEWAY else "the fallback tiers -- dede iku / dede nnet"),
        ("Security PIN", pin_is_set(), "guards changes to production and scheduled tasks"),
        ("Environment brief", brief_configured(), "what this agent looks after"),
    ]


def _wizard_keyboard(state: dict, lang: str = "id") -> InlineKeyboardMarkup:
    rows = []
    for key, (label, done, _) in zip(("agy", "claude", "pin", "brief"), setup_summary()):
        mark = "✅" if done else "⬜"
        verb = _t(lang, "Change", "Ganti") if done else _t(lang, "Set up", "Atur")
        rows.append([InlineKeyboardButton(f"{mark} {verb} {label}", callback_data=f"setup:{key}")])
    rows.append([InlineKeyboardButton(_t(lang, "✖️ Close", "✖️ Tutup"), callback_data="setup:close")])
    return InlineKeyboardMarkup(rows)


def _wizard_text(lang: str = "id") -> str:
    lines = [_t(lang, "\U0001f6e0 <b>iSmart-LA setup</b>", "\U0001f6e0 <b>Setup iSmart-LA</b>"), ""]
    for label, done, hint in setup_summary():
        lines.append(f"{'✅' if done else '⬜'} <b>{label}</b>\n   <i>{hint}</i>")
    if all(done for _, done, _ in setup_summary()):
        lines.append(_t(lang, "\nEverything is set up. Just talk to me normally.",
                              "\nSemuanya sudah diatur. Tinggal chat biasa saja."))
    else:
        lines.append(_t(lang,
            "\nTap anything above to set it up. You can stop and come back later -- "
            "each one is done separately, so after finishing one, send /start again "
            "to continue with what's left.",
            "\nTap salah satu di atas untuk mengaturnya. Bisa berhenti dan lanjut nanti -- "
            "tiap bagian diatur terpisah, jadi setelah satu selesai, kirim /start lagi "
            "untuk lanjut ke yang belum.",
        ))
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
# Extra tiers -- /usemodel only, NEVER part of the automatic fallback chain
# above. Reaching for Opus or Gemini Pro-high on every routine turn would burn
# through the shared subscription fast (see the ordering rationale above);
# kept reachable only when someone deliberately names one for a case that
# genuinely needs it. The default chain and its order are untouched by these
# existing at all.
# --------------------------------------------------------------------------
EXTRA_TIERS = _parse_tiers(
    "claude:claude-opus-5:dede opus,"
    "agy:gemini-3.1-pro-high:mini pro max"
)
BACKEND_LABELS.update({t["model"]: t["label"] for t in EXTRA_TIERS})
ALL_TIERS = TIERS + EXTRA_TIERS


# --------------------------------------------------------------------------
# Session persistence: telegram chat_id -> { active: name, sessions: {name: {
#   "claude": {model: session_id}, "agy": {model: conversation_id}
# }}}. Each of the 4 tiers keeps its OWN resume handle because they don't
# share conversation history with each other -- if a turn falls through from
# one tier to the next, the next tier starts that turn without the previous
# tier's history, but each tier's OWN history stays continuous across turns.
# --------------------------------------------------------------------------

def _empty_session() -> dict:
    """A genuinely fresh, unshared session block.

    This was a module-level constant copied with `dict(EMPTY_SESSION)` -- a
    SHALLOW copy, so the two inner dicts were the SAME objects in every
    session ever created that way. run_combo writes resume handles straight
    into them (`sess.setdefault("agy", {})[model] = conversation_id`), so the
    first chat to answer anything wrote its conversation id into the shared
    constant, and every session created afterwards inherited it.

    Two things that caused, both confirmed against the real get_chat_state:
      * /new did not actually start fresh -- it handed back a session already
        pointing at the previous conversation, quietly defeating the single
        biggest cost-saving habit this bot documents.
      * a brand-new chat could resume a DIFFERENT chat's conversation, which
        in a deployment shared between groups is a context leak, not just a
        billing surprise.

    A function, not a constant, so there is no shared mutable default left to
    reintroduce it.
    """
    return {"claude": {}, "agy": {}}
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
        entry = {"active": DEFAULT_SESSION_NAME, "sessions": {DEFAULT_SESSION_NAME: _empty_session()}}
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
        entry["sessions"].setdefault(entry["active"], _empty_session())
        for name in entry["sessions"]:
            entry["sessions"][name].setdefault("claude", {})
            entry["sessions"][name].setdefault("agy", {})
    sessions[chat_id] = entry
    return entry


# --------------------------------------------------------------------------
# Memory: MEMORY.md, edited ONLY via explicit /remember -- never automatically
# --------------------------------------------------------------------------

# Per-chat memory. MEMORY.md stays as a SHARED base every chat still reads --
# removing it would silently drop facts people already rely on -- but /remember
# now writes into this chat's own file instead. Before, one file was shared by
# every DM and every registered group, which directly contradicted the
# multi-tenant model this project deliberately supports (per-group PINs, one
# deployment shared between companies): a fact remembered in one company's
# group was injected into another company's very next turn.
MEMORY_DIR = BASE_DIR / "memory"
_CHAT_ID_SAFE = re.compile(r"^-?\d+$")


def _chat_memory_file(chat_id: Optional[str]) -> Optional[Path]:
    """This chat's own memory file, or None when there is no usable chat id.
    The id is validated rather than escaped -- a Telegram chat id is always an
    optionally-negative integer, so anything else is a bug upstream and has no
    business being turned into a filename."""
    if not chat_id or not _CHAT_ID_SAFE.match(str(chat_id)):
        return None
    return MEMORY_DIR / f"{chat_id}.md"


def load_memory_text(chat_id: Optional[str] = None) -> str:
    """Shared base first, then this chat's own facts. chat_id=None returns only
    the shared part -- for callers that genuinely have no chat context."""
    parts = []
    if MEMORY_FILE.exists():
        shared = MEMORY_FILE.read_text().strip()
        if shared:
            parts.append(shared)
    own = _chat_memory_file(chat_id)
    if own and own.exists():
        mine = own.read_text().strip()
        if mine:
            parts.append(mine)
    return "\n\n".join(parts)


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


def append_memory(fact: str, chat_id: Optional[str] = None) -> bool:
    """Record a fact for THIS chat only. Returns False when there is no usable
    chat id -- refusing is better than falling back to the shared file, which
    would leak it into every other chat: exactly the bug this replaced."""
    path = _chat_memory_file(chat_id)
    if path is None:
        return False
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Memory -- chat {chat_id}\n\nCross-session facts for THIS chat, "
            "curated manually via /remember. Never written to automatically, "
            "and never visible to any other chat.\n\n",
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- [{ts}] {fact}\n")
    return True


# --------------------------------------------------------------------------
# Claude Code invocation
# --------------------------------------------------------------------------

def _run_claude_once(prompt: str, session_id: Optional[str], session_name: str, model: str,
                     timeout: Optional[int] = None, owner_dm: bool = False,
                     chat_id: Optional[str] = None) -> dict:
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

    allowed_tools = ALLOWED_TOOLS
    mcp_suffix = _mcp_allowed_tools_suffix()
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--system-prompt-file", str(SYSTEM_PROMPT_FILE),
        "--output-format", "json",
    ]
    if mcp_suffix:
        # --mcp-config takes the SAME {"mcpServers": {...}} shape the file on
        # disk is already stored in -- handed to the CLI unmodified, no
        # translation layer to keep in sync. Granting "mcp__<server>" (not a
        # per-tool allowlist) matches the granularity /addserver already uses
        # for a whole machine: the operator who registered the server via PIN
        # is the one who decided it's trusted, not this call site.
        cmd += ["--mcp-config", str(MCP_CONFIG_FILE)]
        allowed_tools = f"{allowed_tools},{mcp_suffix}"
    cmd += ["--allowedTools", allowed_tools]

    # Checked fresh every turn, same as agy's _build_agy_prompt -- a group
    # conversation must never carry this even if the owner once spoke in it,
    # and the owner's own DM must have it the instant they show up in a
    # resumed conversation, not just when one starts. Folded into the SAME
    # --append-system-prompt call as MEMORY.md rather than a second one, since
    # whether Claude Code CLI accumulates repeated flags or lets the last one
    # win was never verified and isn't worth depending on either way.
    memory_text = load_memory_text(chat_id)
    extra_parts = [p for p in (memory_text, owner_scope_text() if owner_dm else "") if p]
    combined_extra = "\n\n".join(extra_parts)
    if combined_extra:
        cmd += ["--append-system-prompt", combined_extra]

    if session_id:
        cmd += ["--resume", session_id]
    else:
        # Fresh session: tag it with our session_name so it's identifiable if
        # someone inspects `claude` sessions directly on the box.
        cmd += ["--name", session_name]

    logger.info(
        "running claude: session_name=%s session_id=%s prompt_len=%d memory_chars=%d owner_dm=%s",
        session_name, session_id, len(prompt), len(memory_text), owner_dm,
    )
    proc = subprocess.run(
        cmd, cwd=str(BASE_DIR), env=env,
        capture_output=True, text=True, timeout=timeout or CLAUDE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Claude Code exited {proc.returncode}: {proc.stderr[-800:]}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude Code returned non-JSON output: {proc.stdout[-800:]}")


def run_claude(prompt: str, session_id: Optional[str], session_name: str, model: str,
              timeout: Optional[int] = None, owner_dm: bool = False,
              chat_id: Optional[str] = None) -> dict:
    """Run one Claude Code turn on an explicit model. Falls back to a fresh
    session if --resume fails (e.g. the referenced session expired)."""
    try:
        return _run_claude_once(prompt, session_id, session_name, model, timeout, owner_dm, chat_id)
    except RuntimeError as exc:
        if session_id:
            logger.warning("resume with session=%s failed (%s), retrying fresh", session_id, exc)
            return _run_claude_once(prompt, None, session_name, model, timeout, owner_dm, chat_id)
        raise


# --------------------------------------------------------------------------
# agy (Antigravity CLI) invocation -- native Gemini access, fixed-price
# --------------------------------------------------------------------------

def _build_agy_prompt(prompt: str, include_env: bool = False, owner_dm: bool = False,
                      chat_id: Optional[str] = None) -> str:
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
    memory_text = load_memory_text(chat_id)
    if memory_text:
        parts.append(f"[Cross-session facts to remember:]\n{memory_text}")
    # Checked fresh every turn, unlike include_env above -- on purpose: a
    # group conversation must never carry this even if the owner once spoke
    # in it, and the owner's own DM must have it the instant they show up in
    # a resumed conversation, not just when one starts. owner_dm reflects
    # THIS turn's real sender and chat type (owner, AND in their own private
    # DM -- never a group, even one the owner is speaking in), confirmed by
    # the caller, not something conversation history can carry forward.
    if owner_dm:
        extra = owner_scope_text()
        if extra:
            parts.append(
                "[Additional scope, ONLY because this message is confirmed from "
                f"the bot owner in their own private chat:]\n{extra}"
            )
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

# A conversation handle is only worth throwing away when the handle ITSELF is
# what failed. A timeout -- or a network blip, or a rate limit -- leaves the
# conversation intact on disk and usually mid-task, so discarding it there
# forces the next message to start from nothing AND re-send the whole brief,
# which is the single most expensive thing this bot does.
# Found live on bscloud: agy timed out after 2 turns on a healthy conversation
# (error: "timeout waiting for response"), the handle was dropped, and the
# user's next "Lanjutkan yang ini" opened a BRAND NEW conversation with a
# ~12,000-character brief (prompt_len=11823, conversation_id=None) instead of
# resuming the task it was told to continue.
_HANDLE_SURVIVES_HINTS = (
    "timeout", "timed out", "deadline",
    "connection", "network", "unreachable", "reset by peer", "broken pipe",
    "rate limit", "ratelimit", "429", "quota", "overloaded",
    "temporarily unavailable", "502", "503", "504",
)


def _handle_survives(exc: Exception) -> bool:
    """True when the failure says nothing bad about the conversation handle
    itself, so the NEXT turn should resume it rather than start over."""
    msg = str(exc).lower()
    return any(h in msg for h in _HANDLE_SURVIVES_HINTS)


# agy reports the conversation it was working in even when the run fails, e.g.
# {"conversation_id":"5dd5b5ca-...","status":"ERROR","error":"timeout ..."}.
# That matters for a run that STARTED fresh: the id exists only in that error
# payload, so without picking it up here a long first turn that times out
# orphans the conversation and every bit of work it already did.
_CONV_ID_IN_ERROR_RE = re.compile(
    r'"conversation_id"\s*:\s*"([0-9a-fA-F-]{36})"'
)


def _conversation_id_from_error(exc: Exception) -> Optional[str]:
    m = _CONV_ID_IN_ERROR_RE.search(str(exc))
    return m.group(1) if m else None


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


# Antigravity's OAuth session can expire between actual uses without agy_signed_in()
# ever noticing (that check is filesystem-based, see its own docstring) -- so an
# idle deployment can sit "signed in" on the setup card while every real gemini
# call quietly fails over to Claude. Detected from the tier chain's own failure
# log: when this happens, agy's stderr carries the OAuth authorize URL it's stuck
# waiting on. The prompt text ("Waiting for authentication...") itself never
# reaches this -- it goes straight to agy's controlling terminal, not the pipe
# _run_agy_once() reads -- but the URL survives into the captured stderr tail.
_AGY_REAUTH_MARKERS = ("accounts.google.com", "oauth", "googleapis.com/auth", "googleapis.com%2fauth")


def _agy_attempt_needs_reauth(attempts: list[str]) -> bool:
    for line in attempts:
        if line.startswith("agy:") and "FAILED" in line:
            low = line.lower()
            if any(marker in low for marker in _AGY_REAUTH_MARKERS):
                return True
    return False


# How often to say something again while it stays broken -- once per real
# message that hits it, not a timer, and reset the moment agy actually
# succeeds again so a NEW outage is never left waiting out a stale cooldown
# from one that already resolved.
AGY_REAUTH_NOTICE_COOLDOWN_HOURS = float(os.environ.get("AGY_REAUTH_NOTICE_COOLDOWN_HOURS", "1"))
_agy_reauth_last_notice: float = 0.0


def _run_agy_once(prompt: str, model: str, conversation_id: Optional[str],
                  timeout: Optional[int] = None, print_timeout: Optional[str] = None) -> dict:
    cmd = [
        AGY_BIN, "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--print-timeout", print_timeout or AGY_PRINT_TIMEOUT,
    ]
    if conversation_id:
        cmd += ["--conversation", conversation_id]

    logger.info(
        "running agy: model=%s conversation_id=%s prompt_len=%d",
        model, conversation_id, len(prompt),
    )
    proc = subprocess.run(
        cmd, cwd=str(AGY_WORKDIR), capture_output=True, text=True, timeout=timeout or AGY_TIMEOUT,
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

# How many model turns may be genuinely in flight at once, process-wide.
# Less a throughput knob than a memory one: each in-flight turn is a real
# agy/claude subprocess, and one agy process was measured at ~240MB RSS on a
# live deployment, so an unbounded fan-out is how a busy group becomes an OOM.
MAX_CONCURRENT_TURNS = int(os.environ.get("MAX_CONCURRENT_TURNS", "3") or 3)

_turn_slots: Optional[asyncio.Semaphore] = None
_chat_turn_locks: dict[str, asyncio.Lock] = {}


def _turn_semaphore() -> asyncio.Semaphore:
    """Created lazily so it binds to the running loop rather than import time."""
    global _turn_slots
    if _turn_slots is None:
        _turn_slots = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
    return _turn_slots


def _chat_turn_lock(chat_id: str) -> asyncio.Lock:
    """One lock per chat: turns run in PARALLEL across chats, strictly in
    ORDER within a chat.

    Within a chat that ordering is correctness, not politeness -- two turns in
    the same chat share one session file and the same per-tier conversation
    ids, so overlapping them would have them clobber each other's resume
    handles. That is the same class of bug v0.2b.39 had to fix by hand, and
    there is no reason to reintroduce it concurrently.
    """
    lock = _chat_turn_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_turn_locks[chat_id] = lock
    return lock


# --------------------------------------------------------------------------
# Token ledger -- the cost claim, written as data instead of prose
# --------------------------------------------------------------------------

def _ledger_append(row: dict) -> None:
    """Append one turn to the ledger. Best-effort by design: a turn that
    already cost real tokens must never be lost or failed because bookkeeping
    hit a disk error."""
    try:
        row.setdefault("ts", _dt.datetime.now().isoformat(timespec="seconds"))
        with LEDGER_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("could not write the spend ledger", exc_info=True)


def _ledger_read(days: int = 30) -> list[dict]:
    """Rows from the last `days` days, newest last. Unreadable lines are
    skipped rather than aborting the whole report -- a truncated final line
    (a crash mid-write) must not make /spend useless."""
    if not LEDGER_FILE.exists():
        return []
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).isoformat(timespec="seconds")
    rows = []
    try:
        for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if str(r.get("ts", "")) >= cutoff:
                rows.append(r)
    except OSError:
        logger.warning("could not read the spend ledger", exc_info=True)
    return rows


def _int(v) -> int:
    return v if isinstance(v, int) else 0


def run_combo(prompt: str, sess: dict, session_name: str,
              forced_tier: Optional[dict] = None, owner_dm: bool = False,
              trace: Optional[list] = None, chat_id: Optional[str] = None) -> tuple[dict, str, list[str]]:
    """Walk the TIERS chain in order, moving on only when a tier fails in a way
    another tier could plausibly survive.

    `sess` is this named session's {"claude": {model: id}, "agy": {model: id}}
    dict -- mutated in place with whichever tier's resume handle actually got
    used; the caller persists it afterward.

    `trace`: optional list the caller owns; one structured dict is appended per
    tier attempt (provider, model, ok/skipped, usage, tokens actually burned on
    a failure). An out-parameter rather than a global on purpose -- turns run
    concurrently since v0.2b.40, so a module-level collector would interleave
    two chats' numbers into one row.

    `owner_dm`: True only when THIS turn is confirmed from the bot owner in
    their own private chat (never a group, even one the owner is speaking
    in) -- see owner_scope_text(). Passed straight through to whichever
    backend answers so it can fold in the owner's extra scope; a resumed
    conversation never carries it forward on its own.

    Returns (normalized_result, model_name, attempt_log). model_name is the
    literal model that answered -- never an alias -- so the "— by <label>" tag
    is always exact. attempt_log records every attempt, including skipped and
    failed ones, so a human can see what a turn really cost before it landed.
    """
    attempts: list[str] = []
    agy_convs = sess.setdefault("agy", {})       # {model: conversation_id}
    claude_sessions = sess.setdefault("claude", {})  # {model: session_id}

    # A forced tier (/usemodel) goes first; the normal chain still backs it up
    # if it fails, rather than leaving the user with a hard error -- the reply
    # tag already shows whenever that safety net had to fire.
    chain = TIERS
    if forced_tier is not None:
        chain = [forced_tier] + [t for t in TIERS if t["model"] != forced_tier["model"]]

    burned = 0          # everything this turn has cost so far, failures included

    for tier in chain:
        provider, model = tier["provider"], tier["model"]
        if TURN_TOKEN_CEILING and burned >= TURN_TOKEN_CEILING:
            attempts.append(f"{provider}:{model} SKIPPED (turn ceiling)")
            if trace is not None:
                trace.append({"provider": provider, "model": model,
                              "outcome": "skipped", "reason": "ceiling", "total": 0})
            logger.warning("turn ceiling hit: %d/%d tokens burned, stopping the chain",
                           burned, TURN_TOKEN_CEILING)
            raise RuntimeError(
                f"Turn stopped at the {TURN_TOKEN_CEILING:,}-token ceiling after "
                f"burning {burned:,} across {len(attempts)} attempt(s). "
                f"Raise TURN_TOKEN_CEILING or split the request into smaller steps."
            )
        if not _tier_available(model):
            attempts.append(f"{provider}:{model} SKIPPED (cooldown)")
            if trace is not None:
                trace.append({"provider": provider, "model": model,
                              "outcome": "skipped", "total": 0})
            continue
        try:
            if provider == "agy":
                conv_id = agy_convs.get(model)
                # Each tier keeps its OWN conversation, so "is this fresh?" is a
                # per-tier question -- the brief has to go to whichever tier is
                # starting from nothing, which may be a later one even while an
                # earlier one is mid-conversation.
                parsed = _run_agy_once(
                    _build_agy_prompt(prompt, include_env=(conv_id is None), owner_dm=owner_dm,
                                      chat_id=chat_id),
                    model, conv_id
                )
                usage = parsed.get("usage", {}) or {}
                attempts.append(f"agy:{model} OK ({usage.get('total_tokens', '?')} tok)")
                if trace is not None:
                    trace.append({
                        "provider": "agy", "model": model, "outcome": "ok",
                        "in": _int(usage.get("input_tokens")),
                        "out": _int(usage.get("output_tokens")),
                        "think": _int(usage.get("thinking_tokens")),
                        "cache_read": _int(usage.get("cache_read_tokens")),
                        "total": _int(usage.get("total_tokens")),
                    })
                burned += _int(usage.get("total_tokens"))
                agy_convs[model] = parsed.get("conversation_id")
                _note_tier_success(model)
                global _agy_reauth_last_notice
                _agy_reauth_last_notice = 0.0  # a real success -- the next outage is a fresh one
                return _normalize_agy_result(parsed), model, attempts

            result = run_claude(prompt, claude_sessions.get(model), session_name, model,
                               owner_dm=owner_dm, chat_id=chat_id)
            usage = result.get("usage", {}) or {}
            total = sum(v for v in usage.values() if isinstance(v, int))
            attempts.append(f"claude:{model} OK ({total} tok)")
            if trace is not None:
                trace.append({
                    "provider": "claude", "model": model, "outcome": "ok",
                    "in": _int(usage.get("input_tokens")),
                    "out": _int(usage.get("output_tokens")),
                    "cache_read": _int(usage.get("cache_read_input_tokens")),
                    "cache_write": _int(usage.get("cache_creation_input_tokens")),
                    "total": total,
                    "cost_usd": result.get("total_cost_usd"),
                })
            burned += total
            claude_sessions[model] = result.get("session_id")
            _note_tier_success(model)
            return result, model, attempts

        except Exception as exc:
            kind = _classify_failure(exc)
            attempts.append(f"{provider}:{model} FAILED/{kind} ({exc})")
            # A failed tier still spent real tokens most of the time (agy
            # reports them in its own error envelope). Those are the ones
            # nobody could add up before, and the ones the ceiling most needs
            # to count -- parsed OUTSIDE the trace block on purpose, since the
            # ceiling must work whether or not a caller asked for a trace.
            m = re.search(r"wasted_tokens=(\d+)", str(exc))
            wasted = int(m.group(1)) if m else 0
            if trace is not None:
                trace.append({"provider": provider, "model": model,
                              "outcome": "failed", "kind": kind,
                              "wasted": wasted, "total": 0})
            burned += wasted
            logger.warning("%s model=%s failed (%s): %s", provider, model, kind, exc)
            # Keep the resume handle unless the handle itself is suspect --
            # see _handle_survives. A timed-out conversation is still full of
            # the work the next message is meant to continue.
            store = agy_convs if provider == "agy" else claude_sessions
            if _handle_survives(exc):
                if not store.get(model):
                    # A fresh run that failed: its id exists only in the error.
                    recovered = _conversation_id_from_error(exc)
                    if recovered:
                        store[model] = recovered
                        logger.info("kept %s conversation %s despite a transient failure",
                                    model, recovered)
            else:
                store[model] = None
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
    raise RuntimeError(f"All {len(chain)} tier(s) failed: {' -> '.join(attempts)}")


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


# -- group "wake word" gating ------------------------------------------------
# With Telegram's own Privacy Mode, a group only forwards commands and replies
# to the bot -- confirmed live that a plain "@mention" embedded mid-sentence
# does NOT count as an exception and never reaches the bot at all, even though
# it looks like a valid mention in the client. To let a real @mention wake the
# bot up (not just a reply), Privacy Mode has to be turned OFF, which then
# forwards every group message -- so this gate replaces what Privacy Mode used
# to enforce, checked in-app instead: a reply to the bot's own message, or an
# actual @mention entity, or nothing happens. Ordinary chatter never reaches
# the model either way; only the mechanism moved.
def _entity_text(text: str, offset: int, length: int) -> str:
    """Telegram entity offsets are UTF-16 code-unit based, not Python codepoint
    indices -- slicing the string directly misaligns whenever a character
    before the entity sits outside the BMP (e.g. an emoji). Round-trip
    through UTF-16 to get this right."""
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def _strip_entity(text: str, offset: int, length: int) -> str:
    """Remove a UTF-16-indexed entity span from text, same care as above."""
    encoded = text.encode("utf-16-le")
    start, end = offset * 2, (offset + length) * 2
    return (encoded[:start] + encoded[end:]).decode("utf-16-le", errors="ignore")


def _group_mention_span(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[tuple[int, int]]:
    """(offset, length) of the entity that @-mentions this bot by username in
    update.message, or None. A literal '@name' substring Telegram did not
    itself parse as a mention entity does not count -- this matches exactly
    what Telegram recognizes as addressing the bot, nothing looser."""
    msg = update.message
    bot_username = (context.bot.username or "").lower()
    if not msg or not msg.entities or not bot_username:
        return None
    text = msg.text or ""
    for ent in msg.entities:
        if ent.type != "mention":
            continue
        mention = _entity_text(text, ent.offset, ent.length)
        if mention.lstrip("@").lower() == bot_username:
            return ent.offset, ent.length
    return None


def _is_reply_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    replied = msg.reply_to_message if msg else None
    return bool(replied and replied.from_user and replied.from_user.id == context.bot.id)


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


def _msg(update: Update):
    """The message to reply to, whether this turn came from the user typing or
    from them tapping a button (a callback update has no .message)."""
    if update.message is not None:
        return update.message
    return update.callback_query.message if update.callback_query else None


async def _reply_chunked(update: Update, text: str, tag_html: str = "",
                         already_html: bool = False) -> None:
    """Send a (possibly long) reply.

    `tag_html` is appended to the LAST chunk only and passed through as trusted
    HTML -- it is our own backend label, not model output.

    `already_html` marks text WE built, which is already valid Telegram HTML and
    must not be escaped. Model output (the default) is Markdown from an
    untrusted source and always goes through the converter. Two explicit paths
    rather than sniffing the string: a model reply that happens to mention
    <b> should still show those characters, not turn bold."""
    text, redacted = redact_secrets(text)
    if redacted:
        logger.warning("outbound message contained a known secret -- redacted before sending")
    chunks = _chunk_lines(text, 3500)
    for idx, chunk in enumerate(chunks):
        body = chunk if already_html else _md_to_telegram_html(chunk)
        if idx == len(chunks) - 1 and tag_html:
            body = f"{body}\n\n{tag_html}"
        plain = chunk
        if idx == len(chunks) - 1 and tag_html:
            plain = f"{plain}\n\n{re.sub(r'<[^>]+>', '', tag_html)}"
        for attempt in (1, 2, 3):
            try:
                await _msg(update).reply_text(body, parse_mode="HTML")
                break
            except BadRequest as exc:
                # Malformed entities: formatting is the problem, so retrying it
                # unchanged would fail identically. Send it unformatted instead
                # -- losing the formatting beats losing the message.
                logger.warning("HTML rejected by Telegram (%s); sending as plain text", exc)
                try:
                    await _msg(update).reply_text(plain)
                except (TimedOut, NetworkError):
                    logger.warning("plain-text fallback also timed out", exc_info=True)
                break
            except (TimedOut, NetworkError) as exc:
                # Nothing wrong with the message -- the network just hiccuped.
                # These clear in seconds, so wait briefly and try the same text.
                if attempt == 3:
                    logger.error("gave up sending a reply after 3 attempts: %s", exc)
                    raise
                logger.warning("send failed (%s), retrying (%d/3)", exc, attempt)
                await asyncio.sleep(2 * attempt)



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
    lang = _chat_lang(update)
    if not p.exists() or not p.is_file():
        logger.warning("MEDIA path not found: %s", path)
        await _msg(update).reply_text(_t(lang,
            f"⚠️ File not found, couldn't send it: {path}",
            f"⚠️ File nggak ketemu buat dikirim: {path}",
        ))
        return
    size = p.stat().st_size
    if size > TELEGRAM_MAX_FILE_BYTES:
        await _msg(update).reply_text(_t(lang,
            f"⚠️ File {p.name} ({size / 1024 / 1024:.1f}MB) exceeds Telegram's bot "
            f"upload limit (50MB), can't send it.",
            f"⚠️ File {p.name} ({size / 1024 / 1024:.1f}MB) melebihi limit Telegram "
            f"buat bot (50MB), nggak bisa dikirim.",
        ))
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
            await _msg(update).reply_text(_t(lang,
                f"🔒 *{p.name}* was not sent: it contains a credential "
                f"(token/API key). Strip that out first if it really needs sending.",
                f"🔒 File *{p.name}* tidak dikirim: isinya memuat kredensial "
                f"(token/API key). Hapus dulu bagian itu kalau memang perlu dikirim.",
            ), parse_mode="Markdown")
            return
    try:
        with p.open("rb") as f:
            await _msg(update).reply_document(
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
        await _msg(update).reply_text(_t(lang,
            f"⚠️ Failed to send {p.name}: upload error.",
            f"⚠️ Gagal kirim file {p.name}: error saat upload.",
        ))


def _list_gdrive_accounts() -> list[str]:
    """Every connected Drive account -- any rclone remote named exactly
    "gdrive" or "gdrive_<something>". That naming convention is the whole
    registry; no separate file to keep in sync with rclone.conf itself."""
    if not RCLONE_CONF.exists():
        return []
    try:
        text = RCLONE_CONF.read_text(encoding="utf-8")
    except OSError:
        return []
    names = re.findall(r"^\[(gdrive(?:_[A-Za-z0-9_-]+)?)\]", text, re.MULTILINE)
    return sorted(names, key=lambda n: (n != "gdrive", n))


def _read_gdrive_room_accounts() -> dict:
    if not GDRIVE_ROOM_ACCOUNTS_FILE.exists():
        return {}
    try:
        return json.loads(GDRIVE_ROOM_ACCOUNTS_FILE.read_text())
    except Exception:
        logger.warning("gdrive_room_accounts.json unreadable", exc_info=True)
        return {}


def _write_gdrive_room_accounts(items: dict) -> None:
    GDRIVE_ROOM_ACCOUNTS_FILE.write_text(json.dumps(items, indent=2))


def _gdrive_effective_default(chat_id: str, accounts: list[str]) -> Optional[str]:
    """This room's explicitly chosen account, or -- if it has never chosen and
    there is only ONE account connected -- that one, since there is no real
    ambiguity to ask about. Returns None only when a genuine choice is needed
    (two or more accounts exist and this room hasn't picked)."""
    explicit = _read_gdrive_room_accounts().get(chat_id)
    if explicit and explicit in accounts:
        return explicit
    if len(accounts) == 1:
        return accounts[0]
    return None


def _sanitize_drive_folder_name(name: str) -> str:
    """A Telegram group title, made safe as a single path segment -- no
    slashes to accidentally escape into a different folder, no leading/
    trailing junk, capped so an absurd title can't produce an absurd path."""
    cleaned = re.sub(r"[/\\]+", "-", name or "").strip()
    return (cleaned or "group")[:80]


async def _gdrive_effective_path(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  to_raw: str) -> str:
    """A plain path lands inside THIS chat's own Drive subfolder when the
    chat is a group -- the model never needs to know or add the group's
    name itself. A leading "/" asks for the shared root instead, but that
    only works for the room's own admin (or the owner) -- anyone else's
    escape attempt is quietly kept inside the group's own folder instead of
    erroring, the same way an untrusted LEARN: line is quietly dropped
    rather than refused with a scary message. A DM has no group folder to
    begin with, so it always goes straight to the path given."""
    chat = update.effective_chat
    rel = to_raw.lstrip("/")
    if chat is None or chat.type == "private":
        return rel
    wants_root = to_raw.startswith("/")
    if wants_root and await _may_authorize_group_action(update, context):
        return rel
    folder = _sanitize_drive_folder_name(chat.title or str(chat.id))
    return f"{folder}/{rel}"


def _gdrive_upload(remote: str, local_path: str, drive_rel_path: str) -> tuple[bool, str]:
    """Copy one local file to <remote>:GDRIVE_ROOT/drive_rel_path via rclone,
    then fetch a shareable link. Blocking -- call via loop.run_in_executor.
    rclone creates any missing intermediate folders on its own, so the
    caller never needs to check or create the destination first."""
    dest = f"{remote}:{GDRIVE_ROOT}/{drive_rel_path}"
    try:
        subprocess.run(
            [RCLONE_BIN, "copyto", local_path, dest],
            check=True, capture_output=True, text=True, timeout=120,
        )
        link = subprocess.run(
            [RCLONE_BIN, "link", dest],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return True, link.stdout.strip()
    except subprocess.CalledProcessError as exc:
        return False, ((exc.stderr or exc.stdout or str(exc)) or "").strip()[:500]
    except subprocess.TimeoutExpired:
        return False, "timed out talking to Google Drive"


async def _send_to_gdrive(update: Update, context: ContextTypes.DEFAULT_TYPE, req: dict) -> None:
    lang = _chat_lang(update)
    local_path = req["file"]
    p = Path(local_path)
    if not p.is_absolute():
        p = BASE_DIR / p
    if not p.exists() or not p.is_file():
        logger.warning("GDRIVE source not found: %s", local_path)
        await _msg(update).reply_text(_t(lang,
            f"\u26a0\ufe0f File not found, couldn't upload to Drive: {local_path}",
            f"\u26a0\ufe0f File tidak ditemukan, gagal upload ke Drive: {local_path}",
        ))
        return
    size = p.stat().st_size
    # Same gate as MEDIA: delivery -- leaving the box via Drive deserves the
    # same caution as leaving it via Telegram.
    if _SECRETS and size <= 8 * 1024 * 1024:
        try:
            blob = p.read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            blob = ""
        if any(sec in blob for sec in _SECRETS):
            logger.error("REFUSED to upload %s to Drive -- it contains a credential", p)
            await _msg(update).reply_text(_t(lang,
                f"\U0001f512 *{p.name}* was not uploaded to Drive: it contains a credential "
                f"(token/API key). Strip that out first if it really needs sending.",
                f"\U0001f512 *{p.name}* tidak diupload ke Drive: berisi kredensial "
                f"(token/API key). Hilangkan dulu kalau memang perlu dikirim.",
            ), parse_mode="Markdown")
            return
    accounts = _list_gdrive_accounts()
    if not accounts:
        await _msg(update).reply_text(_t(lang,
            "\u26a0\ufe0f Google Drive isn't connected yet -- see README (\"Google Drive\") to set it up.",
            "\u26a0\ufe0f Google Drive belum terhubung -- lihat README (\"Google Drive\") untuk setup.",
        ))
        return
    chat_id = str(update.effective_chat.id)
    remote = _gdrive_effective_default(chat_id, accounts)
    if not remote:
        await _msg(update).reply_text(_t(lang,
            "\U0001f4c1 This room hasn't picked a Drive account yet -- run /gdrive to choose one.",
            "\U0001f4c1 Room ini belum pilih akun Drive -- jalankan /gdrive untuk pilih.",
        ))
        return
    rel_path = await _gdrive_effective_path(update, context, req["to"])
    loop = asyncio.get_running_loop()
    ok, detail = await loop.run_in_executor(None, _gdrive_upload, remote, str(p), rel_path)
    if ok:
        await _msg(update).reply_text(_t(lang,
            f"\U0001f4c1 Uploaded to Drive ({remote}): {rel_path}\n{detail}",
            f"\U0001f4c1 Terupload ke Drive ({remote}): {rel_path}\n{detail}",
        ))
    else:
        await _msg(update).reply_text(_t(lang,
            f"\u26a0\ufe0f Drive upload failed for {rel_path}: {detail}",
            f"\u26a0\ufe0f Upload ke Drive gagal untuk {rel_path}: {detail}",
        ))



async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    chat_id = str(update.effective_chat.id)
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    active = state["active"]
    state["sessions"][active] = _empty_session()
    save_sessions(sessions)
    lang = _chat_lang(update)
    await update.message.reply_text(_t(lang,
        f"Session '{active}' restarted (fresh). \U0001f9f9",
        f"Sesi '{active}' dimulai ulang (fresh). \U0001f9f9",
    ))


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    name = " ".join(context.args).strip() if context.args else ""
    if not name:
        await update.message.reply_text(_t(lang,
            "Usage: /session <name>\nExample: /session incident-123\n"
            "Use /sessions to see existing sessions.",
            "Pakai: /session <nama>\nContoh: /session qradar-incident\n"
            "Ketik /sessions buat lihat daftar sesi yang sudah ada.",
        ))
        return
    chat_id = str(update.effective_chat.id)
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    is_new = name not in state["sessions"]
    if is_new:
        state["sessions"][name] = _empty_session()
    state["active"] = name
    save_sessions(sessions)
    if is_new:
        await update.message.reply_text(_t(lang,
            f"New session '{name}' created & active. \U0001f4c2",
            f"Sesi baru '{name}' dibuat & aktif. \U0001f4c2",
        ))
    else:
        await update.message.reply_text(_t(lang,
            f"Switched to session '{name}' (continuing from before). \U0001f4c2",
            f"Pindah ke sesi '{name}' (lanjut dari sebelumnya). \U0001f4c2",
        ))


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    chat_id = str(update.effective_chat.id)
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    lang = _chat_lang(update)
    lines = [_t(lang, "\U0001f4c2 Saved sessions:", "\U0001f4c2 Sesi tersimpan:")]
    for name in state["sessions"]:
        marker = _t(lang, " (active)", " (aktif)") if name == state["active"] else ""
        lines.append(f"• {name}{marker}")
    lines.append(_t(lang, "\nSwitch session: /session <name>", "\nPindah sesi: /session <nama>"))
    await update.message.reply_text("\n".join(lines))


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    fact = " ".join(context.args).strip() if context.args else ""
    if not fact:
        await update.message.reply_text(_t(lang,
            "Usage: /remember <fact to remember permanently>",
            "Pakai: /remember <fakta yang mau disimpan permanen>",
        ))
        return
    if not append_memory(fact, str(update.effective_chat.id)):
        return await update.message.reply_text(_t(lang,
            "Couldn't store that -- this chat has no usable id.",
            "Tidak bisa disimpan -- chat ini tidak punya id yang bisa dipakai."))
    await update.message.reply_text(_t(lang,
        f"\U0001f4dd Remembered <b>for this chat only</b>: {_tg_escape(fact)}",
        f"\U0001f4dd Diingat <b>khusus chat ini</b>: {_tg_escape(fact)}"),
        parse_mode="HTML")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    chat_id = str(update.effective_chat.id)
    shared = MEMORY_FILE.read_text().strip() if MEMORY_FILE.exists() else ""
    own_path = _chat_memory_file(chat_id)
    own = own_path.read_text().strip() if own_path and own_path.exists() else ""
    if not shared and not own:
        await update.message.reply_text(_t(lang,
            "Memory is empty. Add facts with /remember <fact>.",
            "Memori masih kosong. Tambah lewat /remember <fakta>.",
        ))
        return
    # Labelled on purpose: after per-chat isolation, "who else can see this?"
    # is the first thing anyone should be able to answer at a glance.
    parts = []
    if own:
        parts.append(_t(lang, "=== THIS CHAT ONLY ===",
                              "=== KHUSUS CHAT INI ===") + "\n" + own)
    if shared:
        parts.append(_t(lang, "=== SHARED (every chat sees this) ===",
                              "=== BERSAMA (semua chat melihat ini) ===") + "\n" + shared)
    await _reply_chunked(update, "\n\n".join(parts))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Status check with ZERO model tokens: runs the snapshot collector
    directly and prints its already-digested output. This is the 'graduated
    skill' path -- a question we've already solved is answered by a script,
    not by re-deriving it with an LLM every time."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    if not SNAPSHOT_SCRIPT.exists():
        await update.message.reply_text(_t(lang,
            f"⚠️ Collector not installed: {SNAPSHOT_SCRIPT}",
            f"⚠️ Collector belum terpasang: {SNAPSHOT_SCRIPT}",
        ))
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    force = bool(context.args and context.args[0].lower() in ("force", "fresh", "-f"))
    cmd = ["python3", str(SNAPSHOT_SCRIPT)] + (["--force"] if force else [])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        await update.message.reply_text(_t(lang, "⚠️ Collector timeout (>120s).", "⚠️ Collector timeout (>120 detik)."))
        return
    if proc.returncode != 0:
        await update.message.reply_text(_t(lang,
            f"⚠️ Collector failed: {proc.stderr[-500:]}",
            f"⚠️ Collector gagal: {proc.stderr[-500:]}",
        ))
        return
    logger.info("status command served (0 model tokens, force=%s)", force)
    await _reply_chunked(update, f"```\n{proc.stdout.strip()}\n```")


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List graduated skills. Zero model tokens -- just reads the registry."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    if not LIST_TOOLS_SCRIPT.exists():
        await update.message.reply_text(_t(lang,
            f"⚠️ Not installed: {LIST_TOOLS_SCRIPT}",
            f"⚠️ Belum terpasang: {LIST_TOOLS_SCRIPT}",
        ))
        return
    proc = subprocess.run(
        ["python3", str(LIST_TOOLS_SCRIPT)], capture_output=True, text=True, timeout=30
    )
    out = proc.stdout.strip() or proc.stderr.strip() or _t(lang, "(empty)", "(kosong)")
    await _reply_chunked(update, f"```\n{out}\n```")


def _graduate_target(sess: dict) -> Optional[tuple[str, str, str]]:
    """-> (provider, model, conversation_id) for the tier holding the case
    just solved, or None if this session has no history anywhere yet.

    /graduate used to look only at the primary Claude session, and said so in
    its own refusal ("if the last turn was answered by Gemini/'mini',
    /graduate can't see that history"). Since the default chain answers with
    Gemini first, that meant the one feature that genuinely REDUCES future
    token spend -- turning a solved case into a zero-token script -- was
    unavailable for most cases, which is precisely backwards.

    Preference order: whichever tier actually answered last (that is where the
    history is), then any Claude conversation, then any agy one.
    """
    last = sess.get("last_model")
    agy_convs = sess.get("agy") or {}
    claude_sessions = sess.get("claude") or {}
    if last:
        if agy_convs.get(last):
            return "agy", last, agy_convs[last]
        if claude_sessions.get(last):
            return "claude", last, claude_sessions[last]
    if claude_sessions.get(CLAUDE_MODEL_PRIMARY):
        return "claude", CLAUDE_MODEL_PRIMARY, claude_sessions[CLAUDE_MODEL_PRIMARY]
    for m, cid in claude_sessions.items():
        if cid:
            return "claude", m, cid
    for m, cid in agy_convs.items():
        if cid:
            return "agy", m, cid
    return None


async def cmd_graduate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn the case just solved in this session into a reusable script.

    Explicitly user-triggered, one bounded call, fixed instruction --
    deliberately NOT a background job that decides on its own what is
    worth saving."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    name = "-".join(context.args).strip().lower() if context.args else ""
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        await update.message.reply_text(_t(lang,
            "Usage: /graduate <script-name>\n"
            "Example: /graduate backup-coverage\n"
            "(lowercase letters, digits, - and _ only)",
            "Pakai: /graduate <nama-script>\n"
            "Contoh: /graduate backup-coverage\n"
            "(huruf kecil, angka, - dan _ saja)",
        ))
        return

    chat_id = str(update.effective_chat.id)
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    active = state["active"]
    target = _graduate_target(state["sessions"].get(active, {}))
    if target is None:
        await update.message.reply_text(_t(lang,
            "Nothing to graduate yet -- this session has no conversation on any "
            "tier. Solve a case first (ask, let it investigate, confirm the "
            "answer), then run /graduate here.",
            "Belum ada yang bisa di-graduate -- sesi ini belum punya percakapan di "
            "tier mana pun. Selesaikan dulu satu kasus (tanya, biarkan diselidiki, "
            "pastikan jawabannya benar), baru jalankan /graduate di sini.",
        ))
        return
    provider, gmodel, conv_id = target
    label = BACKEND_LABELS.get(gmodel, gmodel)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    instruction = GRADUATE_INSTRUCTION.format(name=name)
    loop = asyncio.get_running_loop()
    try:
        # Off the event loop for the same reason turns are (v0.2b.40): this
        # shells out to a CLI and can run for minutes.
        if provider == "agy":
            parsed = await loop.run_in_executor(
                None, functools.partial(_run_agy_once, instruction, gmodel, conv_id))
            result = _normalize_agy_result(parsed)
        else:
            result = await loop.run_in_executor(
                None, functools.partial(run_claude, instruction, conv_id, active, gmodel))
    except Exception as exc:
        logger.exception("graduate failed")
        await update.message.reply_text(_t(lang, f"⚠️ Error: {exc}", f"⚠️ Error: {exc}"))
        return

    new_session_id = result.get("session_id")
    if new_session_id:
        sessions = load_sessions()
        state = get_chat_state(sessions, chat_id)
        state["sessions"].setdefault(active, _empty_session())
        state["sessions"][active].setdefault(provider, {})[gmodel] = new_session_id
        save_sessions(sessions)

    usage = result.get("usage", {})
    logger.info(
        "graduate done: name=%s via=%s:%s in=%s out=%s cache_read=%s",
        name, provider, gmodel, usage.get("input_tokens"), usage.get("output_tokens"),
        usage.get("cache_read_input_tokens"),
    )
    body = result.get("result") or _t(lang, "(no response)", "(tidak ada respons)")
    # Same visibility principle as a normal reply's tag: say which tier's
    # history this was actually graduated from, since each tier keeps its own
    # and the answer depends on which one held the case.
    await _reply_chunked(update, f"{body}\n\n\u2014 graduated from {label}")


async def cmd_secure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Install and verify the read-only guard on every configured server.

    Exists because a deployment that predates this feature has hosts reachable
    by one unrestricted key, and no amount of bot-side gating helps when the
    credential itself has no limits. Idempotent, so re-running is the way to
    confirm nothing has drifted.
    """
    if not await _may_authorize_group_action(update, context):
        return
    lang = _chat_lang(update)
    servers = _read_servers()
    if not servers:
        await update.message.reply_text(_t(lang,
            "No servers configured yet -- /addserver first.",
            "Belum ada server terdaftar -- /addserver dulu."))
        return
    if not _keys_configured():
        await update.message.reply_text(_t(lang,
            "\u26a0\ufe0f The read-only/write keypair is missing, so there is nothing "
            "to install. Restart the bot to generate it.",
            "\u26a0\ufe0f Pasangan key read-only/write belum ada, jadi tidak ada yang "
            "bisa dipasang. Restart bot untuk membuatnya."))
        return

    msg = await update.message.reply_text(_t(lang,
        "\U0001f512 Securing every configured host\u2026",
        "\U0001f512 Mengamankan semua host terdaftar\u2026"))
    loop = asyncio.get_running_loop()
    lines: list[str] = []
    secured: list[tuple] = []
    for srv in servers:
        host, user = srv.get("host"), srv.get("user", "root")
        port = int(srv.get("port", 22))
        try:
            ok, detail = await loop.run_in_executor(None, secure_server, host, user, port)
        except Exception as exc:
            ok, detail = False, str(exc)
        mark = "\u2705" if ok else "\u274c"
        lines.append(f"{mark} <b>{_tg_escape(srv.get('name') or host)}</b>\n   {_tg_escape(detail)}")
        logger.warning("secure %s: %s (%s)", host, "OK" if ok else "FAILED", detail)
        if ok:
            secured.append((host, user, port))

    # Point ~/.ssh/config at the active-key symlink BEFORE retiring anything.
    # Until this runs, the legacy key is still the credential in use, and
    # removing it from a node would lock the agent out of the host it had just
    # secured. Only after this does lock/unlock actually swap what gets used.
    if secured:
        try:
            await loop.run_in_executor(None, _rebuild_ssh_config, servers)
            for host, user, port in secured:
                if await loop.run_in_executor(None, retire_legacy_key, host, user, port):
                    lines.append(_t(lang,
                        f"   \u21b3 retired the old unrestricted key on {_tg_escape(host)}",
                        f"   \u21b3 kunci lama tanpa batas di {_tg_escape(host)} dipensiunkan"))
        except Exception as exc:
            logger.exception("post-secure cleanup failed")
            lines.append(_t(lang,
                f"\u26a0\ufe0f cleanup incomplete: {_tg_escape(str(exc))}",
                f"\u26a0\ufe0f pembersihan belum tuntas: {_tg_escape(str(exc))}"))

    await msg.edit_text(
        _t(lang, "\U0001f512 <b>Guard status</b>\n\n", "\U0001f512 <b>Status guard</b>\n\n")
        + "\n".join(lines)
        + _t(lang,
             "\n\n<i>A green line means a write with the read-only key was actually "
             "attempted and refused -- not merely assumed. This does not reduce what the "
             "agent can do: after /unlock it holds an unrestricted key and can create, "
             "change and delete normally, until the window closes by itself.</i>",
             "\n\n<i>Baris hijau berarti percobaan tulis dengan key read-only benar-benar "
             "dilakukan dan ditolak -- bukan sekadar diasumsikan. Ini tidak mengurangi "
             "kemampuan agent: setelah /unlock ia memegang key tanpa batas dan bisa membuat, "
             "mengubah, serta menghapus seperti biasa, sampai jendelanya tertutup sendiri.</i>"),
        parse_mode="HTML")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliberately NOT gated by _authorized() -- its only job is to reveal IDs
    needed for setup (registering a group in ALLOWED_GROUP_IDS, or a person in
    ALLOWED_USER_IDS). It reveals no infra data and takes no action, so it's
    safe to leave open even to people not yet authorized for anything else."""
    chat = update.effective_chat
    user = update.effective_user
    lang = _chat_lang(update)
    kind = _t(lang,
        {"private": "Private DM", "group": "Group", "supergroup": "Supergroup", "channel": "Channel"},
        {"private": "DM pribadi", "group": "Grup", "supergroup": "Supergrup", "channel": "Channel"},
    ).get(chat.type, chat.type)
    lines = [
        f"\U0001f4cd Chat ID ({kind}): `{chat.id}`",
    ]
    if user:
        lines.append(_t(lang, f"\U0001f464 Your User ID: `{user.id}`", f"\U0001f464 User ID kamu: `{user.id}`"))
    if chat.type != "private":
        lines.append(_t(lang,
            "\nTo open this bot to EVERY member of this group, ask an admin to run "
            "`/registergroup` here (or send the Chat ID above to an admin).",
            "\nBuat buka akses bot ini ke SEMUA anggota grup ini, minta admin ketik "
            "`/registergroup` di grup ini (atau kirim Chat ID di atas ke admin).",
        ))
    else:
        lines.append(_t(lang,
            "\nTo request personal access (not via a group), send the User ID above "
            "to an admin to be added to `ALLOWED_USER_IDS`.",
            "\nBuat minta akses personal (bukan lewat grup), kirim User ID di atas "
            "ke admin buat ditambahkan ke `ALLOWED_USER_IDS`.",
        ))
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
    lang = _chat_lang(update)
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text(_t(lang,
            "This command is for groups, not a private DM -- run it inside the group you want to open access to.",
            "Command ini buat grup, bukan DM pribadi -- jalankan di dalam grup yang mau dibuka aksesnya.",
        ))
        return
    if chat.id in ALLOWED_GROUP_IDS:
        await update.message.reply_text(_t(lang,
            f"This group (`{chat.id}`) is already registered.",
            f"Grup ini (`{chat.id}`) sudah terdaftar sebelumnya.",
        ), parse_mode="Markdown")
        return
    ALLOWED_GROUP_IDS.add(chat.id)
    _save_allowed_groups_file(ALLOWED_GROUP_IDS)
    logger.info("group registered by admin: chat_id=%s title=%s", chat.id, chat.title)
    await update.message.reply_text(_t(lang,
        f"✅ Group *{chat.title or chat.id}* registered. Every member can now use this bot.",
        f"✅ Grup *{chat.title or chat.id}* terdaftar. Semua anggota sekarang bisa pakai bot ini.",
    ), parse_mode="Markdown")


async def cmd_unregistergroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only. Revokes a group's whole-group access; per-person entries in
    ALLOWED_USER_IDS (if any of that group's members were also individually
    whitelisted) are untouched."""
    if not _is_admin(update):
        return
    lang = _chat_lang(update)
    chat = update.effective_chat
    if chat.id not in ALLOWED_GROUP_IDS:
        await update.message.reply_text(_t(lang, "This group isn't registered.", "Grup ini belum/tidak terdaftar."))
        return
    ALLOWED_GROUP_IDS.discard(chat.id)
    _save_allowed_groups_file(ALLOWED_GROUP_IDS)
    logger.info("group unregistered by admin: chat_id=%s title=%s", chat.id, chat.title)
    await update.message.reply_text(_t(lang,
        f"Access revoked for group *{chat.title or chat.id}*.",
        f"Akses grup *{chat.title or chat.id}* dicabut.",
    ), parse_mode="Markdown")


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


def _effective_unlock_cap(update: Update) -> int:
    """The ceiling that applies to an /unlock opened from THIS chat -- the
    DM ceiling, or the shorter group one if this isn't a private chat."""
    chat = update.effective_chat
    if chat and chat.type != "private":
        return WRITE_MODE_GROUP_MAX_MINUTES
    return WRITE_MODE_MAX_MINUTES


async def _may_authorize_group_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Owner anywhere, or an admin of a REGISTERED group -- the same trust
    level already granted to /addserver. A PIN still confirms the actual
    action, so this only decides who may reach that PIN prompt. Used for
    scheduling and for /unlock; the two differ in how long a window /unlock
    grants when it's this path that opened it (see _effective_unlock_cap)."""
    if _is_owner(update):
        return True
    chat = update.effective_chat
    if not (chat and chat.type != "private" and chat.id in ALLOWED_GROUP_IDS):
        return False
    return await _is_group_admin(update, context)


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
    lang = _chat_lang(update)
    if not _authorized(update):
        await update.message.reply_text(_t(lang,
            "Sorry, you're not authorized to use this bot.",
            "Maaf, kamu tidak diizinkan pakai bot ini.",
        ))
        return
    if not _may_run_setup(update):
        await update.message.reply_text(_t(lang,
            "\U0001f44b I'm ready. Send anything to get started — status checks, "
            "investigations, reports.\n\nType /help for the full guide.",
            "\U0001f44b Saya siap. Kirim apa saja untuk mulai — cek status, "
            "investigasi, laporan.\n\nKetik /help untuk panduan lengkap.",
        ))
        return
    if not _is_owner(update) and not await _is_group_admin(update, context):
        await update.message.reply_text(_t(lang,
            "\U0001f44b I'm ready. Send anything to get started.\n\n"
            "Type /help for the guide. (Setup is limited to the bot owner and this "
            "group's admins.)",
            "\U0001f44b Saya siap. Kirim apa saja untuk mulai.\n\n"
            "Ketik /help untuk panduan. (Setup dibatasi untuk pemilik bot dan admin "
            "grup ini.)",
        ))
        return

    done = [d for _, d, _ in setup_summary()]
    if all(done) and not _is_owner(update):
        await update.message.reply_text(_t(lang,
            "✅ This bot is already set up by the owner.\n\nChange anything?",
            "✅ Bot ini sudah diatur oleh pemilik.\n\nMau ubah sesuatu?",
        ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(_t(lang, "Yes, change setup", "Ya, ubah setup"), callback_data="setup:menu"),
                InlineKeyboardButton(_t(lang, "No, leave it", "Tidak, biarkan"), callback_data="setup:close"),
            ]]),
        )
        return
    if str(update.effective_chat.id) not in _read_chat_languages():
        await update.message.reply_text(
            "\U0001f310 Which language should the bot's own replies use? Your own "
            "messages can be in either language regardless -- this only picks the "
            "bot's fixed text, like this wizard.\n\n"
            "Bahasa apa untuk balasan tetap bot? Pesan Anda sendiri boleh bahasa apa "
            "saja -- ini cuma pilih teks tetap bot, seperti wizard ini.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\U0001f1ee\U0001f1e9 Indonesia", callback_data="startlang:id"),
                InlineKeyboardButton("\U0001f1ec\U0001f1e7 English", callback_data="startlang:en"),
            ]]),
        )
        return
    await update.message.reply_text(_wizard_text(lang), parse_mode="HTML",
                                    reply_markup=_wizard_keyboard({}, lang))


async def cmd_start_lang_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _may_run_setup(update):
        await query.answer(_t(_chat_lang(update), "Not permitted.", "Tidak diizinkan."), show_alert=True)
        return
    await query.answer()
    _, choice = query.data.split(":", 1)
    chat_id = str(update.effective_chat.id)
    prefs = _read_chat_languages()
    prefs[chat_id] = choice
    _write_chat_languages(prefs)
    await query.edit_message_text(_wizard_text(choice), parse_mode="HTML",
                                  reply_markup=_wizard_keyboard({}, choice))


async def cmd_setup_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = _chat_lang(update)
    _, _, what = query.data.partition(":")
    if not _may_run_setup(update):
        await query.answer(_t(lang, "Not permitted.", "Tidak diizinkan."), show_alert=True)
        return
    if not _is_owner(update) and not await _is_group_admin(update, context):
        await query.answer(_t(lang, "Group admins only.", "Cuma admin grup."), show_alert=True)
        return
    await query.answer()

    if what == "close":
        await query.edit_message_text(_t(lang, "Setup closed. Run /start any time.",
                                              "Setup ditutup. Jalankan /start kapan saja."))
        return
    if what == "menu":
        await query.edit_message_text(_wizard_text(lang), parse_mode="HTML",
                                      reply_markup=_wizard_keyboard({}, lang))
        return
    if what == "pin":
        await _begin_new_pin(update, query) if not pin_is_set() else await request_pin(
            update, "change_pin_start", {}, _t(lang,
                "🔢 Changing the PIN. First, confirm the CURRENT one.",
                "🔢 Mengganti PIN. Konfirmasi dulu PIN yang SEKARANG.",
            ))
        return
    if what == "brief":
        _wizard[update.effective_chat.id] = {
            "step": "await_brief",
            "expires": _dt.datetime.now().timestamp() + WIZARD_TTL_SECONDS,
        }
        await query.edit_message_text(_t(lang,
            "\U0001f5fa <b>What should this agent look after?</b>\n\n"
            "One plain sentence is enough -- it goes into the agent's brief as-is.\n\n"
            "e.g. <code>a 7-node Proxmox cluster</code>\n"
            "e.g. <code>our Kubernetes staging cluster</code>\n"
            "e.g. <code>a fleet of 20 Ubuntu web servers</code>\n\n"
            "Send it as your next message. /cancel to stop.\n\n"
            "<i>How it reaches those machines comes later, from /addserver. What it must "
            "never touch comes from /addboundary.</i>",

            "\U0001f5fa <b>Agent ini mengurus apa?</b>\n\n"
            "Satu kalimat biasa sudah cukup -- langsung masuk ke brief agent apa adanya.\n\n"
            "contoh: <code>cluster Proxmox 7 node</code>\n"
            "contoh: <code>cluster Kubernetes staging kami</code>\n"
            "contoh: <code>20 server web Ubuntu</code>\n\n"
            "Kirim sebagai pesan berikutnya. /cancel untuk batal.\n\n"
            "<i>Cara menjangkau mesinnya diatur belakangan lewat /addserver. Apa yang tidak "
            "boleh disentuh lewat /addboundary.</i>",
        ), parse_mode="HTML")
        return
    if what in ("agy", "claude"):
        await _begin_cli_login(update, query, what)
        return


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear a provider's stored credentials so the next sign-in is genuinely
    fresh -- the fix for a sign-in that keeps reporting success without a
    real OAuth exchange ever happening (a stale or broken session), and the
    way to switch this deployment to a different account."""
    if not _may_run_setup(update):
        return
    lang = _chat_lang(update)
    if not _is_owner(update) and not await _is_group_admin(update, context):
        return await update.message.reply_text(_t(lang,
            "\U0001f512 Bot owner or a group admin only.",
            "\U0001f512 Cuma pemilik bot atau admin grup.",
        ))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(_t(lang, "Gemini (Antigravity)", "Gemini (Antigravity)"),
                              callback_data="logout:agy"),
         InlineKeyboardButton(_t(lang, "Claude Code", "Claude Code"), callback_data="logout:claude")],
        [InlineKeyboardButton(_t(lang, "✖️ Cancel", "✖️ Batal"),
                              callback_data="logout:cancel")],
    ])
    await update.message.reply_text(_t(lang,
        "\U0001f513 <b>Log out which one?</b>\n\n"
        "Clears its stored credentials so the NEXT sign-in is genuinely fresh. "
        "Use this if a sign-in keeps reporting “already signed in” without you "
        "actually doing anything, or if you're switching this deployment to a "
        "different account.",
        "\U0001f513 <b>Logout yang mana?</b>\n\n"
        "Menghapus kredensial tersimpannya supaya sign-in BERIKUTNYA benar-benar "
        "baru. Pakai ini kalau sign-in terus bilang “sudah sign-in” padahal "
        "Anda tidak melakukan apa-apa, atau kalau mau ganti ke akun lain.",
    ), reply_markup=kb, parse_mode="HTML")


async def cmd_logout_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = _chat_lang(update)
    if not _may_run_setup(update):
        await query.answer()
        return
    if not _is_owner(update) and not await _is_group_admin(update, context):
        await query.answer(_t(lang, "Not permitted.", "Tidak diizinkan."), show_alert=True)
        return
    await query.answer()
    _, _, which = query.data.partition(":")

    if which == "cancel":
        await query.edit_message_text(_t(lang, "✖️ Cancelled.", "✖️ Dibatalkan."))
        return

    if which == "agy":
        if logout_agy():
            await query.edit_message_text(_t(lang,
                "✅ <b>Logged out of Gemini (Antigravity).</b>\n\n"
                "Run /start and tap “Change Gemini (Antigravity)” to sign in "
                "again -- it will now show a real sign-in URL instead of reporting "
                "success from old state.",
                "✅ <b>Sudah logout dari Gemini (Antigravity).</b>\n\n"
                "Jalankan /start lalu tap “Ganti Gemini (Antigravity)” untuk "
                "sign-in lagi -- sekarang akan tampil URL sign-in sungguhan, bukan "
                "“sudah sign-in” dari state lama.",
            ), parse_mode="HTML")
        else:
            await query.edit_message_text(_t(lang,
                "ℹ️ Already signed out -- no stored Gemini credentials found.",
                "ℹ️ Sudah logout -- tidak ada kredensial Gemini tersimpan.",
            ))
        return

    if which == "claude":
        ok, detail = await logout_claude()
        if ok:
            await query.edit_message_text(_t(lang,
                "✅ <b>Logged out of Claude Code.</b>\n\n"
                "Run /start and tap “Set up Claude Code” to sign in again.",
                "✅ <b>Sudah logout dari Claude Code.</b>\n\n"
                "Jalankan /start lalu tap “Atur Claude Code” untuk sign-in lagi.",
            ), parse_mode="HTML")
        elif detail:
            await query.edit_message_text(_t(lang,
                f"⚠️ Logout failed:\n<pre>{_tg_escape(detail)}</pre>",
                f"⚠️ Logout gagal:\n<pre>{_tg_escape(detail)}</pre>",
            ), parse_mode="HTML")
        else:
            await query.edit_message_text(_t(lang,
                "ℹ️ Already signed out of Claude Code.",
                "ℹ️ Sudah logout dari Claude Code.",
            ))
        return

    logger.error("unknown /logout choice: %s", which)
    await query.edit_message_text(_t(lang, "⚠️ Internal error: unknown choice.",
                                          "⚠️ Error internal: pilihan tidak dikenal."))


async def _begin_cli_login(update: Update, query, provider: str) -> None:
    """Kick off a provider's OAuth and show the URL."""
    lang = _chat_lang(update)
    if not tmux_available():
        await query.edit_message_text(_t(lang,
            "⚠️ tmux isn't installed, and it's needed to drive the sign-in screen.\n"
            "Install it (<code>apt install tmux</code>) and try again.",
            "⚠️ tmux belum terpasang, padahal dibutuhkan untuk layar sign-in.\n"
            "Pasang dulu (<code>apt install tmux</code>) lalu coba lagi.",
        ), parse_mode="HTML")
        return

    if provider == "agy":
        cmd, human = [AGY_BIN], "Antigravity (Gemini)"
    else:
        cmd, human = [CLAUDE_BIN, "auth", "login"], "Claude Code"

    handle = LoginHandle(session=f"ismart-login-{provider}", command=cmd)
    await query.edit_message_text(_t(lang, f"⏳ Starting {human} sign-in…", f"⏳ Memulai sign-in {human}…"))
    try:
        handle.start()
        url = await asyncio.get_running_loop().run_in_executor(None, handle.wait_for_url, 45)
    except Exception as exc:
        logger.exception("login start failed for %s", provider)
        await query.edit_message_text(_t(lang, f"⚠️ Couldn't start the sign-in: {exc}",
                                              f"⚠️ Gagal mulai sign-in: {exc}"))
        return

    if url is None:
        screen = handle.pane()
        handle.kill()
        if LoginHandle.already_done(screen):
            _mark_setup(provider, update.effective_user.id)
            await query.edit_message_text(_t(lang,
                f"✅ {human} is already signed in.\n\nRun /start to see what's left.",
                f"✅ {human} sudah sign-in.\n\nJalankan /start untuk lihat sisanya."))
            return
        await query.edit_message_text(_t(lang,
            f"⚠️ Couldn't find a sign-in URL for {human}. Last output:\n\n"
            f"<pre>{_tg_escape(screen[-600:])}</pre>",
            f"⚠️ Tidak ketemu URL sign-in untuk {human}. Output terakhir:\n\n"
            f"<pre>{_tg_escape(screen[-600:])}</pre>",
        ), parse_mode="HTML")
        return

    _wizard[update.effective_chat.id] = {
        "step": f"await_code_{provider}",
        "handle": handle,
        "human": human,
        "expires": _dt.datetime.now().timestamp() + WIZARD_TTL_SECONDS,
    }
    # A bare URL in the message TEXT relies on Telegram's own auto-linkifier to
    # find and open it correctly (v0.2b.32 tried a real <a href> anchor instead
    # -- ALSO wrong: found live, with the exact broken URL in hand, that at
    # least one real Telegram client double-percent-encodes an already-encoded
    # href when the user taps it to launch a browser (redirect_uri went from
    # ...%3A%2F%2F... to ...%253A%252F%2F..., i.e. the literal "%" itself got
    # re-escaped to "%25"), which Google's OAuth server correctly rejects as
    # invalid_request since it no longer matches any registered redirect_uri).
    # Both a bare auto-linked URL and an <a href> ultimately hand the same
    # string to the same "launch this URL" platform call -- tapping either is
    # the vulnerable path. A <code> block sidesteps it entirely: Telegram
    # treats it as literal text to COPY, not a link to open, so what reaches
    # the clipboard -- and then the browser's own address bar, pasted by the
    # human -- is the untouched, single-encoded string agy actually printed.
    safe_url = _tg_escape(url)
    await query.edit_message_text(_t(lang,
        f"🔗 <b>Sign in to {human}</b>\n\n"
        f"1. Copy this link (tap it to copy) and open it in a browser:\n<code>{safe_url}</code>\n\n"
        "2. Approve it, copy the code you get back.\n"
        "3. <b>Send that code here as your next message.</b>\n\n"
        "<i>The code is single-use and expires quickly, which is why it's safe to "
        "paste in chat — unlike a password or an SSH key, which I'll never ask for. "
        "Copy the link rather than tapping to open it directly -- some Telegram clients "
        "mangle a long URL when launching a browser straight from a tap.</i>\n\n"
        "Send /cancel to stop.",
        f"🔗 <b>Sign in ke {human}</b>\n\n"
        f"1. Salin link ini (tap untuk menyalin) dan buka di browser:\n<code>{safe_url}</code>\n\n"
        "2. Setujui, salin kode yang muncul.\n"
        "3. <b>Kirim kode itu di sini sebagai pesan berikutnya.</b>\n\n"
        "<i>Kodenya sekali-pakai dan cepat kedaluwarsa, makanya aman ditempel "
        "di chat — beda dengan password atau SSH key, yang tidak akan pernah saya minta. "
        "Salin link-nya, jangan langsung tap untuk membuka -- beberapa client Telegram "
        "merusak URL panjang saat membuka browser langsung dari tap.</i>\n\n"
        "Kirim /cancel untuk berhenti.",
    ), parse_mode="HTML", disable_web_page_preview=True)


def _gdrive_client_setup_instructions(lang: str) -> str:
    return _t(lang,
        "🔑 <b>One-time setup: your own Google OAuth client</b>\n\n"
        "Asked once per deployment, never again. It stays on this server and "
        "is never committed anywhere — like the bot token.\n\n"
        "1. Open <code>https://console.cloud.google.com/apis/credentials</code>\n"
        "2. Create a project (any name), then <b>Create credentials → OAuth client ID</b>\n"
        "3. Application type: <b>TV and Limited Input devices</b>\n"
        "   <i>(this exact type — Desktop or Web will be rejected)</i>\n"
        "4. Enable the <b>Google Drive API</b> for that project\n"
        "5. On the OAuth consent screen, press <b>Publish app</b>\n"
        "   <i>(no Google review needed — this only asks for drive.file, a "
        "non-sensitive scope. Skipping this leaves the app in Testing, where "
        "Google expires the login every 7 days.)</i>\n\n"
        "Then send both values here as one message, separated by a space:\n"
        "<code>&lt;client_id&gt; &lt;client_secret&gt;</code>\n\n"
        "Send /cancel to stop.",
        "🔑 <b>Setup sekali saja: OAuth client milik Anda sendiri</b>\n\n"
        "Ditanya sekali per deployment, tidak akan diminta lagi. Tersimpan di "
        "server ini dan tidak pernah ikut ke mana pun — seperti token bot.\n\n"
        "1. Buka <code>https://console.cloud.google.com/apis/credentials</code>\n"
        "2. Buat project (nama bebas), lalu <b>Create credentials → OAuth client ID</b>\n"
        "3. Application type: <b>TV and Limited Input devices</b>\n"
        "   <i>(harus tipe ini — Desktop atau Web akan ditolak)</i>\n"
        "4. Aktifkan <b>Google Drive API</b> untuk project itu\n"
        "5. Di OAuth consent screen, tekan <b>Publish app</b>\n"
        "   <i>(tidak perlu review Google — ini cuma minta drive.file yang "
        "non-sensitive. Kalau dilewati, app tetap berstatus Testing dan Google "
        "mematikan login-nya tiap 7 hari.)</i>\n\n"
        "Lalu kirim kedua nilainya di sini dalam satu pesan, dipisah spasi:\n"
        "<code>&lt;client_id&gt; &lt;client_secret&gt;</code>\n\n"
        "Kirim /cancel untuk berhenti.",
    )


async def _gdrive_begin_device(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               lang: str, name: str) -> None:
    """Ask Google for a device code, show the card, and start waiting.

    The wait is a bounded, deterministic poll of a request THIS person just
    made, with a hard deadline Google itself sets (expires_in) -- the same
    shape as the existing sign-in waits, not a background agent deciding
    anything on its own.
    """
    chat_id = update.effective_chat.id
    loop = asyncio.get_running_loop()
    ok, body = await loop.run_in_executor(None, gdrive_device_start)
    if not ok:
        _gdrive_wizard.pop(chat_id, None)
        detail = str(body.get("error_description", "unknown"))
        return await update.message.reply_text(_t(lang,
            f"⚠️ Google refused to start the sign-in: {detail}",
            f"⚠️ Google menolak memulai sign-in: {detail}",
        ))

    url = body.get("verification_url") or body.get("verification_uri") or "https://google.com/device"
    code = body.get("user_code", "")
    mins = max(1, int(body.get("expires_in", 900)) // 60)
    state = _gdrive_wizard.get(chat_id)
    if state is not None:
        state["step"] = "device_pending"
    await update.message.reply_text(_t(lang,
        f"🔗 <b>Connect Google Drive</b> (<code>{_tg_escape(name)}</code>)\n\n"
        f"1. Open <code>{_tg_escape(url)}</code> on any device — your phone is fine\n"
        f"2. Enter this code:\n\n<code>{_tg_escape(code)}</code>\n\n"
        "3. Approve access. I pick it up automatically — nothing to paste back.\n\n"
        f"<i>The code is good for about {mins} minutes. No terminal needed.</i>\n\n"
        "Send /cancel to stop.",
        f"🔗 <b>Hubungkan Google Drive</b> (<code>{_tg_escape(name)}</code>)\n\n"
        f"1. Buka <code>{_tg_escape(url)}</code> di perangkat apa pun — HP juga bisa\n"
        f"2. Masukkan kode ini:\n\n<code>{_tg_escape(code)}</code>\n\n"
        "3. Setujui aksesnya. Hasilnya saya ambil sendiri — tidak ada yang perlu ditempel balik.\n\n"
        f"<i>Kodenya berlaku sekitar {mins} menit. Tidak perlu terminal.</i>\n\n"
        "Kirim /cancel untuk berhenti.",
    ), parse_mode="HTML", disable_web_page_preview=True)

    asyncio.create_task(_gdrive_device_wait(context, chat_id, name, lang, body))


async def _gdrive_device_wait(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                              name: str, lang: str, body: dict) -> None:
    """Poll until approved, refused, or the code dies. Never runs longer than
    Google's own expires_in, and stops the moment the wizard is cancelled."""
    device_code = body.get("device_code", "")
    interval = max(5, int(body.get("interval", 5)))
    deadline = _dt.datetime.now().timestamp() + int(body.get("expires_in", 900))
    loop = asyncio.get_running_loop()

    async def done(text: str) -> None:
        _gdrive_wizard.pop(chat_id, None)
        try:
            await context.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            logger.warning("could not report the Drive sign-in result", exc_info=True)

    while _dt.datetime.now().timestamp() < deadline:
        await asyncio.sleep(interval)
        # Cancelled, or superseded by another /connectgdrive: stop quietly.
        st = _gdrive_wizard.get(chat_id)
        if not st or st.get("step") != "device_pending":
            return
        try:
            state, payload = await loop.run_in_executor(
                None, gdrive_device_poll_once, device_code)
        except Exception:
            logger.warning("Drive device poll failed", exc_info=True)
            continue

        if state == "pending":
            continue
        if state == "slow_down":
            # Google asking to back off is not an error -- honour it, or the
            # next few polls get rejected outright.
            interval += 5
            continue
        if state == "denied":
            return await done(_t(lang, "✖️ Access was declined in the browser.",
                                       "✖️ Aksesnya ditolak di browser."))
        if state == "expired":
            return await done(_t(lang,
                "⌛ That code expired. Run /connectgdrive again.",
                "⌛ Kodenya kedaluwarsa. Jalankan /connectgdrive lagi."))
        if state == "error":
            detail = _tg_escape(str(payload.get("error_description")
                                    or payload.get("error") or "unknown"))
            return await done(_t(lang, f"⚠️ Sign-in failed: {detail}",
                                       f"⚠️ Sign-in gagal: {detail}"))

        # Approved. A token with no refresh_token would work for an hour and
        # then quietly stop -- Google omits it when this account has already
        # authorised this same client before, so say exactly that rather than
        # storing something with an hour to live.
        if not payload.get("refresh_token"):
            return await done(_t(lang,
                "⚠️ Google returned no refresh token, so this would stop working "
                "within the hour. That usually means this account already "
                "authorised this client -- revoke it at "
                "myaccount.google.com/permissions and try again.",
                "⚠️ Google tidak mengirim refresh token, jadi ini akan berhenti "
                "bekerja dalam sejam. Biasanya karena akun ini sudah pernah "
                "mengizinkan client yang sama -- cabut di "
                "myaccount.google.com/permissions lalu coba lagi."))

        # Hand the token to the SAME path a pasted token took, so verification,
        # the duplicate-root-folder guard and rollback behave identically
        # however the token was obtained.
        ok, detail = await loop.run_in_executor(
            None, connect_gdrive_account, name, gdrive_token_to_rclone(payload))
        safe = _tg_escape(str(detail))
        if ok:
            logger.warning("Drive account connected via device flow: %s", name)
            return await done(_t(lang,
                f"✅ <b>{_tg_escape(name)}</b> connected. {safe}",
                f"✅ <b>{_tg_escape(name)}</b> terhubung. {safe}"))
        return await done(_t(lang, f"⚠️ Could not finish: {safe}",
                                   f"⚠️ Tidak bisa menyelesaikan: {safe}"))

    await done(_t(lang, "⌛ Timed out waiting for approval. Run /connectgdrive again.",
                        "⌛ Kelamaan menunggu persetujuan. Jalankan /connectgdrive lagi."))


async def _handle_gdrive_wizard_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """A separate small wizard from _wizard/_server_wizard -- its two steps
    (label, then token) don't overlap either of those in shape, and keeping
    it apart means a stuck gdrive connect can't wedge sign-in or /addserver."""
    chat_id = update.effective_chat.id
    state = _gdrive_wizard.get(chat_id)
    if not state:
        return False
    lang = _chat_lang(update)
    text = (update.message.text or "").strip()

    if state["expires"] < _dt.datetime.now().timestamp():
        _gdrive_wizard.pop(chat_id, None)
        await update.message.reply_text(_t(lang,
            "\u231b That expired. Run /connectgdrive again.",
            "\u231b Sudah kedaluwarsa. Jalankan /connectgdrive lagi."))
        return True
    if text.lower() in ("/cancel", "cancel", "batal"):
        _gdrive_wizard.pop(chat_id, None)
        await update.message.reply_text(_t(lang, "\u2716\ufe0f Cancelled.", "\u2716\ufe0f Dibatalkan."))
        return True

    if state["step"] == "await_gdrive_label":
        name = _sanitize_gdrive_label(text)
        if not name or name == "gdrive_":
            await update.message.reply_text(_t(lang,
                "That didn't leave anything usable -- letters/digits/-/_ only, "
                "try again or /cancel.",
                "Tidak ada yang tersisa dari itu -- huruf/angka/-/_ saja, "
                "coba lagi atau /cancel."))
            return True
        if name in _list_gdrive_accounts():
            await update.message.reply_text(_t(lang,
                f"'{_tg_escape(name)}' already exists -- pick a different label, or /cancel.",
                f"'{_tg_escape(name)}' sudah ada -- pilih label lain, atau /cancel."))
            return True
        state["name"] = name
        state["expires"] = _dt.datetime.now().timestamp() + GDRIVE_TOKEN_WIZARD_TTL
        if read_gdrive_client():
            await _gdrive_begin_device(update, context, lang, name)
        else:
            state["step"] = "await_gdrive_client"
            await update.message.reply_text(
                _gdrive_client_setup_instructions(lang), parse_mode="HTML")
        return True

    if state["step"] == "await_gdrive_client":
        parts = text.split()
        if len(parts) != 2 or not parts[0].endswith(".apps.googleusercontent.com"):
            await update.message.reply_text(_t(lang,
                "Send the client id and secret as one message, separated by a "
                "space. The id ends in .apps.googleusercontent.com — or /cancel.",
                "Kirim client id dan secret dalam satu pesan, dipisah spasi. "
                "Id-nya berakhiran .apps.googleusercontent.com — atau /cancel."))
            return True
        write_gdrive_client(parts[0], parts[1])
        state["expires"] = _dt.datetime.now().timestamp() + GDRIVE_TOKEN_WIZARD_TTL
        await _gdrive_begin_device(update, context, lang, state["name"])
        return True

    if state["step"] == "device_pending":
        # The waiter owns this step; anything typed here is just noise.
        await update.message.reply_text(_t(lang,
            "Still waiting for you to approve it in the browser. /cancel to stop.",
            "Masih menunggu Anda menyetujuinya di browser. /cancel untuk berhenti."))
        return True

    # step == "await_gdrive_token"
    name = state["name"]
    _gdrive_wizard.pop(chat_id, None)
    try:
        await update.message.delete()
    except Exception:
        logger.info("could not delete the pasted gdrive token (needs admin rights in groups)")
    await update.message.reply_text(_t(lang,
        f"\u23f3 Connecting and verifying \u2018{_tg_escape(name)}\u2019\u2026",
        f"\u23f3 Menghubungkan dan memverifikasi \u2018{_tg_escape(name)}\u2019\u2026"))
    loop = asyncio.get_running_loop()
    try:
        ok, detail = await loop.run_in_executor(None, connect_gdrive_account, name, text)
    except Exception as exc:
        logger.exception("gdrive connect failed")
        ok, detail = False, str(exc)
    if ok:
        logger.warning("gdrive account '%s' connected by user=%s chat=%s",
                       name, update.effective_user.id, chat_id)
        await update.message.reply_text(_t(lang,
            f"\u2705 <b>{_tg_escape(name)}</b> connected and verified -- a real upload "
            f"and folder check both succeeded, not just \u201csaved\u201d.\n\n"
            f"Run /gdrive to pick it for this room.",
            f"\u2705 <b>{_tg_escape(name)}</b> terhubung dan terverifikasi -- upload dan "
            f"cek folder sungguhan berhasil, bukan sekadar \u201ctersimpan\u201d.\n\n"
            f"Jalankan /gdrive untuk pilih ini di room ini.",
        ), parse_mode="HTML")
    else:
        logger.warning("gdrive connect '%s' failed for user=%s: %s",
                       name, update.effective_user.id, detail)
        await update.message.reply_text(_t(lang,
            f"\u274c Couldn't connect \u2018{_tg_escape(name)}\u2019: {_tg_escape(detail)}\n\n"
            "Nothing was left half-configured. Run /connectgdrive to try again.",
            f"\u274c Gagal menghubungkan \u2018{_tg_escape(name)}\u2019: {_tg_escape(detail)}\n\n"
            "Tidak ada yang tertinggal setengah-jadi. Jalankan /connectgdrive untuk coba lagi.",
        ))
    return True


async def _handle_wizard_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If this chat is mid-wizard, treat the message as the OAuth code.
    Returns True when the message was consumed and must not reach the model."""
    chat_id = update.effective_chat.id
    if await _handle_gdrive_wizard_input(update, context):
        return True
    state = _wizard.get(chat_id)
    if not state:
        return False
    lang = _chat_lang(update)

    if state.get("step") == "await_brief":
        _wizard.pop(chat_id, None)
        if state["expires"] < _dt.datetime.now().timestamp():
            await update.message.reply_text(_t(lang,
                "\u231b That expired. Run /start again.",
                "\u231b Sudah kedaluwarsa. Jalankan /start lagi.",
            ))
            return True
        role = (update.message.text or "").strip()
        if role.lower() in ("/cancel", "cancel", "batal"):
            await update.message.reply_text(_t(lang, "\u2716\ufe0f Cancelled.", "\u2716\ufe0f Dibatalkan."))
            return True
        if len(role) < 3:
            await update.message.reply_text(_t(lang,
                "That is too short to be useful. Run /start and try again.",
                "Terlalu pendek untuk berguna. Jalankan /start dan coba lagi.",
            ))
            return True
        set_brief_role(role)
        _mark_setup("brief", update.effective_user.id)
        await update.message.reply_text(_t(lang,
            f"\u2705 <b>Recorded.</b> This agent looks after: <b>{_tg_escape(role)}</b>\n\n"
            "Next: /addserver to give it a machine to reach, and /addboundary for "
            "anything it must never touch. Run /start if anything else still needs "
            "setting up.\n\n"
            "<i>Takes effect on the next new conversation -- /new applies it now.</i>",
            f"\u2705 <b>Tercatat.</b> Agent ini mengurus: <b>{_tg_escape(role)}</b>\n\n"
            "Berikutnya: /addserver untuk memberi mesin yang bisa dijangkau, dan "
            "/addboundary untuk hal yang tidak boleh disentuh. Jalankan /start kalau "
            "masih ada yang perlu diatur.\n\n"
            "<i>Berlaku di percakapan baru berikutnya -- /new untuk langsung terapkan.</i>",
        ), parse_mode="HTML")
        return True

    if state["expires"] < _dt.datetime.now().timestamp():
        _wizard.pop(chat_id, None)
        state["handle"].kill()
        await update.message.reply_text(_t(lang,
            "⌛ That sign-in expired. Run /start to try again.",
            "⌛ Sign-in itu sudah kedaluwarsa. Jalankan /start untuk coba lagi.",
        ))
        return True

    text = (update.message.text or "").strip()
    if text.lower() in ("/cancel", "cancel", "batal"):
        _wizard.pop(chat_id, None)
        state["handle"].kill()
        await update.message.reply_text(_t(lang, "✖️ Sign-in cancelled.", "✖️ Sign-in dibatalkan."))
        return True

    handle, human = state["handle"], state["human"]
    _wizard.pop(chat_id, None)
    await update.message.reply_text(_t(lang, f"⏳ Sending the code to {human}…", f"⏳ Mengirim kode ke {human}…"))

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
        await update.message.reply_text(_t(lang, f"⚠️ Sign-in failed: {exc}", f"⚠️ Sign-in gagal: {exc}"))
        return True
    handle.kill()

    if ok:
        provider = "agy" if "Antigravity" in human else "claude"
        _mark_setup(provider, update.effective_user.id)
        logger.warning("%s sign-in completed by user=%s", human, update.effective_user.id)
        await update.message.reply_text(_t(lang,
            f"✅ <b>{human} signed in.</b>\n\nRun /start to see what's left.",
            f"✅ <b>{human} sudah sign-in.</b>\n\nJalankan /start untuk lihat sisanya.",
        ), parse_mode="HTML")
    else:
        await update.message.reply_text(_t(lang,
            f"⚠️ {human} didn't accept that code. It may have expired — codes are "
            f"short-lived, so grabbing a fresh one usually fixes it.\n\n"
            f"<pre>{_tg_escape(screen[-500:])}</pre>\n\nRun /start to retry.",
            f"⚠️ {human} tidak menerima kode itu. Mungkin sudah kedaluwarsa — kodenya "
            f"cepat basi, biasanya ambil yang baru langsung beres.\n\n"
            f"<pre>{_tg_escape(screen[-500:])}</pre>\n\nJalankan /start untuk coba lagi.",
        ), parse_mode="HTML")
    return True


def _update_card(lang: str, info: dict) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Shared by /update and the automatic notice, so both read identically."""
    if not info["ok"] and info["detail"] == "not-a-checkout":
        return _t(lang,
            "\U0001f4e6 <b>This deployment cannot update itself.</b>\n\n"
            "It was installed by copying files, not with <code>git clone</code>, so "
            "there is no remote to compare against.\n\n"
            f"Running: <b>{_tg_escape(info['current'])}</b>\n\n"
            "<i>To enable updates, re-deploy it as a git clone of the repository -- "
            "your .env, briefs and saved state are not tracked by git and would be "
            "kept.</i>",

            "\U0001f4e6 <b>Deployment ini tidak bisa update sendiri.</b>\n\n"
            "Dipasang dengan menyalin file, bukan <code>git clone</code>, jadi tidak ada "
            "remote untuk dibandingkan.\n\n"
            f"Sedang jalan: <b>{_tg_escape(info['current'])}</b>\n\n"
            "<i>Supaya bisa update, pasang ulang sebagai git clone dari repository -- "
            ".env, brief, dan state tersimpan tidak dilacak git, jadi tetap aman.</i>",
        ), None

    if not info["ok"]:
        return _t(lang,
            f"\u26a0\ufe0f Couldn't reach the repository:\n<pre>{_tg_escape(info['detail'])}</pre>",
            f"\u26a0\ufe0f Tidak bisa menghubungi repository:\n<pre>{_tg_escape(info['detail'])}</pre>",
        ), None

    if not info["has_update"]:
        return _t(lang,
            f"\u2705 <b>Already up to date.</b>\n\nRunning <b>{_tg_escape(info['current'])}</b>.",
            f"\u2705 <b>Sudah versi terbaru.</b>\n\nSedang jalan <b>{_tg_escape(info['current'])}</b>.",
        ), None

    if info["detail"] == "diverged":
        return _t(lang,
            f"\u26a0\ufe0f <b>Local changes would be lost.</b>\n\n"
            f"This copy has commits the repository does not, so it cannot be "
            f"fast-forwarded to <b>{_tg_escape(info['latest'])}</b>.\n\n"
            "<i>Nothing was changed. Sort it out on the host -- refusing is safer than "
            "throwing away whatever those commits were.</i>",

            f"\u26a0\ufe0f <b>Perubahan lokal bisa hilang.</b>\n\n"
            f"Salinan ini punya commit yang tidak ada di repository, jadi tidak bisa "
            f"di-fast-forward ke <b>{_tg_escape(info['latest'])}</b>.\n\n"
            "<i>Tidak ada yang diubah. Selesaikan dulu di host -- menolak lebih aman "
            "daripada membuang commit itu begitu saja.</i>",
        ), None

    changes = update_changelog()
    body = f"\n\n<pre>{_tg_escape(changes)}</pre>" if changes else ""
    n = info["behind"]
    text = _t(lang,
        f"\U0001f4e6 <b>An update is available.</b>\n\n"
        f"Running:  <b>{_tg_escape(info['current'])}</b>\n"
        f"Latest:   <b>{_tg_escape(info['latest'])}</b>\n"
        f"{n} commit(s) behind.{body}\n\n"
        "Updating pulls the new code, checks it compiles, and restarts the bot. "
        "It confirms here once the new version is actually running.",

        f"\U0001f4e6 <b>Ada update tersedia.</b>\n\n"
        f"Sekarang: <b>{_tg_escape(info['current'])}</b>\n"
        f"Terbaru:  <b>{_tg_escape(info['latest'])}</b>\n"
        f"Tertinggal {n} commit.{body}\n\n"
        "Update akan menarik kode baru, memastikan bisa dikompilasi, lalu restart bot. "
        "Konfirmasinya muncul di sini begitu versi baru benar-benar jalan.",
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(_t(lang, "\u2b07\ufe0f Update now", "\u2b07\ufe0f Update sekarang"),
                             callback_data="upd:yes"),
        InlineKeyboardButton(_t(lang, "\u2716\ufe0f Not now", "\u2716\ufe0f Nanti saja"),
                             callback_data="upd:no"),
    ]])
    return text, kb


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what this deployment runs vs what the repository has."""
    if not await _may_authorize_group_action(update, context):
        return
    lang = _chat_lang(update)
    await update.message.reply_text(_t(lang, "\U0001f50e Checking\u2026", "\U0001f50e Mengecek\u2026"))
    info = await asyncio.get_running_loop().run_in_executor(None, check_for_update)
    st = _read_update_state()
    st["last_check"] = _dt.datetime.now().timestamp()
    if info.get("latest"):
        st["seen_version"] = info["latest"]
    _write_update_state(st)
    text, kb = _update_card(lang, info)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cmd_update_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = _chat_lang(update)
    if not await _may_authorize_group_action(update, context):
        await query.answer(_t(lang, "Not permitted.", "Tidak diizinkan."), show_alert=True)
        return
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "no":
        await query.edit_message_text(_t(lang,
            "\u2716\ufe0f Left as is. Run /update whenever you want it.",
            "\u2716\ufe0f Dibiarkan. Jalankan /update kapan pun mau.",
        ))
        return
    # Replacing the code the bot runs is a bigger capability than /unlock, which
    # only widens an SSH credential for minutes. A tap from a signed-in device
    # is not enough on its own.
    await query.edit_message_text(_t(lang,
        "\U0001f4e6 Updating \u2014 confirm with your PIN.",
        "\U0001f4e6 Update \u2014 konfirmasi dengan PIN.",
    ))
    await request_pin(update, "update", {}, _t(lang,
        "\U0001f4e6 Confirm updating this bot.",
        "\U0001f4e6 Konfirmasi update bot ini.",
    ))


async def cmd_setbrief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Say what this agent looks after, without going through /start."""
    if not _may_run_setup(update):
        return
    lang = _chat_lang(update)
    if not _is_owner(update) and not await _is_group_admin(update, context):
        return await update.message.reply_text(_t(lang,
            "\U0001f512 Bot owner or a group admin only.",
            "\U0001f512 Cuma pemilik bot atau admin grup.",
        ))
    role = " ".join(context.args).strip() if context.args else ""
    if len(role) < 3:
        current = ""
        if brief_configured():
            try:
                first = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").split("\n", 1)[0]
                current = _t(lang, f"\n\nRight now: {first.strip()}", f"\n\nSekarang: {first.strip()}")
            except OSError:
                pass
        return await update.message.reply_text(_t(lang,
            "\U0001f5fa <b>What should this agent look after?</b>\n\n"
            "<code>/setbrief a 7-node Proxmox cluster</code>\n"
            "<code>/setbrief our Kubernetes staging cluster</code>\n\n"
            "<i>How it reaches those machines comes from /addserver; what it must never "
            "touch comes from /addboundary.</i>" + current,

            "\U0001f5fa <b>Agent ini mengurus apa?</b>\n\n"
            "<code>/setbrief cluster Proxmox 7 node</code>\n"
            "<code>/setbrief cluster Kubernetes staging kami</code>\n\n"
            "<i>Cara menjangkau mesinnya dari /addserver; apa yang tidak boleh disentuh "
            "dari /addboundary.</i>" + current,
        ), parse_mode="HTML")
    set_brief_role(role)
    _mark_setup("brief", update.effective_user.id)
    await update.message.reply_text(_t(lang,
        f"\u2705 <b>Recorded.</b> This agent looks after: <b>{_tg_escape(role)}</b>\n\n"
        "Run /start if anything else still needs setting up.\n\n"
        "<i>Takes effect on the next new conversation -- /new applies it now.</i>",
        f"\u2705 <b>Tercatat.</b> Agent ini mengurus: <b>{_tg_escape(role)}</b>\n\n"
        "Jalankan /start kalau masih ada yang perlu diatur.\n\n"
        "<i>Berlaku di percakapan baru berikutnya -- /new untuk langsung terapkan.</i>",
    ), parse_mode="HTML")


async def cmd_setscope(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change what KIND of assistant this is -- not what it looks after
    (/setbrief), the role word itself: "You are a(n) ___ assistant"."""
    if not _may_run_setup(update):
        return
    lang = _chat_lang(update)
    if not _is_owner(update) and not await _is_group_admin(update, context):
        return await update.message.reply_text(_t(lang,
            "\U0001f512 Bot owner or a group admin only.",
            "\U0001f512 Cuma pemilik bot atau admin grup.",
        ))
    scope = " ".join(context.args).strip() if context.args else ""
    if len(scope) < 3:
        current = brief_scope()
        current_note = _t(lang, f"\n\nRight now: {current}", f"\n\nSekarang: {current}") if current else ""
        return await update.message.reply_text(_t(lang,
            "\U0001f3af <b>What KIND of assistant should this be?</b>\n\n"
            "This changes the role itself -- \"You are a(n) ___ assistant\" -- not what "
            "it looks after, which is /setbrief.\n\n"
            "<code>/setscope infrastructure</code> (the default)\n"
            "<code>/setscope general-purpose, with strong infrastructure skills</code>\n\n"
            "<i>Broadening this means it will also try to help with things that have "
            "nothing to do with infrastructure -- weigh that against the token-efficiency "
            "this project is built around.</i>" + current_note,

            "\U0001f3af <b>Ini agent jenis apa?</b>\n\n"
            "Ini mengubah perannya sendiri -- \"You are a(n) ___ assistant\" -- bukan apa "
            "yang diurusnya, itu /setbrief.\n\n"
            "<code>/setscope infrastructure</code> (bawaan)\n"
            "<code>/setscope general-purpose, with strong infrastructure skills</code>\n\n"
            "<i>Memperluas ini artinya dia juga akan mencoba membantu hal yang sama sekali "
            "tidak berhubungan dengan infrastruktur -- pertimbangkan itu terhadap prinsip "
            "efisiensi token yang jadi dasar proyek ini.</i>" + current_note,
        ), parse_mode="HTML")
    if not set_brief_scope(scope):
        return await update.message.reply_text(_t(lang,
            "\u26a0\ufe0f Couldn't find the opening sentence to rewrite in the brief -- "
            "it may have been hand-edited away from the template shape. Nothing changed.",
            "\u26a0\ufe0f Tidak ketemu kalimat pembuka yang bisa diubah di brief -- "
            "mungkin sudah diedit manual dan bentuknya beda dari template. Tidak ada yang diubah.",
        ))
    await update.message.reply_text(_t(lang,
        f"\u2705 <b>Recorded.</b> This is now a(n) <b>{_tg_escape(scope)}</b> assistant.\n\n"
        "<i>Takes effect on the next new conversation -- /new applies it now.</i>",
        f"\u2705 <b>Tercatat.</b> Sekarang jadi agent <b>{_tg_escape(scope)}</b>.\n\n"
        "<i>Berlaku di percakapan baru berikutnya -- /new untuk langsung terapkan.</i>",
    ), parse_mode="HTML")


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}k"
    return str(n)


async def cmd_spend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What this deployment actually spent, read straight from the ledger.
    Zero model tokens -- it is arithmetic over a file, not a question for a
    model. `/spend 7` for the last 7 days (default 1, i.e. today-ish)."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    try:
        days = max(1, min(90, int(context.args[0]))) if context.args else 1
    except (ValueError, IndexError):
        days = 1

    rows = _ledger_read(days)
    if not rows:
        return await update.message.reply_text(_t(lang,
            f"\U0001f4b8 No turns recorded in the last {days} day(s).\n\n"
            "<i>The ledger starts filling from the first turn after this version "
            "was installed -- it cannot show history from before it existed.</i>",
            f"\U0001f4b8 Belum ada giliran tercatat dalam {days} hari terakhir.\n\n"
            "<i>Ledger mulai terisi dari giliran pertama setelah versi ini dipasang "
            "-- riwayat sebelum ini memang tidak ada datanya.</i>",
        ), parse_mode="HTML")

    this_chat = str(update.effective_chat.id)
    mine = [r for r in rows if str(r.get("chat")) == this_chat]

    def totals(rs):
        return (sum(_int(r.get("total")) for r in rs),
                sum(_int(r.get("wasted")) for r in rs),
                sum(r.get("cost_usd") or 0 for r in rs),
                len(rs))

    a_tok, a_waste, a_cost, a_n = totals(rows)
    m_tok, m_waste, m_cost, m_n = totals(mine)

    by_model: dict[str, list] = {}
    for r in rows:
        by_model.setdefault(r.get("label") or r.get("model") or "?", []).append(r)

    lines = [_t(lang,
        f"\U0001f4b8 <b>Spend, last {days} day(s)</b>",
        f"\U0001f4b8 <b>Pemakaian, {days} hari terakhir</b>"), ""]
    lines.append(_t(lang,
        f"<b>All chats:</b> {_fmt_tok(a_tok)} tokens over {a_n} turn(s)",
        f"<b>Semua chat:</b> {_fmt_tok(a_tok)} token dalam {a_n} giliran"))
    if a_waste:
        lines.append(_t(lang,
            f"   ⚠️ wasted on failed tiers: <b>{_fmt_tok(a_waste)}</b> "
            f"({a_waste*100//max(a_tok+a_waste,1)}%)",
            f"   ⚠️ terbuang di tier gagal: <b>{_fmt_tok(a_waste)}</b> "
            f"({a_waste*100//max(a_tok+a_waste,1)}%)"))
    if a_cost:
        lines.append(f"   \U0001f4b5 ${a_cost:.2f}")
    lines.append("")
    lines.append(_t(lang,
        f"<b>This chat:</b> {_fmt_tok(m_tok)} tokens over {m_n} turn(s)",
        f"<b>Chat ini:</b> {_fmt_tok(m_tok)} token dalam {m_n} giliran"))
    if m_waste:
        lines.append(_t(lang, f"   ⚠️ wasted: {_fmt_tok(m_waste)}",
                              f"   ⚠️ terbuang: {_fmt_tok(m_waste)}"))
    lines.append("")
    lines.append(_t(lang, "<b>By tier</b> (all chats):", "<b>Per tier</b> (semua chat):"))
    for label, rs in sorted(by_model.items(), key=lambda kv: -sum(_int(r.get("total")) for r in kv[1])):
        t, w, c, n = totals(rs)
        extra = _t(lang, f", {_fmt_tok(w)} wasted", f", {_fmt_tok(w)} terbuang") if w else ""
        cost = f", ${c:.2f}" if c else ""
        lines.append(f"  • {_tg_escape(label)}: {_fmt_tok(t)} / {n}x{extra}{cost}")

    lines.append("")
    lines.append(_t(lang,
        "<i>0 model tokens -- read from the ledger, not asked to a model.</i>",
        "<i>0 token model -- dibaca dari ledger, bukan ditanyakan ke model.</i>"))
    logger.info("spend command served (0 model tokens, days=%d)", days)
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# MCP is a wire protocol (JSON-RPC over stdio), not tied to any language --
# this stdlib default satisfies it fully (verified live against the real
# claude CLI: a genuine tools/list + tools/call round-trip, permission
# granted, zero denials), so it needs nothing installed beyond the python3
# this whole project already requires. A server that only ships via npm
# still works the same way (see README "MCP servers") -- Node.js just isn't
# something this installs for you unasked.
_MCP_EXAMPLE = "python3 tools/mcp_readonly_fs.py <folder>"


async def cmd_addmcp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register an MCP server Claude Code may load via --mcp-config. Gated
    exactly like /addserver -- owner anywhere, or a registered group's own
    admin, PIN required -- because this grants a genuinely new trust surface:
    an MCP server can read and write on the agent's behalf, and the model
    must never be the one deciding to add one for itself."""
    if not _may_run_setup(update):
        return
    lang = _chat_lang(update)
    if not _is_owner(update) and not await _is_group_admin(update, context):
        return await update.message.reply_text(_t(lang,
            "🔒 Bot owner or a group admin only.", "🔒 Cuma pemilik bot atau admin grup."))

    args = context.args or []
    if len(args) < 2 or not _MCP_NAME_RE.match(args[0]):
        existing = ", ".join(sorted(read_mcp_servers())) or _t(lang, "(none yet)", "(belum ada)")
        return await update.message.reply_text(_t(lang,
            "Usage: <code>/addmcp &lt;name&gt; &lt;command&gt; [args...]</code>\n\n"
            "<b>Ready-to-use default</b> -- stdlib only, no pip/npx install, read-only, "
            "locked to one folder, costs nothing to run:\n"
            f"<pre>/addmcp reports {_tg_escape(_MCP_EXAMPLE)}</pre>\n"
            "Gives the model two tools scoped to that one folder: list its files, read "
            "one. Nothing outside it is ever reachable -- see "
            "<code>tools/mcp_readonly_fs.py</code>'s own docstring for exactly how.\n\n"
            f"Currently registered: {existing}",
            "Pakai: <code>/addmcp &lt;nama&gt; &lt;perintah&gt; [argumen...]</code>\n\n"
            "<b>Default siap pakai</b> -- cuma stdlib, tidak perlu install pip/npx, "
            "read-only, terkunci ke satu folder, tidak ada biaya jalan:\n"
            f"<pre>/addmcp reports {_tg_escape(_MCP_EXAMPLE)}</pre>\n"
            "Kasih model dua tool terbatas ke satu folder itu: lihat isinya, baca satu "
            "file. Di luar folder itu tidak pernah terjangkau -- lihat docstring "
            "<code>tools/mcp_readonly_fs.py</code> untuk detail caranya.\n\n"
            f"Terdaftar sekarang: {existing}",
        ), parse_mode="HTML")

    name, command, rest = args[0], args[1], args[2:]
    if name in read_mcp_servers():
        return await update.message.reply_text(_t(lang,
            f"'{name}' is already registered -- /rmmcp it first to replace.",
            f"'{name}' sudah terdaftar -- /rmmcp dulu untuk mengganti."))

    payload = {"name": name, "command": command, "args": rest}
    summary = f"{command} {' '.join(rest)}".strip()
    if pin_is_set(update.effective_chat.id):
        await request_pin(update, "addmcp", payload, _t(lang,
            f"🔌 Adding MCP server <b>{_tg_escape(name)}</b>:\n<code>{_tg_escape(summary)}</code>\n\n"
            "This grants the model a new tool surface -- it can read and, depending on "
            "the server, write on your behalf.",
            f"🔌 Menambah server MCP <b>{_tg_escape(name)}</b>:\n<code>{_tg_escape(summary)}</code>\n\n"
            "Ini memberi model permukaan tool baru -- bisa membaca dan, tergantung "
            "servernya, menulis atas nama Anda.",
        ))
    else:
        await update.message.reply_text(_t(lang,
            "⚠️ No PIN is set, so this isn't protected yet. Set one with /setpin "
            "when you're done.",
            "⚠️ Belum ada PIN, jadi ini belum terlindungi. Atur satu dengan /setpin "
            "kalau sudah selesai.",
        ))
        register_mcp_server(name, command, rest)
        await update.message.reply_text(_t(lang,
            f"🔌 <b>{_tg_escape(name)}</b> registered.\n\n"
            "<i>Takes effect on the next new conversation -- /new applies it now.</i>",
            f"🔌 <b>{_tg_escape(name)}</b> terdaftar.\n\n"
            "<i>Berlaku di percakapan baru berikutnya -- /new untuk langsung terapkan.</i>",
        ), parse_mode="HTML")


async def cmd_rmmcp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """No PIN -- removing a server only ever REDUCES the model's tool surface,
    the same direction /removeserver and /addboundary already leave free."""
    if not _may_run_setup(update):
        return
    lang = _chat_lang(update)
    if not _is_owner(update) and not await _is_group_admin(update, context):
        return await update.message.reply_text(_t(lang,
            "🔒 Bot owner or a group admin only.", "🔒 Cuma pemilik bot atau admin grup."))
    name = (context.args[0] if context.args else "").strip()
    if not name or not remove_mcp_server(name):
        return await update.message.reply_text(_t(lang,
            "Usage: /rmmcp <name>\nSee names with /mcpservers",
            "Pakai: /rmmcp <nama>\nLihat nama di /mcpservers"))
    await update.message.reply_text(_t(lang,
        f"🔌 Removed: {_tg_escape(name)}\n\n<i>Takes effect on the next new conversation -- /new applies it now.</i>",
        f"🔌 Dihapus: {_tg_escape(name)}\n\n<i>Berlaku di percakapan baru berikutnya -- /new untuk langsung terapkan.</i>",
    ), parse_mode="HTML")


async def cmd_mcpservers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Zero tokens -- reads the registry file directly, same as /servers."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    servers = read_mcp_servers()
    if not servers:
        return await update.message.reply_text(_t(lang,
            "No MCP servers registered. /addmcp to add one -- run it bare for a "
            "ready-to-use example.",
            "Belum ada server MCP terdaftar. /addmcp untuk menambah -- jalankan "
            "kosong untuk contoh siap pakai.",
        ))
    lines = [_t(lang, "🔌 <b>MCP servers</b>", "🔌 <b>Server MCP</b>"), ""]
    for name, spec in sorted(servers.items()):
        cmdline = f"{spec.get('command', '?')} {' '.join(spec.get('args', []))}".strip()
        lines.append(f"• <b>{_tg_escape(name)}</b> — <code>{_tg_escape(cmdline)}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_setownerscope(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Extra scope that applies ONLY when the owner is speaking in their own
    private chat with the bot -- never in a group, even one the owner is
    speaking in, and never for anyone else even inside the owner's own
    resumed conversation. See owner_scope_text() / run_combo(owner_dm=...).

    Deliberately separate from /setscope: /setscope changes what the agent IS
    FOR, identically for every chat that talks to it; this changes what it's
    ADDITIONALLY willing to do, and only in the one place it can't leak into
    a group's shared, possibly narrower, brief.

    Owner-only (not group-admin-eligible like /setscope) and only runnable
    from the owner's own DM -- run from a group, the "where does this even
    apply" question has an obvious wrong answer waiting to happen.
    """
    lang = _chat_lang(update)
    if not _is_owner(update):
        return await update.message.reply_text(_t(lang,
            "\U0001f512 Bot owner only.", "\U0001f512 Cuma pemilik bot."))
    if update.effective_chat.type != "private":
        return await update.message.reply_text(_t(lang,
            "This only takes effect in your own private chat, so it can only be set "
            "from there too -- message me directly.",
            "Ini cuma berlaku di chat pribadi kamu, jadi cuma bisa diatur dari sana juga "
            "-- chat saya langsung.",
        ))

    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        current = owner_scope_text()
        if current:
            return await update.message.reply_text(_t(lang,
                f"\U0001f451 <b>Your extra scope</b> (this chat only):\n\n{_tg_escape(current)}\n\n"
                "<code>/setownerscope clear</code> to remove it, or send new text to replace it.",
                f"\U0001f451 <b>Scope tambahan kamu</b> (cuma di chat ini):\n\n{_tg_escape(current)}\n\n"
                "<code>/setownerscope clear</code> untuk menghapus, atau kirim teks baru untuk mengganti.",
            ), parse_mode="HTML")
        return await update.message.reply_text(_t(lang,
            "\U0001f451 <b>Extra scope, just for you, just here</b>\n\n"
            "Every chat already gets whatever /setscope set. This ADDS to that, but only "
            "when you're the one messaging me here -- a group never gets it, even one "
            "you're speaking in, and this chat's own history never carries it to anyone "
            "else who might resume it.\n\n"
            "<code>/setownerscope can also help with general questions, jokes, and "
            "anything else, not just infrastructure</code>\n\n"
            "<i>Takes effect on the next new conversation -- /new applies it now.</i>",
            "\U0001f451 <b>Scope tambahan, cuma untuk kamu, cuma di sini</b>\n\n"
            "Setiap chat sudah dapat apa pun yang diatur /setscope. Ini MENAMBAH itu, tapi "
            "cuma saat kamu yang chat saya di sini -- grup tidak pernah dapat ini, "
            "walau kamu yang bicara di situ, dan riwayat chat ini juga tidak membawanya "
            "ke siapa pun lain yang mungkin melanjutkannya.\n\n"
            "<code>/setownerscope boleh juga bantu pertanyaan umum, joke, dan hal lain, "
            "tidak cuma infrastruktur</code>\n\n"
            "<i>Berlaku di percakapan baru berikutnya -- /new untuk langsung terapkan.</i>",
        ), parse_mode="HTML")

    if text.lower() in ("clear", "hapus", "none"):
        cleared = clear_owner_scope()
        return await update.message.reply_text(_t(lang,
            "✅ Cleared." if cleared else "Nothing was set.",
            "✅ Sudah dihapus." if cleared else "Memang belum ada yang diatur.",
        ))

    set_owner_scope(text)
    await update.message.reply_text(_t(lang,
        f"✅ <b>Recorded, just for you, just here.</b>\n\n{_tg_escape(text)}\n\n"
        "<i>Takes effect on the next new conversation -- /new applies it now.</i>",
        f"✅ <b>Tercatat, cuma untuk kamu, cuma di sini.</b>\n\n{_tg_escape(text)}\n\n"
        "<i>Berlaku di percakapan baru berikutnya -- /new untuk langsung terapkan.</i>",
    ), parse_mode="HTML")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    state = _wizard.pop(update.effective_chat.id, None)
    if state:
        state["handle"].kill()
        await update.message.reply_text(_t(lang, "✖️ Cancelled.", "✖️ Dibatalkan."))
    else:
        await update.message.reply_text(_t(lang, "Nothing to cancel.", "Tidak ada yang perlu dibatalkan."))


def _tier_summary() -> str:
    """The live chain, numbered, for /help. Built from TIERS so it can never
    drift from what the bot will actually do."""
    lines = []
    for i, t in enumerate(TIERS, 1):
        where = "primary" if i == 1 else ("last resort" if i == len(TIERS) else f"fallback #{i - 1}")
        lines.append(f"{i}. {t['label']} \u2014 {t['model']} ({where})")
    return "\n".join(lines)


# Required by LICENSE -- do not remove or alter without written permission
# from the copyright holder (see LICENSE at the repo root).
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
/remember <fact> — save a fact PERMANENTLY for THIS CHAT, read in EVERY session & EVERY tier, even after many /new
/spend [days] — what this deployment actually spent, straight from the ledger (0 tokens)
/memory — view memory: this chat's own facts, plus the shared base
/schedules — everything that runs on a timer, and what it does (0 tokens)
/unschedule <name> — remove a scheduled task
/adopt — bring pre-existing cron entries under management
/setpin — set/change the OWNER's PIN, works everywhere (entered on a keypad, never typed in chat)
/setgrouppin — set/change THIS group's own PIN (owner or this group's admin, run inside the group)
/rmgrouppin — remove this group's own PIN, falls back to the owner's (owner or this group's admin)
/update — check GitHub for a newer version and install it (PIN)
/setbrief <what it looks after> — set the one-line environment brief
/setscope <kind of assistant> — change the role itself, e.g. beyond infrastructure-only
/setownerscope <extra text> — extra scope for YOU only, in YOUR DM only (owner-only)
/logout — clear a sign-in (Gemini or Claude) so the next /start is a genuinely fresh one
/boundaries — what the agent must never do (0 tokens)
/addboundary <rule> — add a rule the agent may NEVER break (run it alone to see what that means)
/rmboundary <n> — remove one (PIN)
/snapshots — snapshots taken before changes (0 tokens)
/servers — machines the agent may reach (0 tokens)
/addserver — register a new machine, step by step
/removeserver <name> — unregister one
/agentstatus — live check: is each AI tier actually up right now?
/providers — which AI tiers are configured and which are healthy (0 tokens)
/usemodel [name] — force a specific tier for this chat (Opus, Gemini Pro-high, ...); `/usemodel auto` for the default chain
/gdrive — pick (or show) which connected Drive account this room uploads to (0 tokens)
/connectgdrive — connect a new Drive account, through Telegram (owner anywhere, or a registered group's own admin)
/lang [en|id] — set/show this chat's language for the bot's own fixed replies (0 tokens)
/mode — read-only right now, or able to make changes? (0 tokens)
/unlock [minutes] — open a time-boxed window for real changes (owner anywhere, or a registered group's own admin -- capped at 10 min from a group)
/lock — close that window early
/learned — see what the agent figured out about this environment BY ITSELF
/forget <number> — delete one wrong learned fact (numbers come from /learned)
/cancel — abort a multi-step form (/start, /addserver)
/help — this guide (choose EN or ID)

*3 habits that keep it cheap*
1. *`/new` every time the topic changes.* A continuing conversation history is EXPENSIVE -- the longer it gets, the more expensive every next turn (can be 10-20x if left to pile up). Infra case closed → want to ask something unrelated? `/new` first.
2. *Don't `/new` BETWEEN "generate a report" and "send it to me".* A fresh session has no memory of which report was just made -- ask "send the file" in a new session and it'll go looking through every old report that exists and offer all of them.
3. *Important facts → `/remember`, not chat history.* Cases that are closed/decided go into `/remember` so they don't get re-asked or re-investigated -- this is the ONLY thing that survives across `/new`.

*Using it in a Telegram Group*
If this group has been registered by an admin (check with `/chatid`), EVERY member can give the bot commands automatically -- no per-person whitelist needed.
1. By default the bot only sees *commands* (`/status` etc) or a reply to one of its own messages -- that's Telegram's Privacy Mode, ON for every new bot, and it hides everyday chat (a plain `@botname` typed mid-sentence included, despite looking like a mention) from the bot entirely. **For most groups this is what you want**: no token spent unless someone actually asked it something. To make a real `@botname` mention wake it up too: (a) @BotFather → `/mybots` → this bot → *Bot Settings* → *Group Privacy* → *Turn off*, then (b) **remove the bot from this group and invite it back** -- Telegram locks in the privacy setting when the bot JOINS, so without the re-invite the old setting sticks and mentions keep being ignored. No need to `/registergroup` again; the chat ID (and its PIN) survive. This does NOT turn the bot into a full participant: ordinary chatter still costs nothing, only mentions and replies do.
2. Sessions (`/new`, `/session`) in this group are *separate* from each member's private DM -- safely isolated. But `/remember` is *GLOBAL* across every chat including this group -- if someone remembers a fact here, everyone here (and in every other chat with this bot) can see it via `/memory`.
3. This group doesn't have access yet? An admin just needs to type `/registergroup` here -- takes effect immediately, no restart needed. (`/unregistergroup` to revoke it again.)
4. Confirming with a PIN (`/addserver`, `/update`, etc) here uses this group's OWN PIN if it has one (`/setgrouppin`), so different companies sharing one bot don't need to share a secret -- the owner's personal PIN always works too, everywhere, as a master credential.

{_HELP_CREDITS}"""

HELP_TEXT_ID = f"""\U0001f4d6 *iSmart-LA — Panduan Pemakaian*

*Cara kerjanya*
Setiap pesan dicoba lewat 4 tingkatan, dari yang paling murah dulu:
{_tier_summary()}

Setiap balasan diakhiri tanda "— by ...". Kalau tandanya BUKAN "{TIERS[0]['label']}", itu sinyal ada gangguan di salah satu layanan (rate limit, auth, dll) -- gampang dipantau sekilas tanpa perlu buka log.

*Daftar perintah*
/status — cek status instan, NOL token (langsung dari script, bukan model)
/tools — daftar skill yang sudah jadi script, NOL token
/graduate <nama> — ubah kasus yang BARU SAJA selesai jadi script reusable (gratis dipakai lagi)
/new — mulai ulang sesi AKTIF dari nol (riwayat percakapan direset, MEMORY.md tetap ada)
/session <nama> — buat/pindah ke sesi bernama, buat pisahin kasus berbeda
/sessions — lihat semua sesi tersimpan
/remember <fakta> — simpan fakta PERMANEN KHUSUS CHAT INI, kebaca di SEMUA sesi & tingkatan, walau sudah /new berkali-kali
/spend [hari] — pemakaian token nyata, langsung dari ledger (NOL token)
/memory — lihat memori: fakta milik chat ini, plus yang dipakai bersama
/schedules — semua yang jalan terjadwal, dan isinya apa (NOL token)
/unschedule <nama> — hapus satu task terjadwal
/adopt — bawa cron lama ke dalam kelolaan bot
/setpin — atur/ganti PIN OWNER, berlaku di mana pun (lewat keypad, tidak pernah diketik di chat)
/setgrouppin — atur/ganti PIN milik grup INI (owner atau admin grup ini, jalankan di dalam grupnya)
/rmgrouppin — hapus PIN grup ini, kembali pakai PIN owner (owner atau admin grup ini)
/update — cek versi terbaru di GitHub dan pasang (pakai PIN)
/setbrief <yang diurus> — atur brief lingkungan satu baris
/setscope <jenis agent> — ubah perannya sendiri, mis. lebih luas dari infrastruktur saja
/setownerscope <teks tambahan> — scope tambahan cuma untuk KAMU, cuma di DM KAMU (owner-only)
/logout — hapus satu sign-in (Gemini atau Claude) supaya /start berikutnya benar-benar baru
/boundaries — apa yang tidak boleh dilakukan agent (NOL token)
/addboundary <aturan> — tambah aturan yang TIDAK BOLEH dilanggar agent (ketik sendirian untuk lihat penjelasannya)
/rmboundary <n> — hapus satu (pakai PIN)
/snapshots — snapshot yang diambil sebelum perubahan (NOL token)
/servers — daftar mesin yang boleh diakses agent (NOL token)
/addserver — daftarkan mesin baru, langkah demi langkah
/removeserver <nama> — hapus satu
/agentstatus — cek langsung: tiap tingkat AI benar-benar hidup sekarang?
/providers — tingkat AI mana saja yang dipakai dan mana yang sehat (NOL token)
/usemodel [nama] — paksa satu tingkatan tertentu untuk chat ini (Opus, Gemini Pro-high, ...); `/usemodel auto` untuk balik ke rantai default
/gdrive — pilih (atau lihat) akun Drive mana yang dipakai room ini untuk upload (NOL token)
/connectgdrive — hubungkan akun Drive baru, lewat Telegram (owner di mana saja, atau admin grup terdaftar)
/lang [en|id] — atur/lihat bahasa balasan tetap bot untuk chat ini (NOL token)
/mode — agent lagi read-only atau boleh mengubah? (NOL token)
/unlock [menit] — buka mode tulis untuk waktu terbatas (pemilik di mana saja, atau admin grup terdaftar -- dibatasi maks 10 menit dari grup)
/lock — tutup lebih awal
/learned — lihat apa saja yang agent pelajari SENDIRI soal lingkungan ini
/forget <nomor> — hapus satu catatan hasil belajar yang keliru (nomornya dari /learned)
/cancel — batalkan form bertahap yang sedang jalan (/start, /addserver)
/help — panduan ini (pilih EN atau ID)

*3 kebiasaan supaya tetap irit*
1. *`/new` setiap topik berganti.* Riwayat percakapan yang terus nyambung itu MAHAL -- makin panjang, makin mahal setiap giliran berikutnya (bisa 10-20x kalau dibiarkan menumpuk). Case infra sudah selesai → mau tanya hal lain yang tidak nyambung? `/new` dulu.
2. *Jangan `/new` DI ANTARA "buatkan laporan" dan "kirim ke saya".* Sesi baru tidak ingat laporan mana yang baru saja dibuat -- minta "kirim filenya" di sesi baru malah bikin dia mencari semua laporan lama yang pernah ada dan menawarkan semuanya.
3. *Fakta penting → `/remember`, bukan riwayat chat.* Case yang sudah selesai/diputuskan masukkan ke `/remember` supaya tidak ditanyakan/diselidiki ulang -- ini SATU-SATUNYA yang bertahan lintas `/new`.

*Memakainya di Grup Telegram*
Kalau grup ini sudah didaftarkan admin (cek dengan `/chatid`), SEMUA anggota otomatis bisa kasih perintah ke bot -- tidak perlu whitelist per orang.
1. Secara default bot cuma melihat *perintah* (`/status` dll) atau reply ke salah satu pesannya -- itu Privacy Mode bawaan Telegram, AKTIF untuk setiap bot baru, dan itu menyembunyikan chat sehari-hari (termasuk sekadar ketik `@namabot` di tengah kalimat, meski terlihat seperti mention) dari bot sepenuhnya. **Untuk kebanyakan grup, ini yang Anda mau**: nol token terpakai kecuali memang ada yang bertanya. Supaya mention `@namabot` beneran juga bisa membangunkan bot: (a) chat @BotFather → `/mybots` → pilih bot ini → *Bot Settings* → *Group Privacy* → *Turn off*, lalu (b) **keluarkan bot dari grup ini, terus undang lagi** -- Telegram mengunci setting privacy saat bot BERGABUNG, jadi tanpa diundang ulang setting lama tetap menempel dan mention terus diabaikan. Tidak perlu `/registergroup` lagi; ID grup (dan PIN-nya) tetap tersimpan. Ini TIDAK bikin bot jadi anggota penuh yang baca semua chat: obrolan biasa tetap nol token, cuma mention dan reply yang diproses.
2. Sesi (`/new`, `/session`) di grup ini *terpisah* dari DM pribadi masing-masing anggota -- aman terisolasi. Tapi `/remember` bersifat *GLOBAL* di semua chat termasuk grup ini -- kalau seseorang me-remember fakta di sini, semua orang di sini (dan di semua chat lain dengan bot ini) bisa melihatnya lewat `/memory`.
3. Grup ini belum punya akses? Admin tinggal ketik `/registergroup` di sini -- langsung aktif, tanpa perlu restart. (`/unregistergroup` untuk mencabutnya lagi.)
4. Konfirmasi pakai PIN (`/addserver`, `/update`, dll) di sini memakai PIN milik grup INI kalau sudah diatur (`/setgrouppin`), jadi beberapa perusahaan yang berbagi satu bot tidak perlu berbagi rahasia yang sama -- PIN pribadi owner tetap berlaku di mana pun, sebagai kredensial utama.

{_HELP_CREDITS}"""

_HELP_LANG_KEYBOARD = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("\U0001f1ee\U0001f1e9 Indonesia", callback_data="help_id"),
        InlineKeyboardButton("\U0001f1ec\U0001f1e7 English", callback_data="help_en"),
    ]]
)

_HELP_LANG_PROMPT = "Pilih bahasa / Choose a language:"


TELEGRAM_MESSAGE_LIMIT = 4096


def _split_for_telegram(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split text into <= limit-char chunks for sending as separate messages.

    HELP_TEXT_EN/ID passed this limit during this project's own growth --
    a real /help tap in production came back "Message_too_long" from Telegram,
    not a bug in the content, just more of it than one message can hold. This
    keeps growing content from silently breaking delivery again: it cuts at
    the last blank line (paragraph break) at or before the limit when one
    exists, falling back to the last plain newline, so a cut lands between
    sections rather than mid-sentence -- and, since every section in this
    file's help text opens and closes its own Markdown markers, mid-paragraph
    rather than mid-entity.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut].rstrip("\n"))
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg in ("id", "indonesia", "indonesian"):
        for chunk in _split_for_telegram(HELP_TEXT_ID):
            await update.message.reply_text(chunk, parse_mode="Markdown")
        return
    if arg in ("en", "english"):
        for chunk in _split_for_telegram(HELP_TEXT_EN):
            await update.message.reply_text(chunk, parse_mode="Markdown")
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
    chunks = _split_for_telegram(text)
    # An edit can only ever hold ONE message's worth of text -- the language
    # picker's own message becomes the first chunk, and the rest (if any)
    # follow as new messages, since a single message can't be split in place.
    await query.edit_message_text(chunks[0], parse_mode="Markdown")
    for chunk in chunks[1:]:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk, parse_mode="Markdown")


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
    lang = _chat_lang(update)
    facts = _learned_facts()
    if not facts:
        await update.message.reply_text(_t(lang,
            "The agent hasn't recorded anything about this environment yet.\n\n"
            "Environment knowledge fills itself in as you use it. Safety rules "
            "(hard boundaries) live in the protected zone and never change with it.",
            "Agent belum mencatat apa pun tentang lingkungan ini.\n\n"
            "Pengetahuan lingkungan terisi sendiri sambil jalan. Aturan keselamatan "
            "(hard boundaries) ada di zona terkunci dan tidak pernah ikut berubah.",
        ))
        return
    numbered = "\n".join(f"{i}. {f[2:]}" for i, f in enumerate(facts, 1))
    await _reply_chunked(
        update,
        _t(lang,
           f"🧠 What the agent has worked out about this environment ({len(facts)}):\n\n{numbered}"
           "\n\nSomething wrong in there? Remove it with /forget <number>.",
           f"🧠 Yang sudah dipelajari agent tentang lingkungan ini ({len(facts)}):\n\n{numbered}"
           "\n\nSalah satu keliru? Hapus dengan /forget <nomor>.",
        ),
    )


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove one learned fact. The counterpart to automatic learning: anything
    written without being asked must be just as easy to take back."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    arg = (context.args[0] if context.args else "").strip()
    facts = _learned_facts()
    if not arg.isdigit() or not (1 <= int(arg) <= len(facts)):
        await update.message.reply_text(_t(lang,
            f"Usage: /forget <number>\nNumbers come from /learned "
            f"({len(facts)} recorded right now).",
            f"Pakai: /forget <nomor>\nLihat nomornya di /learned "
            f"({len(facts)} catatan saat ini).",
        ))
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
    await update.message.reply_text(_t(lang,
        f"🗑 Forgotten:\n{target[2:]}" if removed else "Nothing was removed.",
        f"🗑 Dilupakan:\n{target[2:]}" if removed else "Tidak ada yang dihapus.",
    ))


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open write mode for a fixed window.

    Deliberately stricter than _authorized(): _is_trusted_origin() means a
    private DM from a named account. A group is a place where text arrives from
    people and systems this deployment does not vet, and this is the one command
    that hands the agent the ability to change production -- so it is not
    something a group can reach, even a registered one.
    """
    lang = _chat_lang(update)
    if not await _may_authorize_group_action(update, context):
        logger.warning(
            "refused /unlock from chat=%s user=%s",
            getattr(update.effective_chat, "id", "?"),
            getattr(update.effective_user, "id", "?"),
        )
        await update.message.reply_text(_t(lang,
            "🔒 Bot owner, or a registered group's own admin.",
            "🔒 Pemilik bot, atau admin dari grup yang sudah terdaftar.",
        ))
        return
    if not _keys_configured():
        await update.message.reply_text(_t(lang,
            "⚠️ Write-mode keys are not set up, so there is nothing to unlock — the agent "
            f"is using whatever `{SSH_ACTIVE_KEY.name}` already points at.\n\n"
            "See README (\"Write mode\") to enable the two-key setup.",
            "⚠️ Kunci write-mode belum disiapkan, jadi tidak ada yang perlu dibuka — agent "
            f"masih memakai kunci yang sekarang ada di `{SSH_ACTIVE_KEY.name}`.\n\n"
            "Lihat README (\"Write mode\") untuk mengaktifkan setup dua kunci.",
        ), parse_mode="Markdown")
        return

    cap = _effective_unlock_cap(update)
    minutes = min(WRITE_MODE_DEFAULT_MINUTES, cap)
    if context.args:
        try:
            minutes = int(context.args[0])
        except ValueError:
            await update.message.reply_text(_t(lang,
                f"Usage: /unlock [minutes, max {cap} here]",
                f"Pakai: /unlock [menit, maks {cap} di sini]",
            ))
            return
    # Opening write access is the most consequential thing this bot can do,
    # so it takes more than a tap from a signed-in device. The PIN is the
    # factor that does not come along with a stolen session, and it is
    # entered on a keypad so it never becomes a message in the chat.
    await request_pin(
        update, "unlock", {"minutes": minutes},
        _t(lang,
           f"🔓 Confirm opening write mode for {min(minutes, cap)} minute(s).",
           f"🔓 Konfirmasi buka write mode untuk {min(minutes, cap)} menit.",
        ),
    )


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _may_authorize_group_action(update, context):
        return
    lang = _chat_lang(update)
    was_open = write_mode_expires_at() is not None
    lock_write_mode()
    await update.message.reply_text(_t(lang,
        "🔒 Write mode closed. Back to read-only." if was_open else "🔒 Already read-only.",
        "🔒 Write mode ditutup. Kembali read-only." if was_open else "🔒 Memang sudah read-only.",
    ))


async def cmd_usemodel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force a specific tier to answer THIS chat's turns, bypassing the normal
    cheapest-first order -- for a genuinely demanding case, not routine use.
    Gated like /addserver (owner anywhere, or a registered group's own admin):
    it spends this deployment's own shared subscription quota, so it isn't
    left open to anyone who can merely talk to the bot."""
    if not await _may_authorize_group_action(update, context):
        return
    lang = _chat_lang(update)
    chat_id = str(update.effective_chat.id)
    overrides = _read_model_overrides()

    if not context.args:
        current = overrides.get(chat_id)
        lines = [_t(lang, "\U0001f9e0 <b>Model selection</b>", "\U0001f9e0 <b>Pilihan model</b>"), ""]
        if current:
            label = BACKEND_LABELS.get(current, current)
            lines.append(_t(lang,
                f"Forced: <b>{_tg_escape(label)}</b> for this chat.",
                f"Dipaksa: <b>{_tg_escape(label)}</b> untuk chat ini.",
            ))
            lines.append(_t(lang,
                "Every turn tries this first; the default chain still backs it up if it's unavailable.",
                "Tiap turn coba ini duluan; rantai default tetap jadi cadangan kalau tidak tersedia.",
            ))
        else:
            lines.append(_t(lang,
                f"Auto (default chain) -- currently <b>{_tg_escape(TIERS[0]['label'])}</b> first.",
                f"Auto (rantai default) -- sekarang <b>{_tg_escape(TIERS[0]['label'])}</b> duluan.",
            ))
        lines.append("")
        lines.append(_t(lang, "Default chain (always on, cheapest first):", "Rantai default (selalu aktif, termurah dulu):"))
        lines.extend(f"  \u2022 {_tg_escape(t['label'])}" for t in TIERS)
        lines.append("")
        lines.append(_t(lang, "Extra, on-demand only:", "Tambahan, cuma on-demand:"))
        lines.extend(f"  \u2022 {_tg_escape(t['label'])}" for t in EXTRA_TIERS)
        lines.append("")
        lines.append(_t(lang,
            "<code>/usemodel &lt;name&gt;</code> to force one, <code>/usemodel auto</code> for the default chain.",
            "<code>/usemodel &lt;nama&gt;</code> untuk paksa satu, <code>/usemodel auto</code> untuk balik ke rantai default.",
        ))
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    choice = " ".join(context.args).strip()
    if choice.lower() == "auto":
        if overrides.pop(chat_id, None) is not None:
            _write_model_overrides(overrides)
        await update.message.reply_text(_t(lang,
            "\U0001f504 Back to auto -- default cheapest-first chain.",
            "\U0001f504 Balik ke auto -- rantai default termurah dulu.",
        ))
        return

    tier = _find_tier_by_alias(choice)
    if not tier:
        names = ", ".join(t["label"] for t in ALL_TIERS)
        await update.message.reply_text(_t(lang,
            f"Don't recognize {choice!r}. Options: {names}, or auto.",
            f"Tidak kenal {choice!r}. Pilihan: {names}, atau auto.",
        ))
        return

    overrides[chat_id] = tier["model"]
    _write_model_overrides(overrides)
    await update.message.reply_text(_t(lang,
        f"\U0001f3af Forcing <b>{_tg_escape(tier['label'])}</b> for this chat's next turns. "
        "<code>/usemodel auto</code> to go back to the default chain.",
        f"\U0001f3af Memaksa <b>{_tg_escape(tier['label'])}</b> untuk turn berikutnya di chat ini. "
        "<code>/usemodel auto</code> untuk balik ke rantai default.",
    ), parse_mode="HTML")


def check_gdrive_account(name: str, timeout: int = 30) -> tuple[bool, str]:
    """Is this Drive remote still usable RIGHT NOW? Zero model tokens -- it is
    one rclone call, no model involved.

    Worth having even though rclone refreshes access tokens by itself: the
    refresh token underneath can still die, and every way it dies is silent.
    Access revoked at myaccount.google.com, the OAuth client deleted, the
    Google password changed, or -- the one this project ran into head-on --
    an OAuth client left in "Testing" publishing status, where Google expires
    the refresh token after 7 days no matter how healthy the connection looks.
    Without a check, the first sign of any of that is a report that silently
    never arrives.
    """
    try:
        probe = _rclone_run("lsd", f"{name}:", timeout=timeout)
    except Exception as exc:
        return False, f"could not run rclone: {exc}"
    if probe.returncode == 0:
        return True, "ok"
    err = (probe.stderr or probe.stdout or "").strip()
    low = err.lower()
    if "token" in low and ("expired" in low or "invalid" in low or "revoked" in low):
        return False, "the sign-in has expired or been revoked -- /connectgdrive again"
    if "quota" in low or "rate" in low:
        return False, "Google is rate-limiting or the quota is exhausted"
    return False, err[:200] or f"rclone exited {probe.returncode}"


def check_all_gdrive_accounts() -> list[tuple[str, bool, str]]:
    return [(n, *check_gdrive_account(n)) for n in _list_gdrive_accounts()]


async def cmd_gdrivestatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Live health of every connected Drive account. 0 model tokens."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    accounts = _list_gdrive_accounts()
    if not accounts:
        return await update.message.reply_text(_t(lang,
            "📁 No Google Drive account is connected. /connectgdrive to add one.",
            "📁 Belum ada akun Google Drive terhubung. /connectgdrive untuk menambah."))
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, check_all_gdrive_accounts)
    lines = [_t(lang, "📁 <b>Google Drive</b>", "📁 <b>Google Drive</b>"), ""]
    for name, ok, detail in rows:
        mark = "🟢" if ok else "🔴"
        lines.append(f"{mark} <b>{_tg_escape(name)}</b>"
                     + ("" if ok else f" — {_tg_escape(detail)}"))
    client = read_gdrive_client()
    if not client:
        lines.append("")
        lines.append(_t(lang,
            "<i>No OAuth client set up, so /connectgdrive will ask for one first.</i>",
            "<i>Belum ada OAuth client, jadi /connectgdrive akan menanyakannya dulu.</i>"))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_gdrive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick (or show) which connected Drive account this room uploads to.
    Connecting an account itself is still a one-time step done directly on
    the host, not through Telegram (same as the SSH keypair setup) -- this
    only lets a room choose among accounts that already exist. Gated like
    /usemodel: a room-wide setting, not a per-person one."""
    if not await _may_authorize_group_action(update, context):
        return
    lang = _chat_lang(update)
    accounts = _list_gdrive_accounts()
    if not accounts:
        await update.message.reply_text(_t(lang,
            "\U0001f4c1 No Google Drive account is connected yet. Ask the operator to "
            "connect one (see README \u201cGoogle Drive\u201d) -- a one-time step done "
            "directly on the host, not through Telegram.",
            "\U0001f4c1 Belum ada akun Google Drive yang terhubung. Minta operator untuk "
            "hubungkan satu (lihat README \u201cGoogle Drive\u201d) -- langkah sekali-jalan "
            "langsung di host, bukan lewat Telegram.",
        ))
        return
    chat_id = str(update.effective_chat.id)
    explicit = _read_gdrive_room_accounts().get(chat_id)
    effective = _gdrive_effective_default(chat_id, accounts)
    lines = [_t(lang, "\U0001f4c1 <b>Google Drive account for this room</b>", "\U0001f4c1 <b>Akun Google Drive untuk room ini</b>"), ""]
    if explicit and explicit in accounts:
        lines.append(_t(lang, f"Currently: <b>{_tg_escape(explicit)}</b>", f"Sekarang: <b>{_tg_escape(explicit)}</b>"))
    elif effective:
        lines.append(_t(lang,
            f"Using <b>{_tg_escape(effective)}</b> automatically -- the only account connected.",
            f"Otomatis pakai <b>{_tg_escape(effective)}</b> -- satu-satunya akun yang terhubung.",
        ))
    else:
        lines.append(_t(lang,
            "Not picked yet -- uploads here won't work until you choose one.",
            "Belum dipilih -- upload di sini belum jalan sampai kamu pilih satu.",
        ))
    lines.append("")
    lines.append(_t(lang, "Tap one to use it here:", "Tap salah satu untuk dipakai di sini:"))
    # The tick is bound to a name first, deliberately: a backslash escape
    # INSIDE an f-string expression only became legal in Python 3.12 (PEP 701),
    # so writing it inline made the whole module fail to import on 3.10/3.11 --
    # versions install.sh still advertises, and what Debian 12 and Ubuntu 22.04
    # ship. A syntax error at import is not a degraded feature; nothing runs.
    tick = "\u2705 "
    rows = [[InlineKeyboardButton(
        f"{tick if a == effective else ''}{a}", callback_data=f"gdrv:{a}",
    )] for a in accounts]
    lines.append("")
    lines.append(_t(lang,
        "Need a different account entirely (not listed above)? That's still a "
        "one-time step done directly on the host -- ask the operator to connect one.",
        "Butuh akun lain (belum ada di atas)? Tetap langkah sekali-jalan langsung "
        "di host -- minta operator hubungkan satu.",
    ))
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
    )


async def cmd_gdrive_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = _chat_lang(update)
    if not await _may_authorize_group_action(update, context):
        await query.answer(_t(lang, "Not permitted.", "Tidak diizinkan."), show_alert=True)
        return
    await query.answer()
    _, remote = query.data.split(":", 1)
    if remote not in _list_gdrive_accounts():
        await query.edit_message_text(_t(lang,
            "That account isn't connected any more. Run /gdrive again.",
            "Akun itu sudah tidak terhubung lagi. Jalankan /gdrive lagi.",
        ))
        return
    chat_id = str(update.effective_chat.id)
    room_accounts = _read_gdrive_room_accounts()
    room_accounts[chat_id] = remote
    _write_gdrive_room_accounts(room_accounts)
    await query.edit_message_text(_t(lang,
        f"\U0001f4c1 This room now uploads to Drive account: <b>{_tg_escape(remote)}</b>",
        f"\U0001f4c1 Room ini sekarang upload ke akun Drive: <b>{_tg_escape(remote)}</b>",
    ), parse_mode="HTML")


GDRIVE_TOKEN_WIZARD_TTL = 900
# chat_id -> {"step": "await_gdrive_label" | "await_gdrive_client" |
#             "device_pending" | "await_gdrive_token", "name": str|None,
#             "expires": ts}. "await_gdrive_token" is only reached via
# `/connectgdrive manual` now -- see the note in cmd_connectgdrive.
_gdrive_wizard: dict[int, dict] = {}


def _next_gdrive_default_name() -> str:
    """"gdrive" if nothing is connected yet, else "gdrive_2", "gdrive_3", ...
    Only used as a starting SUGGESTION when a second+ account is being added --
    the operator still gets asked for a short human label first, since a
    numbered name alone gives nobody picking an account in /gdrive a clue
    which is which."""
    existing = set(_list_gdrive_accounts())
    if "gdrive" not in existing:
        return "gdrive"
    n = 2
    while f"gdrive_{n}" in existing:
        n += 1
    return f"gdrive_{n}"


def _sanitize_gdrive_label(label: str) -> str:
    """A free-text label -> "gdrive_<label>", safe as an rclone remote name.
    Same character set _list_gdrive_accounts()'s own regex expects."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", label.strip()).strip("_")
    return f"gdrive_{cleaned}"[:60] if cleaned else ""


def _gdrive_connect_instructions(lang: str, name: str) -> str:
    return _t(lang,
        f"\U0001f511 <b>Connecting ‘{_tg_escape(name)}’</b>\n\n"
        "Google's OAuth for Drive needs a redirect back to a browser on the SAME "
        "machine that runs the command -- unlike Gemini/Claude's sign-in, there is no "
        "single link that works from any device, so this one step has to happen on a "
        "machine you control that has <code>rclone</code> and a browser (your laptop, "
        "not necessarily this server):\n\n"
        f"<pre>rclone authorize drive --drive-scope drive.file</pre>\n\n"
        "Approve in the browser that opens. rclone will then print a block starting "
        "with <code>{\"access_token\"...}</code> -- copy that whole line and paste it "
        "here as your next message. I'll delete it immediately after reading it, same "
        "as an OAuth code.\n\nSend /cancel to stop.",

        f"\U0001f511 <b>Menghubungkan ‘{_tg_escape(name)}’</b>\n\n"
        "OAuth Google untuk Drive butuh redirect balik ke browser di mesin YANG SAMA "
        "dengan yang menjalankan perintah -- beda dari sign-in Gemini/Claude yang satu "
        "link bisa dibuka dari perangkat mana pun, jadi langkah ini harus dilakukan di "
        "mesin yang kamu kuasai dan punya <code>rclone</code> + browser (laptop kamu, "
        "tidak harus server ini):\n\n"
        f"<pre>rclone authorize drive --drive-scope drive.file</pre>\n\n"
        "Setujui di browser yang terbuka. rclone lalu mencetak satu blok diawali "
        "<code>{\"access_token\"...}</code> -- salin seluruh baris itu dan tempel di "
        "sini sebagai pesan berikutnya. Saya hapus segera setelah dibaca, sama seperti "
        "kode OAuth.\n\nKirim /cancel untuk berhenti.",
    )


async def cmd_connectgdrive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start connecting a NEW Google Drive account, explicitly through Telegram.

    Uses Google's OAuth device flow -- open a URL, type a code, done -- so
    this finally matches how signing in to Gemini and Claude already works
    here. It replaces having to run `rclone authorize` in a terminal on your
    own PC and paste the token back, which was the one part of setup that
    still needed a machine other than this one.

    Why it can work at all, checked against Google's documentation rather
    than assumed: the device flow supports only a limited scope list, and of
    the Drive scopes just drive.appdata and drive.file are on it. drive.file
    is what connect_gdrive_account() already asked for, so nothing about the
    bot's reach changes -- only how the token is obtained. drive.file is also
    non-sensitive, so the operator's own OAuth client needs no Google review
    to be published.

    The paste-a-token path is kept as a fallback for anyone who needs the
    full `drive` scope to reach folders the bot did not create itself.
    """
    if not await _may_authorize_group_action(update, context):
        return
    lang = _chat_lang(update)
    chat_id = update.effective_chat.id
    if chat_id in _gdrive_wizard:
        await update.message.reply_text(_t(lang,
            "Already connecting one -- finish that (or /cancel) first.",
            "Sedang menghubungkan satu -- selesaikan itu dulu (atau /cancel)."))
        return

    # The paste-a-token path is still here on purpose: drive.file only reaches
    # files the bot itself created, so anyone who needs the agent to write into
    # a folder they made by hand still needs a full-"drive" token, and that
    # scope is not one Google's device flow will issue.
    manual = bool(context.args) and context.args[0].strip().lower() in ("manual", "token")

    existing = _list_gdrive_accounts()
    if manual:
        name = _next_gdrive_default_name() if existing else "gdrive"
        _gdrive_wizard[chat_id] = {
            "step": "await_gdrive_token", "name": name,
            "expires": _dt.datetime.now().timestamp() + GDRIVE_TOKEN_WIZARD_TTL,
        }
        await update.message.reply_text(
            _gdrive_connect_instructions(lang, name), parse_mode="HTML")
        return

    if not existing:
        name = "gdrive"
        _gdrive_wizard[chat_id] = {
            "step": "await_gdrive_client", "name": name,
            "expires": _dt.datetime.now().timestamp() + GDRIVE_TOKEN_WIZARD_TTL,
        }
        if read_gdrive_client():
            await _gdrive_begin_device(update, context, lang, name)
        else:
            await update.message.reply_text(
                _gdrive_client_setup_instructions(lang), parse_mode="HTML")
        return

    # A second+ account: ask for a short label first so /gdrive's picker stays
    # meaningful ("gdrive_clienta", not "gdrive_2").
    _gdrive_wizard[chat_id] = {
        "step": "await_gdrive_label", "name": None,
        "expires": _dt.datetime.now().timestamp() + GDRIVE_TOKEN_WIZARD_TTL,
    }
    suggestion = _next_gdrive_default_name()
    await update.message.reply_text(_t(lang,
        f"Already connected: {', '.join(existing)}.\n\n"
        f"Short label for this new one (letters/digits/-/_ only, e.g. "
        f"<code>company</code> or <code>clienta</code>) -- becomes "
        f"<code>gdrive_&lt;label&gt;</code>. Send just the label, or /cancel.\n\n"
        f"<i>No preference? Reply {_tg_escape(suggestion.removeprefix('gdrive_') or '2')} "
        f"for {_tg_escape(suggestion)}.</i>",
        f"Sudah terhubung: {', '.join(existing)}.\n\n"
        f"Label pendek untuk yang baru ini (huruf/angka/-/_ saja, mis. "
        f"<code>company</code> atau <code>clienta</code>) -- jadi "
        f"<code>gdrive_&lt;label&gt;</code>. Kirim labelnya saja, atau /cancel.\n\n"
        f"<i>Tidak ada preferensi? Balas {_tg_escape(suggestion.removeprefix('gdrive_') or '2')} "
        f"untuk {_tg_escape(suggestion)}.</i>",
    ), parse_mode="HTML")


def _rclone_run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([RCLONE_BIN, *args], capture_output=True, text=True, timeout=timeout)


def read_gdrive_client() -> dict:
    """{"client_id": ..., "client_secret": ...}, or {} when not set up yet."""
    if not GDRIVE_CLIENT_FILE.exists():
        return {}
    try:
        d = json.loads(GDRIVE_CLIENT_FILE.read_text())
        return d if d.get("client_id") else {}
    except Exception:
        logger.warning("gdrive_oauth_client.json unreadable", exc_info=True)
        return {}


def write_gdrive_client(client_id: str, client_secret: str) -> None:
    GDRIVE_CLIENT_FILE.write_text(json.dumps(
        {"client_id": client_id.strip(), "client_secret": client_secret.strip()}, indent=2))
    try:
        GDRIVE_CLIENT_FILE.chmod(0o600)
    except OSError:
        logger.warning("could not chmod the Drive client file", exc_info=True)
    logger.warning("Drive OAuth client configured (%s)", client_id[:24])


def _post_form(url: str, fields: dict) -> tuple[int, dict]:
    """POST an application/x-www-form-urlencoded body, parse a JSON reply.

    urllib rather than a new dependency, and the error body is READ rather
    than discarded: Google returns the part that matters ("authorization_
    pending", "slow_down", "expired_token") with a 4xx status, so treating a
    non-200 as simply "failed" would leave the polling loop unable to tell
    "keep waiting" from "give up".
    """
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"error": "network", "error_description": str(exc)[:200]}


def gdrive_device_start() -> tuple[bool, dict]:
    """Ask Google for a device code. Returns (ok, payload-or-error)."""
    client = read_gdrive_client()
    if not client:
        return False, {"error_description": "no OAuth client configured"}
    status, body = _post_form(GDRIVE_DEVICE_CODE_URL, {
        "client_id": client["client_id"],
        "scope": GDRIVE_DEVICE_SCOPE,
    })
    if status != 200 or "device_code" not in body:
        detail = body.get("error_description") or body.get("error") or f"HTTP {status}"
        # The one failure worth naming precisely, because it is the mistake
        # this setup invites: an OAuth client of the wrong TYPE. Google answers
        # "invalid_client / Invalid client type", which reads like a bad id
        # rather than the real problem -- the client must be created as
        # "TV and Limited Input devices", not Desktop or Web.
        if body.get("error") == "invalid_client":
            detail = ("that client is the wrong TYPE -- it must be created as "
                      "'TV and Limited Input devices', not Desktop or Web app")
        return False, {"error_description": detail}
    return True, body


def gdrive_device_poll_once(device_code: str) -> tuple[str, dict]:
    """One poll. Returns (state, payload) where state is one of "ok" (payload
    is the token), "pending", "slow_down", "denied", "expired", "error"."""
    client = read_gdrive_client()
    if not client:
        return "error", {"error_description": "no OAuth client configured"}
    status, body = _post_form(GDRIVE_TOKEN_URL, {
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    if status == 200 and body.get("access_token"):
        return "ok", body
    err = body.get("error", "")
    if err == "authorization_pending":
        return "pending", body
    if err == "slow_down":
        return "slow_down", body
    if err == "access_denied":
        return "denied", body
    if err == "expired_token":
        return "expired", body
    return "error", body


def gdrive_token_to_rclone(tok: dict) -> str:
    """Google's device-flow reply -> the token blob rclone stores.

    Google returns a RELATIVE lifetime ("expires_in": 3599); rclone stores an
    ABSOLUTE RFC3339 "expiry". Handing rclone the raw reply would leave it
    with no expiry at all, so it would keep presenting a dead access token
    instead of refreshing -- working at first, then failing an hour later for
    no visible reason.
    """
    expires_in = int(tok.get("expires_in") or 3600)
    expiry = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=expires_in)
    return json.dumps({
        "access_token": tok.get("access_token", ""),
        "token_type": tok.get("token_type", "Bearer"),
        "refresh_token": tok.get("refresh_token", ""),
        "expiry": expiry.isoformat().replace("+00:00", "Z"),
    })


def connect_gdrive_account(name: str, token_raw: str) -> tuple[bool, str]:
    """Register a new rclone Drive remote from a pasted OAuth token, verify it
    actually works, and roll back cleanly on any failure.

    Never hand-edits rclone.conf -- `rclone config create` is what actually
    understands the file's format, so a stray character in a pasted token
    can't corrupt every account already in it.
    """
    token_raw = token_raw.strip()
    try:
        token = json.loads(token_raw)
    except (json.JSONDecodeError, ValueError):
        return False, "that doesn't look like the JSON block rclone printed"
    if not isinstance(token, dict) or "access_token" not in token:
        return False, "no access_token in that JSON -- copy the whole printed block"
    if name in _list_gdrive_accounts():
        return False, f"'{name}' already exists -- pick a different label"

    try:
        create = _rclone_run("config", "create", name, "drive",
                             "scope=drive.file", f"token={token_raw}",
                             "--non-interactive")
    except Exception as exc:
        return False, f"couldn't run rclone: {exc}"
    if create.returncode != 0:
        return False, (create.stderr or create.stdout or "rclone config create failed").strip()[:400]

    def rollback() -> None:
        try:
            _rclone_run("config", "delete", name, timeout=15)
        except Exception:
            logger.warning("could not roll back the half-configured remote %s", name, exc_info=True)

    # Same duplicate-folder guard the manual README steps documented: if this
    # token turns out to be the SAME underlying Google account as one already
    # connected (a re-authorize by mistake, or a typo'd label), the shared
    # root folder already exists -- creating it again would silently split
    # future uploads across two "iSmart-LA Data" folders with nothing to
    # notice until files start landing in the wrong one.
    try:
        listing = _rclone_run("lsd", f"{name}:", timeout=30)
        if listing.returncode != 0:
            rollback()
            return False, "connected, but couldn't list the Drive root: " + \
                (listing.stderr or listing.stdout or "").strip()[:300]
        if GDRIVE_ROOT not in listing.stdout:
            mk = _rclone_run("mkdir", f"{name}:{GDRIVE_ROOT}", timeout=30)
            if mk.returncode != 0:
                rollback()
                return False, "connected, but couldn't create the data folder: " + \
                    (mk.stderr or mk.stdout or "").strip()[:300]
    except Exception as exc:
        rollback()
        return False, f"connected, but the folder check failed: {exc}"

    # Verify, don't assume -- the same principle applied to the node guard:
    # confirm this remote genuinely serves the folder before calling it done.
    try:
        verify = _rclone_run("lsd", f"{name}:{GDRIVE_ROOT}", timeout=30)
        if verify.returncode != 0:
            rollback()
            return False, "verification failed after setup: " + \
                (verify.stderr or verify.stdout or "").strip()[:300]
    except Exception as exc:
        rollback()
        return False, f"verification failed after setup: {exc}"

    return True, "connected and verified"


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Zero-token check of what the agent can currently do."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    if not _keys_configured():
        await update.message.reply_text(_t(lang,
            "⚠️ Write-mode keys not configured — the agent uses one fixed SSH key, so it "
            "can change things at any time. See README (\"Write mode\") to gate that.",
            "⚠️ Kunci write-mode belum diatur — agent pakai satu kunci SSH tetap, jadi bisa "
            "mengubah apa pun kapan saja. Lihat README (\"Write mode\") untuk memagarinya.",
        ))
        return
    until = write_mode_expires_at()
    if until:
        left = int((until - _dt.datetime.now().timestamp()) / 60) + 1
        await update.message.reply_text(_t(lang,
            f"🔓 Write mode OPEN — about {left} minute(s) left.",
            f"🔓 Write mode TERBUKA — sisa sekitar {left} menit.",
        ))
    else:
        await update.message.reply_text(_t(lang,
            "🔒 Read-only. Investigation, audits and reports work normally.\nNeed a change? /unlock [minutes]",
            "🔒 Read-only. Investigasi, audit, dan laporan tetap jalan normal.\nPerlu mengubah sesuatu? /unlock [menit]",
        ))


async def request_pin(update: Update, action: str, payload: dict, prompt: str) -> None:
    """Put a PIN keypad in front of a sensitive action.

    The action is NOT performed here -- it is parked with the session and only
    runs once the PIN checks out, in _pin_verified().
    """
    lang = _chat_lang(update)
    if not pin_is_set(update.effective_chat.id):
        await update.effective_message.reply_text(_t(lang,
            "🔢 No PIN is set yet, so sensitive actions are blocked.\n"
            "Set the owner PIN with /setpin (owner, private DM), or -- if this "
            "is a registered group -- its own with /setgrouppin.",
            "🔢 PIN belum diatur, jadi aksi sensitif diblokir.\n"
            "Atur PIN owner dengan /setpin (owner, DM pribadi), atau -- kalau "
            "ini grup terdaftar -- PIN grupnya sendiri dengan /setgrouppin.",
        ))
        return
    left = pin_locked_out()
    if left:
        await update.effective_message.reply_text(_t(lang,
            f"⛔ Too many wrong PIN attempts. Locked for another {left // 60 + 1} minute(s).",
            f"⛔ Terlalu banyak PIN salah. Terkunci {left // 60 + 1} menit lagi.",
        ))
        return
    token = _new_pin_session(action, payload, update.effective_chat.id)
    await update.effective_message.reply_text(
        f"{prompt}\n\n" + _t(lang,
            f"🔢 Enter your {PIN_LENGTH}-digit PIN:\n{_pin_masked(0)}",
            f"🔢 Masukkan {PIN_LENGTH}-digit PIN Anda:\n{_pin_masked(0)}",
        ),
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
    lang = _chat_lang(update)
    session = _pin_sessions.get(token)
    if not session:
        await query.answer(_t(lang, "This PIN entry expired — start again.",
                                   "PIN ini sudah kedaluwarsa — mulai lagi."), show_alert=True)
        return
    # Who may drive the keypad depends on the action: a registered group's own
    # admin may complete anything in PIN_ACTIONS_ALLOWED_IN_GROUP -- everything
    # else stays owner-only. This used to be its own hardcoded tuple here,
    # separate from PIN_ACTIONS_ALLOWED_IN_GROUP below, and the two drifted
    # apart: an action could be group-eligible by one list and not the other,
    # which is exactly what let /update reach the keypad for a group admin and
    # then dead-end at "only in a private DM" on the very next check. One set
    # now answers both questions.
    if session["action"] in PIN_ACTIONS_ALLOWED_IN_GROUP:
        may_touch = await _may_authorize_group_action(update, context)
    else:
        may_touch = _is_owner(update)
    if not may_touch:
        await query.answer(_t(lang, "Not permitted.", "Tidak diizinkan."), show_alert=True)
        return
    # ...but the action decides whether this is an acceptable PLACE to do it.
    # Only reachable for an owner outside PIN_ACTIONS_ALLOWED_IN_GROUP now (a
    # group admin already stopped at the check above) -- kept as its own step
    # so that case gets told WHY, rather than a bare "not permitted" that
    # would be actively misleading for someone who really is the owner.
    if (session["action"] not in PIN_ACTIONS_ALLOWED_IN_GROUP
            and not _is_trusted_origin(update)):
        await query.answer(_t(lang,
            "This action can only be confirmed in a private DM.",
            "Aksi ini cuma bisa dikonfirmasi lewat DM pribadi.",
        ), show_alert=True)
        return
    if session["expires"] < _dt.datetime.now().timestamp():
        _pin_sessions.pop(token, None)
        await query.answer(_t(lang, "Expired.", "Kedaluwarsa."), show_alert=True)
        await query.edit_message_text(_t(lang, "🔢 PIN entry expired.", "🔢 PIN sudah kedaluwarsa."))
        return

    if key == "cancel":
        _pin_sessions.pop(token, None)
        await query.answer()
        await query.edit_message_text(_t(lang, "✖️ Cancelled.", "✖️ Dibatalkan."))
        return
    if key == "del":
        session["digits"] = session["digits"][:-1]
    elif key.isdigit():
        session["digits"] += key
    await query.answer()

    header = (query.message.text or "").split("\n🔢")[0].split("\n●")[0].split("\n○")[0]
    header = header.split("\n❌")[0].rstrip() + _t(lang,
        f"\n\n🔢 {PIN_LENGTH}-digit PIN:", f"\n\n🔢 {PIN_LENGTH}-digit PIN Anda:")
    filled = len(session["digits"])
    if filled < PIN_LENGTH:
        await _redraw(query, header, token, filled)
        return

    entered = session["digits"]
    session["digits"] = ""

    # Choosing a NEW pin has nothing to verify against yet.
    if session["action"] in ("new_pin_capture", "new_pin_confirm",
                              "new_group_pin_capture", "new_group_pin_confirm"):
        await _pin_capture(update, query, session, token, entered)
        return

    if not verify_pin(entered, session["chat_id"]):
        session["attempts"] = session.get("attempts", 0) + 1
        if session["attempts"] >= PIN_MAX_ATTEMPTS:
            _pin_sessions.pop(token, None)
            _pin_lockout_until = _dt.datetime.now().timestamp() + PIN_LOCKOUT_SECONDS
            logger.error("PIN lockout triggered after %d failed attempts", PIN_MAX_ATTEMPTS)
            await query.edit_message_text(_t(lang,
                f"⛔ Wrong PIN {PIN_MAX_ATTEMPTS} times. Locked for "
                f"{PIN_LOCKOUT_SECONDS // 60} minutes.",
                f"⛔ PIN salah {PIN_MAX_ATTEMPTS} kali. Terkunci "
                f"{PIN_LOCKOUT_SECONDS // 60} menit.",
            ))
            return
        remaining = PIN_MAX_ATTEMPTS - session["attempts"]
        logger.warning("wrong PIN (%d attempt(s) left)", remaining)
        await _redraw(query, header, token, 0, _t(lang,
            f"\n❌ Wrong PIN — {remaining} attempt(s) left.",
            f"\n❌ PIN salah — {remaining} percobaan lagi.",
        ))
        return

    _pin_sessions.pop(token, None)
    await _pin_verified(update, context, query, session)


async def _pin_capture(update: Update, query, session: dict, token: str, entered: str) -> None:
    """Two-step entry for a new PIN: type it, then type it again. A mistyped PIN
    that locks you out of your own production changes is a bad afternoon.

    Shared by the owner's PIN (/setpin) and a specific group's own
    (/setgrouppin) -- same two-step shape, different destination for the
    result once both entries match.
    """
    lang = _chat_lang(update)
    is_group = session["action"] in ("new_group_pin_capture", "new_group_pin_confirm")
    capture_action = "new_group_pin_capture" if is_group else "new_pin_capture"
    confirm_action = "new_group_pin_confirm" if is_group else "new_pin_confirm"

    if session["action"] == capture_action:
        _pin_sessions.pop(token, None)
        confirm_token = _new_pin_session(confirm_action, {"first": entered},
                                         update.effective_chat.id)
        await query.edit_message_text(_t(lang,
            f"🔢 Enter the same {PIN_LENGTH} digits again to confirm:\n{_pin_masked(0)}",
            f"🔢 Masukkan lagi {PIN_LENGTH} digit yang sama untuk konfirmasi:\n{_pin_masked(0)}",
        ), reply_markup=_pin_keyboard(confirm_token, 0))
        return

    _pin_sessions.pop(token, None)
    if entered != session["payload"]["first"]:
        await query.edit_message_text(_t(lang,
            f"❌ The two entries didn't match. Run /{'setgrouppin' if is_group else 'setpin'} again.",
            f"❌ Dua isian tidak sama. Ulangi /{'setgrouppin' if is_group else 'setpin'}.",
        ))
        return

    if is_group:
        chat = update.effective_chat
        set_group_pin(session["chat_id"], entered, update.effective_user.id)
        await query.edit_message_text(_t(lang,
            f"✅ PIN set for <b>{_tg_escape(chat.title or session['chat_id'])}</b>. It now "
            "guards sensitive actions confirmed from this group specifically -- the "
            "owner's own PIN still works here too, as a master credential.\n\n"
            "It is stored only as a salted hash, and it is never typed into the chat.",
            f"✅ PIN tersimpan untuk <b>{_tg_escape(chat.title or session['chat_id'])}</b>. "
            "Sekarang menjaga aksi sensitif yang dikonfirmasi dari grup ini secara "
            "khusus -- PIN owner tetap berlaku di sini juga, sebagai kredensial utama.\n\n"
            "Disimpan hanya sebagai hash yang di-salt, dan tidak pernah diketik di chat.",
        ), parse_mode="HTML")
        return

    set_pin(entered)
    await query.edit_message_text(_t(lang,
        "✅ PIN set. It now guards scheduled tasks and /unlock.\n"
        "It is stored only as a salted hash, and it is never typed into the chat.\n\n"
        "Run /start to see what's left.",
        "✅ PIN tersimpan. Sekarang menjaga task terjadwal dan /unlock.\n"
        "Disimpan hanya sebagai hash yang di-salt, dan tidak pernah diketik di chat.\n\n"
        "Jalankan /start untuk lihat sisanya.",
    ))


async def _pin_verified(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        query, session: dict) -> None:
    """PIN checked out -- carry out the action that was waiting on it."""
    action, payload = session["action"], session["payload"]

    if action == "change_pin_start":
        await _begin_new_pin(update, query)
        return

    if action == "change_group_pin_start":
        await _begin_new_group_pin(update, query)
        return

    if action == "unlock_and_resume":
        await _do_unlock_and_resume(update, context, query)
        return

    lang = _chat_lang(update)
    if action == "rmboundary":
        rule = payload["rule"]
        write_boundaries([x for x in read_boundaries() if x != rule])
        await query.edit_message_text(_t(lang, f"🚧 Removed: {_tg_escape(rule)}",
                                              f"🚧 Dihapus: {_tg_escape(rule)}"), parse_mode="HTML")
        return

    if action == "addserver":
        await _begin_addserver(update, query)
        return

    if action == "addmcp":
        register_mcp_server(payload["name"], payload["command"], payload["args"])
        await query.edit_message_text(_t(lang,
            f"🔌 <b>{_tg_escape(payload['name'])}</b> registered.\n\n"
            "<i>Takes effect on the next new conversation -- /new applies it now.</i>",
            f"🔌 <b>{_tg_escape(payload['name'])}</b> terdaftar.\n\n"
            "<i>Berlaku di percakapan baru berikutnya -- /new untuk langsung terapkan.</i>",
        ), parse_mode="HTML")
        return

    if action == "schedule_install":
        item = payload["item"]
        try:
            install_schedule(item, update.effective_user.id)
        except Exception as exc:
            logger.exception("schedule install failed")
            await query.edit_message_text(_t(lang, f"⚠️ Could not install: {exc}", f"⚠️ Gagal pasang: {exc}"))
            return
        await query.edit_message_text(_t(lang,
            f"✅ Installed <b>{_tg_escape(item['name'])}</b> — runs "
            f"<code>{_tg_escape(item['when'])}</code>.\n"
            f"See /schedules, remove with /unschedule {_tg_escape(item['name'])}.",
            f"✅ Terpasang <b>{_tg_escape(item['name'])}</b> — jalan "
            f"<code>{_tg_escape(item['when'])}</code>.\n"
            f"Lihat /schedules, hapus dengan /unschedule {_tg_escape(item['name'])}.",
        ), parse_mode="HTML")
        return

    if action == "update":
        await query.edit_message_text(_t(lang,
            "\U0001f4e6 Pulling the new version\u2026",
            "\U0001f4e6 Menarik versi baru\u2026",
        ))
        loop = asyncio.get_running_loop()
        ok, before, detail = await loop.run_in_executor(None, apply_update)
        if not ok:
            await query.edit_message_text(_t(lang,
                f"\u26a0\ufe0f Update failed, nothing changed:\n<pre>{_tg_escape(detail)}</pre>",
                f"\u26a0\ufe0f Update gagal, tidak ada yang berubah:\n<pre>{_tg_escape(detail)}</pre>",
            ), parse_mode="HTML")
            return
        new_version = current_version()
        # Hand the confirmation to the process that comes back, so what the
        # operator sees is proof the new build actually starts.
        try:
            UPDATE_ANNOUNCE_FILE.write_text(json.dumps({
                "chat_id": update.effective_chat.id,
                "lang": lang,
                "from": before[:12],
                "to": new_version,
            }))
        except OSError:
            logger.warning("could not write the post-update announcement", exc_info=True)
        await query.edit_message_text(_t(lang,
            f"\u2705 Updated to <b>{_tg_escape(new_version)}</b>. Restarting\u2026",
            f"\u2705 Terupdate ke <b>{_tg_escape(new_version)}</b>. Restart\u2026",
        ), parse_mode="HTML")
        logger.warning("UPDATE applied %s -> %s, restarting", before[:12], new_version)
        # Before the restart, not after: the unit systemd is about to load
        # should be the one this release ships, or a hardening change would
        # sit in the repo unapplied while the operator believed otherwise.
        logger.warning("systemd unit refresh: %s", refresh_systemd_unit())
        # --no-block: this process lives inside the unit systemd is about to
        # stop, so a blocking restart would kill the very command issuing it.
        subprocess.Popen(["sudo", "-n", "systemctl", "--no-block", "restart", SERVICE_NAME])
        return

    if action == "unlock":
        try:
            until = unlock_write_mode(payload["minutes"], max_minutes=_effective_unlock_cap(update))
        except OSError as exc:
            logger.exception("unlock failed")
            await query.edit_message_text(_t(lang, f"⚠️ Could not unlock: {exc}", f"⚠️ Gagal buka: {exc}"))
            return
        left = int((until - _dt.datetime.now().timestamp()) / 60) + 1
        await query.edit_message_text(_t(lang,
            f"🔓 Write mode open for {left} minute(s). It re-locks by itself; "
            "/lock closes it sooner. Hard boundaries still apply.",
            f"🔓 Write mode terbuka {left} menit. Terkunci sendiri lagi nanti; "
            "/lock untuk tutup lebih awal. Hard boundaries tetap berlaku.",
        ))
        return

    logger.error("unknown PIN action: %s", action)
    await query.edit_message_text(_t(lang, "⚠️ Internal error: unknown action.", "⚠️ Error internal: aksi tidak dikenal."))


async def _begin_new_pin(update: Update, query=None) -> None:
    token = _new_pin_session("new_pin_capture", {}, update.effective_chat.id)
    chat = update.effective_chat
    in_group = bool(chat and chat.type != "private")
    lang = _chat_lang(update)
    text = (
        _t(lang, f"🔢 Choose a new {PIN_LENGTH}-digit PIN.\n", f"🔢 Pilih PIN baru {PIN_LENGTH} digit.\n")
        + _t(lang,
             "Avoid birthdays and 123456 — this is the last gate in front of production changes.\n",
             "Hindari tanggal lahir dan 123456 — ini gerbang terakhir sebelum perubahan production.\n",
        )
        + (_t(lang,
             "\n\u2139\ufe0f You're doing this in a group. Your digits stay private "
             "(they never become a message), but the group can see that you're "
             "setting a PIN right now.\n",
             "\n\u2139\ufe0f Kamu lagi di grup. Digitnya tetap privat "
             "(tidak pernah jadi pesan), tapi grup bisa lihat kamu sedang "
             "mengatur PIN sekarang.\n",
        ) if in_group else "")
        + f"{_pin_masked(0)}"
    )
    kb = _pin_keyboard(token, 0)
    if query is not None:
        await query.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


async def cmd_setpin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or change the OWNER's PIN -- the master credential that works
    everywhere: DM, and every group, on top of (never instead of) that
    group's own if it has set one.

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
        await request_pin(update, "change_pin_start", {}, _t(_chat_lang(update),
            "🔢 Changing your PIN. First, confirm the CURRENT one.",
            "🔢 Mengganti PIN. Konfirmasi dulu PIN yang SEKARANG.",
        ))
        return
    await _begin_new_pin(update)


async def _begin_new_group_pin(update: Update, query=None) -> None:
    token = _new_pin_session("new_group_pin_capture", {}, update.effective_chat.id)
    lang = _chat_lang(update)
    text = (
        _t(lang, f"🔢 Choose this group's new {PIN_LENGTH}-digit PIN.\n",
                 f"🔢 Pilih PIN baru {PIN_LENGTH} digit untuk grup ini.\n")
        + _t(lang,
             "Avoid birthdays and 123456. Your digits stay private (they never "
             "become a message), but the group can see someone is setting a "
             "PIN right now.\n",
             "Hindari tanggal lahir dan 123456. Digitnya tetap privat (tidak "
             "pernah jadi pesan), tapi grup bisa lihat ada yang sedang "
             "mengatur PIN sekarang.\n",
        )
        + f"{_pin_masked(0)}"
    )
    kb = _pin_keyboard(token, 0)
    if query is not None:
        await query.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


async def cmd_setgrouppin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or change THIS group's own PIN -- separate from the owner's, and
    only usable to confirm actions from inside this specific group. The
    owner's own PIN keeps working here too, as a master credential; this is
    additive, not a replacement.
    """
    lang = _chat_lang(update)
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.effective_message.reply_text(_t(lang,
            "This command is for a group -- run it inside the group you "
            "want to give its own PIN.",
            "Command ini buat grup -- jalankan di dalam grup yang mau "
            "diberi PIN sendiri.",
        ))
        return
    if chat.id not in ALLOWED_GROUP_IDS:
        await update.effective_message.reply_text(_t(lang,
            "This group isn't registered yet -- run /registergroup first.",
            "Grup ini belum terdaftar -- jalankan /registergroup dulu.",
        ))
        return
    if not _is_owner(update) and not await _is_group_admin(update, context):
        await update.effective_message.reply_text(_t(lang,
            "🔒 Bot owner or a group admin only.",
            "🔒 Cuma pemilik bot atau admin grup.",
        ))
        return
    if group_pin_is_set(chat.id):
        await request_pin(update, "change_group_pin_start", {}, _t(lang,
            "🔢 Changing this group's PIN. First, confirm the CURRENT one "
            "(this group's own, or the owner's).",
            "🔢 Mengganti PIN grup ini. Konfirmasi dulu PIN yang SEKARANG "
            "(punya grup ini, atau punya owner).",
        ))
        return
    await _begin_new_group_pin(update)


async def cmd_rmgrouppin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove this group's own PIN. The group is never left unprotected by
    this -- it falls back to the owner's, exactly like a group that never set
    one of its own. No PIN confirmation needed to remove one: this only ever
    narrows who can confirm sensitive actions here back down to whoever knows
    the owner's PIN, never widens it.
    """
    lang = _chat_lang(update)
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.effective_message.reply_text(_t(lang,
            "This command is for a group -- run it inside the group whose "
            "own PIN you want to remove.",
            "Command ini buat grup -- jalankan di dalam grup yang PIN-nya "
            "mau dihapus.",
        ))
        return
    if not _is_owner(update) and not await _is_group_admin(update, context):
        await update.effective_message.reply_text(_t(lang,
            "🔒 Bot owner or a group admin only.",
            "🔒 Cuma pemilik bot atau admin grup.",
        ))
        return
    if not remove_group_pin(chat.id):
        await update.effective_message.reply_text(_t(lang,
            "This group doesn't have its own PIN set.",
            "Grup ini belum punya PIN sendiri.",
        ))
        return
    await update.effective_message.reply_text(_t(lang,
        "✅ This group's own PIN is removed. Sensitive actions here now "
        "need the owner's PIN, same as a group that never set one.",
        "✅ PIN grup ini sudah dihapus. Aksi sensitif di sini sekarang "
        "butuh PIN owner, sama seperti grup yang belum pernah punya PIN sendiri.",
    ))


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
        lang = _chat_lang(update)
        warn = ""
        if item["needs_write"]:
            warn = _t(lang,
                "\n\n⚠️ This task asks for WRITE access, so it can change things "
                "with nobody watching. Only approve that if it genuinely needs to.",
                "\n\n⚠️ Task ini minta akses TULIS, jadi bisa mengubah sesuatu "
                "tanpa ada yang mengawasi. Setujui cuma kalau memang perlu.",
            )
        await _msg(update).reply_text(
            _t(lang, f"🗓 <b>Install this scheduled task?</b>\n\n", f"🗓 <b>Pasang jadwal ini?</b>\n\n")
            + _t(lang, f"<b>Name:</b> ", f"<b>Nama:</b> ") + f"<code>{_tg_escape(item['name'])}</code>\n"
            + _t(lang, f"<b>When:</b> ", f"<b>Kapan:</b> ") + f"<code>{_tg_escape(item['when'])}</code>\n"
            + _t(lang, f"<b>Runs:</b> ", f"<b>Menjalankan:</b> ") + f"<code>{_tg_escape(item['run'][:300])}</code>\n"
            + _t(lang, f"<b>Write access:</b> ", f"<b>Akses tulis:</b> ") + ('YES' if item['needs_write'] else 'no')
            + warn,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(_t(lang, "✅ Install", "✅ Pasang"), callback_data=f"sched_ok:{token}"),
                InlineKeyboardButton(_t(lang, "✖️ Cancel", "✖️ Batal"), callback_data=f"sched_no:{token}"),
            ]]),
        )


async def cmd_schedule_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, token = query.data.partition(":")
    lang = _chat_lang(update)
    if not await _may_authorize_group_action(update, context):
        await query.answer(_t(lang, "Bot owner, or a registered group's own admin.",
                                   "Pemilik bot, atau admin dari grup yang sudah terdaftar."), show_alert=True)
        return
    item = _pending_schedules.pop(token, None)
    if not item:
        await query.answer()
        await query.edit_message_text(_t(lang,
            "That proposal has expired — ask again if you still want it.",
            "Proposal itu sudah kedaluwarsa — minta lagi kalau masih mau.",
        ))
        return
    if action == "sched_no":
        await query.answer()
        await query.edit_message_text(_t(lang, f"✖️ Not installed: {item['name']}", f"✖️ Tidak dipasang: {item['name']}"))
        return
    # The tap alone is not the authorisation. It only says WHICH proposal; the
    # PIN says a person -- not just a logged-in device -- actually wants it.
    await query.answer()
    await query.edit_message_text(_t(lang,
        f"🗓 Installing <b>{_tg_escape(item['name'])}</b> — confirm with your PIN.",
        f"🗓 Memasang <b>{_tg_escape(item['name'])}</b> — konfirmasi dengan PIN.",
    ), parse_mode="HTML")
    await request_pin(
        update, "schedule_install", {"item": item}, _t(lang,
            f"🗓 Confirm installing scheduled task <b>{_tg_escape(item['name'])}</b>.",
            f"🗓 Konfirmasi pasang task terjadwal <b>{_tg_escape(item['name'])}</b>.",
        ),
    )


async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everything that runs on a timer. Zero tokens -- read straight from the
    registry and the crontab, no model involved."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    items = _read_schedules()
    lines: list[str] = []
    if items:
        lines.append(_t(lang, f"🗓 <b>Scheduled tasks ({len(items)})</b>\n", f"🗓 <b>Task terjadwal ({len(items)})</b>\n"))
        for it in items:
            flag = _t(lang, " ⚠️ <i>write access</i>", " ⚠️ <i>akses tulis</i>") if it.get("needs_write") else ""
            lines.append(
                f"• <b>{_tg_escape(it['name'])}</b>{flag}\n"
                f"  <code>{_tg_escape(it['when'])}</code> — "
                + _t(lang, f"since {it.get('created_at','?')}", f"sejak {it.get('created_at','?')}") + "\n"
                f"  <code>{_tg_escape(it['run'][:160])}</code>"
            )
    else:
        lines.append(_t(lang, "🗓 No scheduled tasks registered.", "🗓 Belum ada task terjadwal."))

    orphans = unmanaged_cron_lines()
    if orphans:
        lines.append(_t(lang,
            "\n⚠️ <b>Unmanaged cron entries</b> — these run on a timer but were not "
            "installed through this bot, so they cannot be removed with /unschedule:",
            "\n⚠️ <b>Cron entry tidak terkelola</b> — ini jalan terjadwal tapi tidak dipasang "
            "lewat bot ini, jadi tidak bisa dihapus dengan /unschedule:",
        ))
        for o in orphans[:10]:
            lines.append(f"  <code>{_tg_escape(o[:160])}</code>")
        lines.append(_t(lang, "<i>Use /adopt to bring them under management.</i>",
                              "<i>Pakai /adopt untuk membawanya ke pengelolaan.</i>"))

    if items:
        lines.append(_t(lang, "\nRemove one: /unschedule &lt;name&gt;", "\nHapus satu: /unschedule &lt;nama&gt;"))
    await _reply_chunked(update, "\n".join(lines), already_html=True)


async def cmd_unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _chat_lang(update)
    if not await _may_authorize_group_action(update, context):
        await update.message.reply_text(_t(lang,
            "🔒 Bot owner, or a registered group's own admin.",
            "🔒 Pemilik bot, atau admin dari grup yang sudah terdaftar.",
        ))
        return
    name = (context.args[0] if context.args else "").strip()
    if not name:
        await update.message.reply_text(_t(lang,
            "Usage: /unschedule <name>\nSee names with /schedules",
            "Pakai: /unschedule <nama>\nLihat nama di /schedules",
        ))
        return
    if remove_schedule(name):
        await update.message.reply_text(_t(lang, f"🗑 Removed scheduled task: {name}", f"🗑 Task terjadwal dihapus: {name}"))
    else:
        await update.message.reply_text(_t(lang,
            f"No scheduled task named '{name}'. See /schedules",
            f"Tidak ada task terjadwal bernama '{name}'. Lihat /schedules",
        ))


async def cmd_adopt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pull pre-existing cron entries into the registry so they become visible
    and removable. Written for a real case: the agent installed a daily report
    before this feature existed, and it was invisible from Telegram."""
    lang = _chat_lang(update)
    if not _is_trusted_origin(update):
        await update.message.reply_text(_t(lang,
            "🔒 Only the bot owner, in a private DM.",
            "🔒 Hanya pemilik bot, lewat DM pribadi.",
        ))
        return
    orphans = unmanaged_cron_lines()
    if not orphans:
        await update.message.reply_text(_t(lang,
            "Nothing to adopt — every cron entry is already managed.",
            "Tidak ada yang perlu di-adopt — semua cron entry sudah terkelola.",
        ))
        return
    items = _read_schedules()
    known = {s["name"] for s in items}
    adopted: list[str] = []
    adopted_raw_lines: set[str] = set()
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
        adopted_raw_lines.add(line.strip())
    if not adopted:
        await update.message.reply_text(_t(lang,
            "Found cron entries but could not parse any of them.",
            "Ada cron entry ditemukan tapi tidak ada yang bisa di-parse.",
        ))
        return
    # strip_lines removes the ORIGINAL raw line now that it lives in the
    # managed block under a new name -- without this it would run twice.
    _rebuild_crontab(items, strip_lines=adopted_raw_lines)
    _write_schedules(items)
    await update.message.reply_text(_t(lang,
        f"✅ Adopted {len(adopted)} entry/entries: {', '.join(adopted)}\n"
        "They now show in /schedules and can be removed with /unschedule.",
        f"✅ Ter-adopt {len(adopted)} entry: {', '.join(adopted)}\n"
        "Sekarang muncul di /schedules dan bisa dihapus dengan /unschedule.",
    ))


async def cmd_providers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The live fallback chain and each tier's current health. Zero tokens --
    read straight from config and the in-memory cooldown table."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    lines = [f"\U0001f9e9 <b>{_t(lang, 'Fallback chain', 'Rantai fallback')} ({len(TIERS)} {_t(lang, 'tier(s)', 'tingkat')})</b>", ""]
    for i, t in enumerate(TIERS, 1):
        if _tier_available(t["model"]):
            state = _t(lang, "✅ ready", "✅ siap")
        else:
            until, fails = _tier_cooldown[t["model"]]
            mins = int((until - _dt.datetime.now().timestamp()) / 60) + 1
            state = _t(lang,
                f"⏸ cooling down ~{mins}m (after {fails} failure(s))",
                f"⏸ istirahat ~{mins}m (setelah {fails} kegagalan)",
            )
        role = (_t(lang, "primary", "utama") if i == 1
                else _t(lang, "last resort", "terakhir") if i == len(TIERS)
                else _t(lang, f"fallback #{i - 1}", f"cadangan #{i - 1}"))
        lines.append(
            f"{i}. <b>{_tg_escape(t['label'])}</b> — {role}\n"
            f"   <code>{_tg_escape(t['provider'])}</code> / <code>{_tg_escape(t['model'])}</code>\n"
            f"   {state}"
        )
    lines.append(_t(lang,
        "\nReplies are tagged with whichever tier answered. Anything other than "
        f"<b>{_tg_escape(TIERS[0]['label'])}</b> means the ones above it were unavailable.",
        "\nTiap balasan ditandai tingkat yang menjawab. Kalau bukan "
        f"<b>{_tg_escape(TIERS[0]['label'])}</b>, berarti yang di atasnya sedang tidak bisa dipakai.",
    ))
    lines.append(_t(lang,
        "\n<i>Change the chain by editing TIERS in .env, then restart.</i>",
        "\n<i>Ubah rantainya lewat TIERS di .env, lalu restart.</i>",
    ))
    await _reply_chunked(update, "\n".join(lines), already_html=True)


async def cmd_addserver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register a machine the agent may reach. PIN first -- this grants access."""
    if not _may_run_setup(update):
        return
    lang = _chat_lang(update)
    if not _is_owner(update) and not await _is_group_admin(update, context):
        await update.message.reply_text(_t(lang, "🔒 Bot owner or a group admin only.",
                                              "🔒 Cuma pemilik bot atau admin grup."))
        return
    if pin_is_set(update.effective_chat.id):
        await request_pin(update, "addserver", {}, _t(lang, "➕ Adding a server.", "➕ Menambah server."))
    else:
        # Nothing to verify against yet. Say so plainly instead of pretending
        # a gate exists -- and nudge, because this is exactly what it protects.
        await update.message.reply_text(_t(lang,
            "⚠️ No PIN is set, so this isn't protected yet. Set one with /setpin "
            "when you're done — it's what stops a stolen Telegram session from "
            "adding a server nobody noticed.",
            "⚠️ Belum ada PIN, jadi ini belum terlindungi. Atur satu dengan /setpin "
            "kalau sudah selesai — itu yang mencegah sesi Telegram yang dicuri "
            "menambah server tanpa disadari.",
        ))
        await _begin_addserver(update)


async def _begin_addserver(update: Update, query=None) -> None:
    _server_wizard[update.effective_chat.id] = {
        "step": "kind", "data": {},
        "expires": _dt.datetime.now().timestamp() + SERVER_WIZARD_TTL,
    }
    lang = _chat_lang(update)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"srv:kind:{key}")]
         for key, label in SERVER_KINDS.items()]
        + [[InlineKeyboardButton(_t(lang, "✖️ Cancel", "✖️ Batal"), callback_data="srv:cancel:")]]
    )
    text = _t(lang, "➕ <b>Add a server</b>\n\nWhat kind of machine is it?",
                    "➕ <b>Tambah server</b>\n\nJenis mesinnya apa?")
    if query is not None:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cmd_server_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = _chat_lang(update)
    _, action, value = query.data.split(":", 2)
    chat_id = update.effective_chat.id
    if not _may_run_setup(update):
        await query.answer(_t(lang, "Not permitted.", "Tidak diizinkan."), show_alert=True)
        return
    await query.answer()

    if action == "cancel":
        _server_wizard.pop(chat_id, None)
        await query.edit_message_text(_t(lang, "✖️ Cancelled. Nothing was saved.",
                                              "✖️ Dibatalkan. Tidak ada yang disimpan."))
        return

    state = _server_wizard.get(chat_id)
    if not state:
        await query.edit_message_text(_t(lang, "That form expired. Run /addserver again.",
                                              "Form itu sudah kedaluwarsa. Jalankan /addserver lagi."))
        return
    data = state["data"]

    if action == "kind":
        data["kind"] = value
        if value == "hypervisor":
            state["step"] = "flavour"
            await query.edit_message_text(
                _t(lang, "➕ <b>Add a server</b>\n\nWhich hypervisor?",
                         "➕ <b>Tambah server</b>\n\nHypervisor yang mana?"), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(l, callback_data=f"srv:flavour:{k}")]
                     for k, l in HYPERVISOR_FLAVOURS.items()]
                    + [[InlineKeyboardButton(_t(lang, "✖️ Cancel", "✖️ Batal"), callback_data="srv:cancel:")]]),
            )
            return
        data["flavour"] = value
        state["step"] = "name"
        await query.edit_message_text(_srv_prompt("name", lang), parse_mode="HTML")
        return

    if action == "flavour":
        data["flavour"] = value
        state["step"] = "name"
        await query.edit_message_text(_srv_prompt("name", lang), parse_mode="HTML")
        return

    if action == "cluster":
        data["cluster_wide"] = (value == "yes")
        state["step"] = "discover"
        if data.get("flavour") != "proxmox":
            await _finish_addserver(update, query)
            return
        await query.edit_message_text(
            _t(lang,
               "🔍 Scan the cluster now?\n\nRead-only — it just asks which nodes exist "
               "and how many guests are running. No changes, and it doesn't need write mode.",
               "🔍 Scan cluster sekarang?\n\nRead-only — cuma tanya node mana saja yang ada "
               "dan berapa guest yang jalan. Tidak ada perubahan, tidak butuh write mode.",
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(_t(lang, "Yes, scan", "Ya, scan"), callback_data="srv:discover:yes"),
                InlineKeyboardButton(_t(lang, "Skip", "Lewati"), callback_data="srv:discover:no"),
            ]]),
        )
        return

    if action == "discover":
        if value == "no":
            await _finish_addserver(update, query)
            return
        await query.edit_message_text(_t(lang, "🔍 Scanning… this can take a moment.",
                                              "🔍 Scanning… bisa makan waktu sebentar."))
        loop = asyncio.get_running_loop()
        ok, detail, nodes = await loop.run_in_executor(
            None, discover_proxmox, data["host"], data["user"], data["port"])
        if ok:
            data["cluster_hosts"] = [n for n in nodes if n != data["host"]]
            data["discovery"] = detail
        await _finish_addserver(update, query, discovery=(detail if ok else f"scan failed: {detail}"))
        return

    if action == "unsafe":
        # Reached only by deliberately tapping through the warning above.
        data["guard"] = None
        logger.warning("server %s added WITHOUT the read-only guard, by explicit choice",
                       data.get("host"))
        data["probe"] = data.get("probe") or "connected (unprotected)"
        if data.get("flavour") == "proxmox":
            state["step"] = "cluster"
            await query.edit_message_text(_t(lang,
                "Added without protection. Does this same key reach every node?",
                "Ditambahkan tanpa perlindungan. Apakah key yang sama menjangkau semua node?"),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(_t(lang, "Yes", "Ya"), callback_data="srv:cluster:yes"),
                    InlineKeyboardButton(_t(lang, "No", "Tidak"), callback_data="srv:cluster:no"),
                ]]))
        else:
            await _finish_addserver(update, query)
        return

    if action == "test":
        await query.edit_message_text(_t(lang, "🔌 Testing the connection…", "🔌 Menguji koneksi…"))
        loop = asyncio.get_running_loop()
        ok, detail = await loop.run_in_executor(
            None, test_server_ssh, data["host"], data["user"], data["port"],
            20, data.get("key"))
        if not ok:
            await query.edit_message_text(
                _t(lang,
                   f"❌ Couldn't connect:\n<pre>{_tg_escape(detail)}</pre>\n\n"
                   "Usually the public key isn't in place yet, or the user/port is off.",
                   f"❌ Gagal konek:\n<pre>{_tg_escape(detail)}</pre>\n\n"
                   "Biasanya public key belum terpasang, atau user/port-nya salah.",
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(_t(lang, "🔁 Test again", "🔁 Tes lagi"), callback_data="srv:test:"),
                    InlineKeyboardButton(_t(lang, "✖️ Cancel", "✖️ Batal"), callback_data="srv:cancel:"),
                ]]))
            return
        # Put the guard in place and PROVE it refuses a write before this host
        # is treated as managed. The failure this whole feature exists to stop
        # was silent: everything looked configured, and nothing was.
        if _keys_configured() and not data.get("key"):
            await query.edit_message_text(_t(lang,
                "🔒 Installing the read-only guard and verifying it really refuses writes\u2026",
                "🔒 Memasang guard read-only dan memastikan tulis benar-benar ditolak\u2026"))
            gok, gdetail = await loop.run_in_executor(
                None, secure_server, data["host"], data["user"], data["port"])
            data["guard"] = gdetail if gok else None
            if not gok:
                await query.edit_message_text(
                    _t(lang,
                       f"\u26a0\ufe0f <b>Connected, but this host is NOT protected.</b>\n\n"
                       f"<pre>{_tg_escape(gdetail)}</pre>\n\n"
                       "Without the guard, a destructive request reaches this host with "
                       "nothing to stop it -- no approval button, no PIN. Retry, or add it "
                       "anyway and accept that.",
                       f"\u26a0\ufe0f <b>Terhubung, tapi host ini TIDAK terlindungi.</b>\n\n"
                       f"<pre>{_tg_escape(gdetail)}</pre>\n\n"
                       "Tanpa guard, perintah merusak sampai ke host ini tanpa penahan apa pun "
                       "-- tanpa tombol persetujuan, tanpa PIN. Coba lagi, atau tetap tambahkan "
                       "dengan menerima risiko itu.",
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(_t(lang, "\U0001f501 Retry protection", "\U0001f501 Coba lindungi lagi"),
                                              callback_data="srv:test:")],
                        [InlineKeyboardButton(_t(lang, "\u26a0\ufe0f Add unprotected", "\u26a0\ufe0f Tambah tanpa perlindungan"),
                                              callback_data="srv:unsafe:")],
                        [InlineKeyboardButton(_t(lang, "\u2716\ufe0f Cancel", "\u2716\ufe0f Batal"),
                                              callback_data="srv:cancel:")],
                    ]))
                return
        data["probe"] = detail
        if data.get("flavour") == "proxmox":
            state["step"] = "cluster"
            await query.edit_message_text(
                _t(lang,
                   f"✅ Connected. <code>{_tg_escape(detail)}</code>\n\n"
                   "Does this same key reach <b>every node</b> in the cluster?",
                   f"✅ Terhubung. <code>{_tg_escape(detail)}</code>\n\n"
                   "Apakah key yang sama ini bisa menjangkau <b>semua node</b> di cluster?",
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(_t(lang, "Yes", "Ya"), callback_data="srv:cluster:yes"),
                    InlineKeyboardButton(_t(lang, "No, just this one", "Tidak, ini saja"), callback_data="srv:cluster:no"),
                ]]))
            return
        await _finish_addserver(update, query)
        return


def _srv_prompt(step: str, lang: str = "id") -> str:
    return {
        "en": {
            "name": "➕ <b>Name</b>\n\nA short label for this machine — lowercase, digits, "
                    "<code>-</code> or <code>_</code>.\n\ne.g. <code>pm-cluster</code>\n\n"
                    "Send it as a message. /cancel to stop.",
            "host": "➕ <b>Address</b>\n\nIP or hostname.\n\n<i>Prefer the raw IP unless you've "
                    "checked what DNS returns — wildcard records pointing at a proxy are common, "
                    "and then you'd be talking to the proxy instead of the machine.</i>",
            "user": "➕ <b>SSH user</b>\n\ne.g. <code>root</code> or <code>ubuntu</code>",
            "port": "➕ <b>SSH port</b>\n\nSend <code>22</code> for the default.",
        },
        "id": {
            "name": "➕ <b>Nama</b>\n\nLabel singkat untuk mesin ini — huruf kecil, angka, "
                    "<code>-</code> atau <code>_</code>.\n\ncontoh: <code>pm-cluster</code>\n\n"
                    "Kirim sebagai pesan. /cancel untuk berhenti.",
            "host": "➕ <b>Alamat</b>\n\nIP atau hostname.\n\n<i>Utamakan IP mentah kecuali sudah "
                    "dicek apa yang dikembalikan DNS — record wildcard yang mengarah ke proxy itu "
                    "umum, nanti malah bicara ke proxy-nya, bukan mesinnya.</i>",
            "user": "➕ <b>User SSH</b>\n\ncontoh: <code>root</code> atau <code>ubuntu</code>",
            "port": "➕ <b>Port SSH</b>\n\nKirim <code>22</code> untuk default.",
        },
    }[lang if lang == "en" else "id"][step]


async def _handle_server_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Text steps of /addserver. Returns True if the message was consumed."""
    chat_id = update.effective_chat.id
    state = _server_wizard.get(chat_id)
    lang = _chat_lang(update)
    if state and state["step"] == "authorize":
        text = (update.message.text or "").strip()
        if text.lower().startswith("usekey "):
            name = text.split(None, 1)[1].strip()
            key = Path.home() / ".ssh" / name
            if not key.exists() or not key.with_suffix(".pub").exists():
                await update.message.reply_text(_t(lang,
                    f"No keypair named '{name}' on this host. Send just the name, without .pub",
                    f"Tidak ada keypair bernama '{name}' di host ini. Kirim namanya saja, tanpa .pub",
                ))
                return True
            state["data"]["key"] = str(key)
            await update.message.reply_text(_t(lang,
                f"🔑 Using <code>{_tg_escape(name)}</code>. Tap Test above.",
                f"🔑 Memakai <code>{_tg_escape(name)}</code>. Tap Test di atas.",
            ), parse_mode="HTML")
            return True
        return False
    if not state or state["step"] not in ("name", "host", "user", "port"):
        return False
    if state["expires"] < _dt.datetime.now().timestamp():
        _server_wizard.pop(chat_id, None)
        await update.message.reply_text(_t(lang, "⌛ That form expired. Run /addserver again.",
                                              "⌛ Form itu sudah kedaluwarsa. Jalankan /addserver lagi."))
        return True

    text = (update.message.text or "").strip()
    if text.lower() in ("/cancel", "cancel", "batal"):
        _server_wizard.pop(chat_id, None)
        await update.message.reply_text(_t(lang, "✖️ Cancelled. Nothing was saved.",
                                              "✖️ Dibatalkan. Tidak ada yang disimpan."))
        return True

    step, data = state["step"], state["data"]
    if step == "name":
        if not _NAME_RE.match(text):
            await update.message.reply_text(_t(lang, "Lowercase letters, digits, - and _ only. Try again.",
                                                  "Huruf kecil, angka, - dan _ saja. Coba lagi."))
            return True
        if any(s["name"] == text for s in _read_servers()):
            await update.message.reply_text(_t(lang, f"'{text}' already exists. Pick another name.",
                                                  f"'{text}' sudah ada. Pilih nama lain."))
            return True
        data["name"] = text
        state["step"] = "host"
        await update.message.reply_text(_srv_prompt("host", lang), parse_mode="HTML")
        return True

    if step == "host":
        if not _HOST_RE.match(text):
            await update.message.reply_text(_t(lang, "That doesn't look like an IP or hostname. Try again.",
                                                  "Itu tidak seperti IP atau hostname. Coba lagi."))
            return True
        data["host"] = text
        state["step"] = "user"
        await update.message.reply_text(_srv_prompt("user", lang), parse_mode="HTML")
        return True

    if step == "user":
        if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", text):
            await update.message.reply_text(_t(lang, "That doesn't look like a username. Try again.",
                                                  "Itu tidak seperti username. Coba lagi."))
            return True
        data["user"] = text
        state["step"] = "port"
        await update.message.reply_text(_srv_prompt("port", lang), parse_mode="HTML")
        return True

    # port -> show the public key and wait for them to install it
    if not text.isdigit() or not (1 <= int(text) <= 65535):
        await update.message.reply_text(_t(lang, "Port must be a number between 1 and 65535.",
                                              "Port harus angka antara 1 dan 65535."))
        return True
    data["port"] = int(text)
    state["step"] = "authorize"

    loop = asyncio.get_running_loop()
    try:
        _, pub = await loop.run_in_executor(None, agent_keypair)
        pubkey = pub.read_text().strip()
        managed = {pub.stem, "agent_active", SSH_RO_KEY.stem, SSH_RW_KEY.stem}
        others = sorted(
            k.stem for k in (Path.home() / ".ssh").glob("*.pub")
            if k.stem not in managed
        )
    except Exception as exc:
        logger.exception("could not prepare the agent keypair")
        _server_wizard.pop(chat_id, None)
        await update.message.reply_text(_t(lang,
            f"⚠️ Couldn't prepare an SSH key: {exc}",
            f"⚠️ Gagal siapkan SSH key: {exc}",
        ))
        return True

    # The operator authorises the WRITE key by hand; the bot then installs its
    # own restricted key behind the guard and proves the guard refuses writes.
    # Handing over the read-only key here instead would look safer and be the
    # opposite -- an unguarded key whose NAME says read-only.
    if _keys_configured():
        pubkey = SSH_RW_KEY.with_suffix(".pub").read_text().strip()
    await update.message.reply_text(
        _t(lang,
           f"🔑 <b>Authorise the agent on {_tg_escape(data['host'])}</b>\n\n"
           "Run this <b>on that machine</b>, as "
           f"<code>{_tg_escape(data['user'])}</code>:\n\n"
           f"<pre>mkdir -p ~/.ssh &amp;&amp; echo '{_tg_escape(pubkey)}' &gt;&gt; "
           "~/.ssh/authorized_keys &amp;&amp; chmod 600 ~/.ssh/authorized_keys</pre>\n\n"
           "<i>That's the PUBLIC half. I generated the pair here and the private half "
           "never leaves this box — which is why I don't ask you to paste a key into "
           "chat.</i>\n\nThen tap Test.",
           f"🔑 <b>Otorisasi agent di {_tg_escape(data['host'])}</b>\n\n"
           "Jalankan ini <b>di mesin itu</b>, sebagai "
           f"<code>{_tg_escape(data['user'])}</code>:\n\n"
           f"<pre>mkdir -p ~/.ssh &amp;&amp; echo '{_tg_escape(pubkey)}' &gt;&gt; "
           "~/.ssh/authorized_keys &amp;&amp; chmod 600 ~/.ssh/authorized_keys</pre>\n\n"
           "<i>Itu bagian PUBLIC-nya. Pasangannya dibuat di sini dan bagian privat "
           "tidak pernah keluar dari box ini — makanya saya tidak minta kamu tempel "
           "key di chat.</i>\n\nLalu tap Test.",
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(_t(lang, "🔌 Test connection", "🔌 Tes koneksi"), callback_data="srv:test:"),
            InlineKeyboardButton(_t(lang, "✖️ Cancel", "✖️ Batal"), callback_data="srv:cancel:"),
        ]]),
    )
    if others:
        # Already have a key that reaches this machine? Say so instead of
        # installing a second one. The key itself never moves through chat --
        # you pick a name, and the file stays on this host.
        await update.message.reply_text(
            _t(lang,
               "🔑 <i>Already have a key that reaches it?</i> These are on this host:\n",
               "🔑 <i>Sudah punya key yang bisa menjangkaunya?</i> Ini yang ada di host ini:\n",
            )
            + "\n".join(f"  • <code>{_tg_escape(o)}</code>" for o in others[:10])
            + _t(lang,
                 "\n\nSend <code>usekey &lt;name&gt;</code> to use one instead.\n\n"
                 "⚠️ <i>A host pinned to its own key sits outside read-only mode — "
                 "the agent could change it at any time, without /unlock. Prefer the "
                 "key above unless this machine genuinely needs its own.</i>",
                 "\n\nKirim <code>usekey &lt;nama&gt;</code> untuk pakai yang itu.\n\n"
                 "⚠️ <i>Host yang dipatok ke key-nya sendiri berada di luar read-only mode — "
                 "agent bisa mengubahnya kapan saja, tanpa /unlock. Utamakan key di atas "
                 "kecuali mesin ini memang perlu key sendiri.</i>",
            ),
            parse_mode="HTML")
    return True


async def _finish_addserver(update: Update, query, discovery: str = "") -> None:
    chat_id = update.effective_chat.id
    state = _server_wizard.pop(chat_id, None)
    if not state:
        return
    data = state["data"]
    items = [s for s in _read_servers() if s["name"] != data["name"]]
    items.append({
        **data,
        "added_by": update.effective_user.id,
        "added_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    lang = _chat_lang(update)
    try:
        _rebuild_ssh_config(items)
    except Exception as exc:
        logger.exception("ssh config update failed")
        await query.edit_message_text(_t(lang,
            f"⚠️ Saved nothing — couldn't update ~/.ssh/config: {exc}",
            f"⚠️ Tidak ada yang disimpan — gagal update ~/.ssh/config: {exc}",
        ))
        return
    _write_servers(items)
    logger.warning("SERVER ADDED name=%s host=%s by=%s",
                   data["name"], data["host"], update.effective_user.id)

    # Record it where the agent will actually read it next conversation.
    fact = (f"Server '{data['name']}' ({SERVER_KINDS.get(data.get('kind'), '?')}"
            f"{'/' + data['flavour'] if data.get('flavour') else ''}) is reachable at "
            f"{data['host']} as {data['user']} on port {data['port']}.")
    append_learned([fact])
    if data.get("cluster_hosts"):
        append_learned([f"Cluster nodes alongside {data['name']}: "
                        f"{', '.join(data['cluster_hosts'][:20])}."])

    msg = (_t(lang, f"✅ <b>{_tg_escape(data['name'])} added.</b>\n\n",
                    f"✅ <b>{_tg_escape(data['name'])} ditambahkan.</b>\n\n")
           + f"<code>{_tg_escape(data['user'])}@{_tg_escape(data['host'])}"
           f":{data['port']}</code>\n")
    if discovery:
        msg += f"\n<pre>{_tg_escape(discovery)}</pre>\n"
    msg += _t(lang,
        "\nIt's in <code>~/.ssh/config</code> and recorded in the agent's brief, "
        "so it can reach it from the next message onward.\n\n/servers to review.",
        "\nSudah ada di <code>~/.ssh/config</code> dan tercatat di brief agent, "
        "jadi bisa dijangkau mulai pesan berikutnya.\n\n/servers untuk melihat lagi.",
    )
    await query.edit_message_text(msg, parse_mode="HTML")


async def cmd_servers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everything the agent has been told it may reach. Zero tokens."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    items = _read_servers()
    if not items:
        return await update.message.reply_text(_t(lang,
            "🖥 No servers registered.\n\nAdd one with /addserver.",
            "🖥 Belum ada server terdaftar.\n\nTambahkan lewat /addserver.",
        ))
    lines = [_t(lang, f"🖥 <b>Registered servers ({len(items)})</b>", f"🖥 <b>Server terdaftar ({len(items)})</b>"), ""]
    for it in items:
        kind = SERVER_KINDS.get(it.get("kind"), "?")
        if it.get("flavour") and it["flavour"] != it.get("kind"):
            kind += f" / {HYPERVISOR_FLAVOURS.get(it['flavour'], it['flavour'])}"
        lines.append(
            f"• <b>{_tg_escape(it['name'])}</b> — {_tg_escape(kind)}\n"
            f"  <code>{_tg_escape(it['user'])}@{_tg_escape(it['host'])}:{it.get('port', 22)}</code>"
            + (_t(lang, f"\n  cluster: {len(it['cluster_hosts']) + 1} nodes",
                       f"\n  cluster: {len(it['cluster_hosts']) + 1} node") if it.get("cluster_hosts") else "")
            + _t(lang, f"\n  added {it.get('added_at', '?')}", f"\n  ditambahkan {it.get('added_at', '?')}")
        )
    lines.append(_t(lang, "\nRemove one: /removeserver &lt;name&gt;", "\nHapus satu: /removeserver &lt;nama&gt;"))
    await _reply_chunked(update, "\n".join(lines), already_html=True)


async def cmd_removeserver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _may_run_setup(update):
        return
    lang = _chat_lang(update)
    if not _is_owner(update) and not await _is_group_admin(update, context):
        await update.message.reply_text(_t(lang, "🔒 Bot owner or a group admin only.", "🔒 Cuma pemilik bot atau admin grup."))
        return
    name = (context.args[0] if context.args else "").strip()
    if not name:
        return await update.message.reply_text(_t(lang,
            "Usage: /removeserver <name>\nSee names with /servers",
            "Pakai: /removeserver <nama>\nLihat nama di /servers",
        ))
    items = _read_servers()
    kept = [s for s in items if s["name"] != name]
    if len(kept) == len(items):
        return await update.message.reply_text(_t(lang,
            f"No server named '{name}'. See /servers",
            f"Tidak ada server bernama '{name}'. Lihat /servers",
        ))
    _rebuild_ssh_config(kept)
    _write_servers(kept)
    logger.warning("SERVER REMOVED name=%s by=%s", name, update.effective_user.id)
    await update.message.reply_text(_t(lang,
        f"🗑 Removed <b>{_tg_escape(name)}</b> from ~/.ssh/config.\n\n"
        "<i>The agent may still mention it from what it learned earlier — "
        "check /learned and /forget if that matters.</i>",
        f"🗑 Dihapus <b>{_tg_escape(name)}</b> dari ~/.ssh/config.\n\n"
        "<i>Agent mungkin masih menyebutnya dari yang sudah dipelajari sebelumnya — "
        "cek /learned dan /forget kalau itu penting.</i>",
    ), parse_mode="HTML")


async def cmd_boundaries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the hard boundaries. Readable by anyone the bot answers -- knowing
    what is off-limits is not sensitive, and a boundary nobody can see is one
    nobody can check."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    items = read_boundaries()
    if not items:
        return await update.message.reply_text(_t(lang,
            "🚧 <b>No hard boundaries set.</b>\n\n"
            "A hard boundary is something the agent must <b>never</b> do on its own, "
            "however it is asked. It has real shell and SSH access, so this is the short "
            "list of things where a misunderstanding would become an incident.\n\n"
            "Whatever you write is copied into the agent's brief word for word, and the "
            "agent can never edit or remove it.\n\n"
            "For example:\n"
            '  <code>/addboundary The VM "prod-db" -- never stop or restart it</code>\n'
            "  <code>/addboundary Never delete or resize a disk without asking</code>\n\n"
            "<i>Nothing is off-limits until you add one.</i>",

            "🚧 <b>Belum ada hard boundary.</b>\n\n"
            "Hard boundary itu hal yang <b>tidak boleh</b> dilakukan agent atas inisiatif "
            "sendiri, bagaimanapun cara memintanya. Agent ini punya akses shell dan SSH "
            "sungguhan, jadi ini daftar pendek hal-hal yang kalau salah paham bisa jadi "
            "insiden.\n\n"
            "Apa pun yang Anda tulis disalin apa adanya ke brief agent, dan agent tidak "
            "akan pernah bisa mengubah atau menghapusnya.\n\n"
            "Contoh:\n"
            '  <code>/addboundary VM "prod-db" -- jangan pernah dimatikan atau direstart</code>\n'
            "  <code>/addboundary Jangan hapus atau resize disk tanpa tanya dulu</code>\n\n"
            "<i>Selama belum ada yang ditambahkan, tidak ada yang dianggap terlarang.</i>",
        ), parse_mode="HTML")
    lines = [_t(lang, f"🚧 <b>Hard boundaries ({len(items)})</b>", f"🚧 <b>Hard boundary ({len(items)})</b>"), ""]
    lines += [f"{i}. {_tg_escape(x)}" for i, x in enumerate(items, 1)]
    lines.append(_t(lang,
        "\n<i>Add: /addboundary &lt;rule&gt;   Remove: /rmboundary &lt;number&gt;</i>",
        "\n<i>Tambah: /addboundary &lt;aturan&gt;   Hapus: /rmboundary &lt;nomor&gt;</i>",
    ))
    await _reply_chunked(update, "\n".join(lines), already_html=True)


async def cmd_addboundary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _may_run_setup(update):
        return
    lang = _chat_lang(update)
    if not _is_owner(update) and not await _is_group_admin(update, context):
        return await update.message.reply_text(_t(lang, "🔒 Bot owner or a group admin only.", "🔒 Cuma pemilik bot atau admin grup."))
    rule = " ".join(context.args).strip() if context.args else ""
    if len(rule) < 8:
        return await update.message.reply_text(_t(lang,
            "\U0001f6a7 What is a hard boundary?\n\n"
            "A line the agent must never cross on its own -- however the request is "
            "worded, and even if someone asks it to. It has real shell and SSH access, "
            "so this is your short list of things where a misunderstanding would become "
            "an incident.\n\n"
            "Your words are copied into the agent's brief verbatim, and the agent can "
            "never edit or remove them. Only you can, from here.\n\n"
            "Usage:  /addboundary <rule>\n\n"
            "Good examples -- name the exact thing:\n"
            '  /addboundary The VM "prod-db" -- never stop, restart or change it\n'
            "  /addboundary Never restart any Elasticsearch node, for any reason\n"
            "  /addboundary Never delete or resize a disk without asking first\n"
            "  /addboundary Never change firewall rules on the gateway\n\n"
            "Avoid vague ones like \"be careful with production\" -- the agent then has "
            "to interpret what you meant, and removing that interpretation is the whole "
            "point of a boundary.\n\n"
            "/boundaries shows what is already set. /rmboundary <n> removes one (PIN).",

            "\U0001f6a7 Apa itu hard boundary?\n\n"
            "Garis yang tidak boleh dilewati agent atas inisiatif sendiri -- bagaimanapun "
            "permintaannya ditulis, bahkan kalau ada yang menyuruhnya. Agent ini punya "
            "akses shell dan SSH sungguhan, jadi ini daftar pendek hal-hal yang kalau "
            "salah paham bisa jadi insiden.\n\n"
            "Kalimat Anda disalin apa adanya ke brief agent, dan agent tidak akan pernah "
            "bisa mengubah atau menghapusnya. Hanya Anda, dari sini.\n\n"
            "Pakai:  /addboundary <aturan>\n\n"
            "Contoh yang baik -- sebut bendanya dengan jelas:\n"
            '  /addboundary VM "prod-db" -- jangan pernah dimatikan, direstart, atau diubah\n'
            "  /addboundary Jangan pernah restart node Elasticsearch, apa pun alasannya\n"
            "  /addboundary Jangan hapus atau resize disk tanpa tanya dulu\n"
            "  /addboundary Jangan ubah aturan firewall di gateway\n\n"
            "Hindari yang samar seperti \"hati-hati sama production\" -- agent jadi harus "
            "menebak maksud Anda, padahal menghilangkan tebakan itulah gunanya boundary.\n\n"
            "/boundaries untuk lihat yang sudah ada. /rmboundary <n> untuk hapus (pakai PIN).",
        ))
    items = read_boundaries()
    if rule in items:
        return await update.message.reply_text(_t(lang, "That boundary is already recorded.", "Boundary itu sudah tercatat."))
    items.append(rule)
    write_boundaries(items)
    logger.warning("BOUNDARY ADDED by=%s: %s", update.effective_user.id, rule)
    await update.message.reply_text(_t(lang,
        f"🚧 Added. {len(items)} boundar{'y' if len(items) == 1 else 'ies'} now in force.\n\n"
        "<i>It takes effect on the next new conversation — existing ones already "
        "have the older list. Use /new to apply it right away.</i>",
        f"🚧 Ditambahkan. {len(items)} boundary sekarang berlaku.\n\n"
        "<i>Efeknya mulai di percakapan baru berikutnya — yang sedang jalan masih pakai "
        "daftar lama. Pakai /new untuk langsung terapkan.</i>",
    ), parse_mode="HTML")


async def cmd_rmboundary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removing a boundary needs the PIN. Adding one only ever restricts the
    agent; removing one hands capability back, which is the direction worth
    slowing down."""
    if not _may_run_setup(update):
        return
    lang = _chat_lang(update)
    if not _is_owner(update) and not await _is_group_admin(update, context):
        return await update.message.reply_text(_t(lang, "🔒 Bot owner or a group admin only.", "🔒 Cuma pemilik bot atau admin grup."))
    arg = (context.args[0] if context.args else "").strip()
    items = read_boundaries()
    if not arg.isdigit() or not (1 <= int(arg) <= len(items)):
        return await update.message.reply_text(_t(lang,
            f"Usage: /rmboundary <number>\nSee /boundaries ({len(items)} recorded).",
            f"Pakai: /rmboundary <nomor>\nLihat di /boundaries ({len(items)} tercatat).",
        ))
    target = items[int(arg) - 1]
    if pin_is_set(update.effective_chat.id):
        await request_pin(update, "rmboundary", {"rule": target}, _t(lang,
            f"🚧 Removing a boundary:\n\n<i>{_tg_escape(target)}</i>",
            f"🚧 Menghapus boundary:\n\n<i>{_tg_escape(target)}</i>",
        ))
    else:
        write_boundaries([x for x in items if x != target])
        await update.message.reply_text(_t(lang, f"🚧 Removed: {target}", f"🚧 Dihapus: {target}"))


async def cmd_snapshots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Snapshots the agent took before changing something. Zero tokens."""
    if not _authorized(update):
        return
    lang = _chat_lang(update)
    items = read_snapshots()
    if not items:
        return await update.message.reply_text(_t(lang,
            "📸 No snapshots recorded yet.\n\n"
            "<i>The agent is told to snapshot a VM before changing it, and each one "
            "it takes shows up here so it can be cleaned up later.</i>",
            "📸 Belum ada snapshot tercatat.\n\n"
            "<i>Agent diinstruksikan snapshot VM sebelum mengubahnya, dan tiap yang "
            "diambil muncul di sini biar bisa dibersihkan nanti.</i>",
        ), parse_mode="HTML")
    lines = [_t(lang, f"📸 <b>Snapshots taken before changes ({len(items)})</b>",
                     f"📸 <b>Snapshot yang diambil sebelum perubahan ({len(items)})</b>"), ""]
    for i, s in enumerate(items[-25:], 1):
        lines.append(
            f"{i}. VM <b>{_tg_escape(str(s.get('vmid')))}</b> on "
            f"{_tg_escape(str(s.get('node')))} — <code>{_tg_escape(s.get('snapname', '?'))}</code>\n"
            f"   {s.get('at', '?')}" + (f" — {_tg_escape(s['reason'])}" if s.get("reason") else ""))
    lines.append(_t(lang,
        "\n<i>Delete one on the cluster with: qm delsnapshot &lt;vmid&gt; &lt;name&gt;</i>\n"
        "<i>Snapshots hold disk space, so they are worth clearing once a change has proven good.</i>",
        "\n<i>Hapus satu di cluster dengan: qm delsnapshot &lt;vmid&gt; &lt;nama&gt;</i>\n"
        "<i>Snapshot makan ruang disk, jadi layak dibersihkan setelah perubahan terbukti aman.</i>",
    ))
    await _reply_chunked(update, "\n".join(lines), already_html=True)


async def offer_unlock(update: Update, reason: str, original_prompt: str,
                       vmid: Optional[str]) -> None:
    """Shown when the agent was refused by the read-only guard."""
    chat_id = update.effective_chat.id
    _pending_write[chat_id] = {
        "prompt": original_prompt, "reason": reason, "vmid": vmid,
        "expires": _dt.datetime.now().timestamp() + PENDING_WRITE_TTL,
    }
    lang = _chat_lang(update)
    offer_snapshot = vmid and needs_snapshot_offer(reason)
    rows = []
    if offer_snapshot:
        rows.append([InlineKeyboardButton(
            _t(lang, f"📸 Snapshot VM {vmid}, then unlock", f"📸 Snapshot VM {vmid}, lalu unlock"),
            callback_data="nw:snap")])
        rows.append([InlineKeyboardButton(
            _t(lang, "🔓 Unlock without a snapshot", "🔓 Unlock tanpa snapshot"), callback_data="nw:nosnap")])
    else:
        rows.append([InlineKeyboardButton(_t(lang, "🔓 Unlock", "🔓 Unlock"), callback_data="nw:nosnap")])
    rows.append([InlineKeyboardButton(_t(lang, "✖️ Leave it locked", "✖️ Biarkan terkunci"), callback_data="nw:cancel")])

    note = _t(lang,
        "\n\n<i>Afterwards I'll tell the agent to continue — it keeps the same "
        "conversation, so it doesn't re-investigate, just carries on. That is a "
        "second turn, but a short one.</i>",
        "\n\n<i>Setelah ini saya suruh agent lanjut — masih di percakapan yang sama, "
        "jadi tidak menyelidiki ulang, cuma lanjut. Itu turn kedua, tapi singkat.</i>",
    )
    if vmid and not offer_snapshot:
        note = _t(lang,
            "\n\n<i>No snapshot offer for a plain power operation -- it wouldn't "
            "protect anything a reboot could touch.</i>",
            "\n\n<i>Tidak ada tawaran snapshot untuk operasi power biasa -- tidak ada "
            "yang perlu dilindungi dari sekadar reboot.</i>",
        ) + note
    elif not vmid:
        note = _t(lang,
            "\n\n<i>I couldn't tell which VM this is about, so there's no snapshot "
            "offer. Snapshot it yourself first if it matters.</i>",
            "\n\n<i>Saya tidak bisa tahu VM mana yang dimaksud, jadi tidak ada tawaran "
            "snapshot. Snapshot sendiri dulu kalau itu penting.</i>",
        ) + note
    await update.message.reply_text(
        _t(lang,
           f"🔒 <b>The agent needs write access.</b>\n\n"
           f"It wants to: <b>{_tg_escape(reason[:300])}</b>\n\n"
           f"Right now its credential physically can't change anything.{note}",
           f"🔒 <b>Agent butuh akses tulis.</b>\n\n"
           f"Ingin: <b>{_tg_escape(reason[:300])}</b>\n\n"
           f"Kredensialnya secara fisik belum bisa mengubah apa pun sekarang.{note}",
        ),
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def cmd_needwrite_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    choice = query.data.split(":", 1)[1]
    chat_id = update.effective_chat.id
    lang = _chat_lang(update)
    if not await _may_authorize_group_action(update, context):
        await query.answer(_t(lang, "Bot owner, or a registered group's own admin.",
                                   "Pemilik bot, atau admin dari grup yang sudah terdaftar."), show_alert=True)
        return
    pending = _pending_write.get(chat_id)
    if not pending or pending["expires"] < _dt.datetime.now().timestamp():
        _pending_write.pop(chat_id, None)
        await query.answer()
        await query.edit_message_text(_t(lang, "That request expired. Just ask again.",
                                               "Permintaan itu sudah kedaluwarsa. Minta lagi saja."))
        return
    await query.answer()

    if choice == "cancel":
        _pending_write.pop(chat_id, None)
        await query.edit_message_text(_t(lang, "🔒 Left locked. Nothing was changed.",
                                               "🔒 Tetap terkunci. Tidak ada yang diubah."))
        return

    pending["snapshot"] = (choice == "snap")
    await query.edit_message_text(_t(lang,
        "📸 Snapshot then unlock — confirm with your PIN." if pending["snapshot"]
        else "🔓 Unlock — confirm with your PIN.",
        "📸 Snapshot lalu unlock — konfirmasi dengan PIN." if pending["snapshot"]
        else "🔓 Unlock — konfirmasi dengan PIN.",
    ))
    await request_pin(update, "unlock_and_resume",
                      {"minutes": WRITE_MODE_DEFAULT_MINUTES},
                      _t(lang, "🔓 Confirm opening write mode.", "🔓 Konfirmasi buka write mode."))


async def _do_unlock_and_resume(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                query) -> None:
    """PIN cleared: snapshot if asked, open write mode, then re-run the request."""
    chat_id = update.effective_chat.id
    lang = _chat_lang(update)
    pending = _pending_write.pop(chat_id, None)
    if not pending:
        await query.edit_message_text(_t(lang, "That request expired. Just ask again.",
                                               "Permintaan itu sudah kedaluwarsa. Minta lagi saja."))
        return
    loop = asyncio.get_running_loop()

    if pending.get("snapshot") and pending.get("vmid"):
        vmid = pending["vmid"]
        await query.edit_message_text(_t(lang, f"📸 Snapshotting VM {vmid}…", f"📸 Snapshot VM {vmid}…"))
        node = await loop.run_in_executor(None, find_vm_node, vmid)
        ok, detail = await loop.run_in_executor(
            None, take_snapshot, vmid, node, pending["reason"])
        if not ok:
            # A failed snapshot is a reason to stop, not a detail to note in
            # passing: proceeding would be making the change without the
            # rollback point that was explicitly asked for.
            await query.edit_message_text(_t(lang,
                f"❌ Snapshot failed, so I've left write mode <b>closed</b>:\n"
                f"<pre>{_tg_escape(detail)}</pre>\n\n"
                "Storage full, or too many snapshots already? Worth checking before "
                "changing anything. Ask again to retry, or use /unlock to proceed "
                "without one.",
                f"❌ Snapshot gagal, jadi write mode saya biarkan <b>tertutup</b>:\n"
                f"<pre>{_tg_escape(detail)}</pre>\n\n"
                "Storage penuh, atau sudah kebanyakan snapshot? Layak dicek dulu "
                "sebelum mengubah apa pun. Minta lagi untuk coba ulang, atau pakai "
                "/unlock untuk lanjut tanpa snapshot.",
            ), parse_mode="HTML")
            return
        await query.edit_message_text(_t(lang,
            f"📸 Snapshot <code>{_tg_escape(detail)}</code> taken.",
            f"📸 Snapshot <code>{_tg_escape(detail)}</code> selesai.",
        ), parse_mode="HTML")

    try:
        until = unlock_write_mode(WRITE_MODE_DEFAULT_MINUTES, max_minutes=_effective_unlock_cap(update))
    except OSError as exc:
        logger.exception("unlock failed")
        await query.message.reply_text(_t(lang, f"⚠️ Couldn't unlock: {exc}", f"⚠️ Gagal buka: {exc}"))
        return
    left = int((until - _dt.datetime.now().timestamp()) / 60) + 1

    # Continue the SAME conversation rather than re-asking. The agent already
    # holds the whole investigation from the blocked turn, so it needs a nudge,
    # not the question again -- which would cost the prompt twice and invite it
    # to repeat an explanation it has already given.
    reason = (pending.get("reason") or "").strip()
    if reason and reason != "make a change it was blocked from making":
        follow_up = f"Write access is open now. Go ahead with: {reason}"
    else:
        # No NEEDS_WRITE line to quote, so fall back to the original request.
        follow_up = pending["prompt"]

    await query.message.reply_text(_t(lang,
        f"🔓 Write mode open for {left} min. Telling the agent to continue…",
        f"🔓 Write mode terbuka {left} menit. Menyuruh agent lanjut…",
    ))
    await _run_turn(update, context, follow_up)


# --------------------------------------------------------------------------
# /agentstatus -- a real liveness probe, not a guess from login state
#
# /providers already shows the configured chain and each tier's cooldown state,
# but that is PASSIVE -- it only knows a tier is down if a real user turn
# recently failed against it. A tier that has not been used since the last
# outage would still show "ready" even if it is down right now.
#
# So this sends an actual tiny probe to every tier, in parallel, and reports
# what came back. Deliberately minimal: no environment brief, no memory, no
# conversation history -- just enough to prove the credential and the endpoint
# both work. Both backends are fixed-price subscriptions, so this costs a
# sliver of quota, not money, and updates the SAME cooldown table /providers
# reads, so a real failure found here also protects the next real turn from
# wasting a full attempt on a tier that just proved to be down.
# --------------------------------------------------------------------------

AGENTSTATUS_PROBE_PROMPT = "Reply with exactly one word: OK"
AGENTSTATUS_PROBE_TIMEOUT = 30          # subprocess hard timeout, per tier
AGENTSTATUS_PROBE_PRINT_TIMEOUT = "20s"  # agy's own --print-timeout
AGENTSTATUS_CACHE_SECONDS = 20          # guards against an accidental double-tap
_agentstatus_cache: dict = {"at": 0.0, "results": []}


async def _probe_one_tier(tier: dict) -> dict:
    provider, model, label = tier["provider"], tier["model"], tier["label"]
    loop = asyncio.get_running_loop()
    start = _dt.datetime.now().timestamp()
    try:
        if provider == "agy":
            await loop.run_in_executor(
                None, _run_agy_once, AGENTSTATUS_PROBE_PROMPT, model, None,
                AGENTSTATUS_PROBE_TIMEOUT, AGENTSTATUS_PROBE_PRINT_TIMEOUT,
            )
        else:
            await loop.run_in_executor(
                None, run_claude, AGENTSTATUS_PROBE_PROMPT, None,
                "agentstatus-probe", model, AGENTSTATUS_PROBE_TIMEOUT,
            )
        elapsed = _dt.datetime.now().timestamp() - start
        _note_tier_success(model)
        return {"provider": provider, "model": model, "label": label,
                "ok": True, "elapsed": elapsed}
    except Exception as exc:
        elapsed = _dt.datetime.now().timestamp() - start
        kind = _classify_failure(exc)
        _note_tier_failure(model)
        return {"provider": provider, "model": model, "label": label, "ok": False,
                "elapsed": elapsed, "error": str(exc)[:220], "kind": kind}


async def check_all_tiers() -> list[dict]:
    return list(await asyncio.gather(*(_probe_one_tier(t) for t in TIERS)))


def _format_agentstatus(results: list[dict], cached_age: Optional[float], lang: str) -> str:
    up = sum(1 for r in results if r["ok"])
    lines = [f"🩺 <b>{_t(lang, 'Agent status', 'Status agent')}</b> — {up}/{len(results)} online", ""]
    for r in results:
        if r["ok"]:
            lines.append(
                f"🟢 <b>{_tg_escape(r['label'])}</b> — online "
                f"({r['elapsed']:.1f}s)\n"
                f"   <code>{_tg_escape(r['provider'])}</code> / <code>{_tg_escape(r['model'])}</code>"
            )
        else:
            lines.append(
                f"🔴 <b>{_tg_escape(r['label'])}</b> — DOWN / ERROR\n"
                f"   <code>{_tg_escape(r['provider'])}</code> / <code>{_tg_escape(r['model'])}</code>\n"
                f"   <i>{_tg_escape(r['error'])}</i>"
            )
    if cached_age is not None:
        lines.append(_t(lang,
            f"\n<i>Cached, checked {int(cached_age)}s ago. /agentstatus force for a fresh check.</i>",
            f"\n<i>Dari cache, dicek {int(cached_age)} detik lalu. /agentstatus force untuk cek ulang.</i>",
        ))
    else:
        lines.append(_t(lang,
            "\n<i>Each check is a tiny real request to every tier -- a sliver of "
            "quota, not a wasted turn. Results also feed /providers' cooldown state.</i>",
            "\n<i>Tiap cek itu request nyata (kecil) ke tiap tier -- sedikit kuota, "
            "bukan turn yang sia-sia. Hasilnya juga masuk ke status cooldown /providers.</i>",
        ))
    return "\n".join(lines)


async def cmd_agentstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Real liveness check of every configured tier, in parallel."""
    if not _authorized(update):
        return
    force = bool(context.args) and context.args[0].lower() in ("force", "refresh")
    now = _dt.datetime.now().timestamp()
    cache_age = now - _agentstatus_cache["at"]
    lang = _chat_lang(update)
    if not force and _agentstatus_cache["results"] and cache_age < AGENTSTATUS_CACHE_SECONDS:
        await _reply_chunked(update, _format_agentstatus(_agentstatus_cache["results"], cache_age, lang),
                             already_html=True)
        return

    msg = await update.message.reply_text(_t(lang, f"🩺 Checking {len(TIERS)} tier(s)…", f"🩺 Cek {len(TIERS)} tier…"))
    try:
        results = await asyncio.wait_for(check_all_tiers(), timeout=AGENTSTATUS_PROBE_TIMEOUT + 15)
    except asyncio.TimeoutError:
        await msg.edit_text(_t(lang,
            "⚠️ The check itself timed out. Try /agentstatus again.",
            "⚠️ Pengecekannya sendiri timeout. Coba /agentstatus lagi.",
        ))
        return
    _agentstatus_cache["at"] = now
    _agentstatus_cache["results"] = results

    text = _format_agentstatus(results, None, lang)
    try:
        await msg.delete()
    except Exception:
        pass
    await _reply_chunked(update, text, already_html=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    chat = update.effective_chat
    mention_span = None
    if chat and chat.type != "private":
        # See the "group wake word" note above _group_mention_span -- a group
        # message that neither replies to the bot nor @-mentions it is
        # ordinary chatter and must never reach the wizard/server-input
        # capture below either, not just the model: with Privacy Mode off,
        # EVERY group message arrives here now, and without this gate an
        # unrelated message posted while, say, an OAuth code capture is
        # pending would get treated as that code.
        mention_span = _group_mention_span(update, context)
        if mention_span is None and not _is_reply_to_bot(update, context):
            return
    if await _handle_wizard_input(update, context):
        return
    if await _handle_server_input(update, context):
        return
    text = update.message.text or ""
    if mention_span is not None:
        # Drop the "@botname" itself so the model sees a clean question
        # ("weather today?") rather than the mention as part of the prompt.
        text = _strip_entity(text, *mention_span).strip()
    if not text.strip():
        return
    await _run_turn(update, context, text)


async def _maybe_notify_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tell the operator once when the repository moves ahead.

    Deliberately NOT a timer. It piggybacks on a real message, runs at most
    once every UPDATE_CHECK_INTERVAL_HOURS, costs no model tokens, and stays
    quiet about a version it has already mentioned. That is bounded automation
    with a clear trigger, not a background process deciding things on its own.
    """
    try:
        if not is_git_checkout():
            return
        st = _read_update_state()
        now = _dt.datetime.now().timestamp()
        if now - st.get("last_check", 0) < UPDATE_CHECK_INTERVAL_HOURS * 3600:
            return
        if not await _may_authorize_group_action(update, context):
            return
        info = await asyncio.get_running_loop().run_in_executor(None, check_for_update)
        st["last_check"] = now
        _write_update_state(st)
        if not (info["ok"] and info["has_update"]):
            return
        if st.get("seen_version") == info["latest"]:
            return  # already mentioned this one; do not nag
        st["seen_version"] = info["latest"]
        _write_update_state(st)
        lang = _chat_lang(update)
        card, kb = _update_card(lang, info)
        await _msg(update).reply_text(card, parse_mode="HTML", reply_markup=kb)
    except Exception:
        # An update check must never be the reason a normal turn fails.
        logger.warning("update check failed", exc_info=True)


async def _run_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """One agent turn, serialised per chat and capped process-wide.

    The work itself is in _run_turn_inner; this wrapper exists only to hold
    the two guards while it runs -- see _chat_turn_lock (ordering within a
    chat, which is a correctness requirement) and _turn_semaphore (a ceiling
    on how many real subprocesses can exist at once).
    """
    async with _chat_turn_lock(str(update.effective_chat.id)):
        async with _turn_semaphore():
            await _run_turn_inner(update, context, text)


async def _run_turn_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """One agent turn, start to finish.

    Separate from handle_message because a turn can also be started by the
    resume-after-unlock flow, which has no incoming message of its own.
    """
    chat_id = str(update.effective_chat.id)

    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    active = state["active"]
    sess = state["sessions"][active]  # {"claude": {model: id}, "agy": {model: id}}, mutated by run_combo

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    forced_model = _read_model_overrides().get(chat_id)
    forced_tier = next((t for t in ALL_TIERS if t["model"] == forced_model), None) if forced_model else None
    # Owner's extra scope only in their OWN private chat -- never a group,
    # even one the owner is speaking in. See owner_scope_text()/run_combo.
    owner_dm = _is_owner(update) and update.effective_chat.type == "private"
    trace: list[dict] = []          # filled per tier attempt, written to the ledger below
    started = _dt.datetime.now()

    lang = _chat_lang(update)
    try:
        # run_combo shells out to agy/claude and blocks for minutes at a time
        # -- one live turn was measured at 4m35s. Called directly, as it was
        # until now, that blocks the whole asyncio loop: the bot could not
        # answer /status, accept /cancel, or serve any OTHER chat until it
        # finished. Every other blocking call in this file already goes
        # through an executor; the longest one in it did not.
        result, model, attempts = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(run_combo, text, sess, active,
                                    forced_tier=forced_tier, owner_dm=owner_dm,
                                    trace=trace, chat_id=chat_id)
        )
    except Exception as exc:
        logger.exception("combo run failed")
        await update.message.reply_text(_t(lang, f"⚠️ Error: {exc}", f"⚠️ Error: {exc}"))
        return

    # Gemini's own session can die silently between turns (see
    # _agy_attempt_needs_reauth) -- the chain already failed over to Claude for
    # THIS reply, so say something rather than let every future turn quietly
    # cost more until someone happens to notice on their own.
    if _agy_attempt_needs_reauth(attempts):
        # Unconditional, NOT gated by the notice cooldown below: a live
        # failure just proved the earlier success no longer holds, so /start's
        # card should stop claiming otherwise right away, not up to an hour
        # from now -- agy_signed_in() falls back to this exact flag once the
        # filesystem check finds nothing (its own docstring says so), which is
        # exactly how it kept showing a green check through the whole outage
        # that led to this feature existing.
        _unmark_setup("agy")
        global _agy_reauth_last_notice
        now = _dt.datetime.now().timestamp()
        if now - _agy_reauth_last_notice > AGY_REAUTH_NOTICE_COOLDOWN_HOURS * 3600:
            _agy_reauth_last_notice = now
            target = _msg(update)
            if target is not None:
                try:
                    await target.reply_text(_t(lang,
                        "⚠️ <b>Gemini (Antigravity) is signed out.</b>\n\n"
                        "Replies are still coming through -- just from the Claude "
                        "fallback instead, which spends more of this deployment's "
                        "shared quota than the usual tiers.\n\n"
                        "Fix: send /start and tap “Change Gemini (Antigravity)” "
                        "to sign in again.",
                        "⚠️ <b>Gemini (Antigravity) sudah logout.</b>\n\n"
                        "Balasan tetap jalan -- cuma lewat fallback Claude, yang lebih "
                        "boros kuota bersama deployment ini dibanding tier biasa.\n\n"
                        "Perbaikan: kirim /start lalu tap “Ganti Gemini "
                        "(Antigravity)” untuk sign-in ulang.",
                    ), parse_mode="HTML")
                except Exception:
                    logger.warning("could not deliver the Gemini re-auth notice", exc_info=True)

    # re-load in case /session or /remember ran concurrently -- unlikely with
    # single-user polling, but avoid clobbering another chat's write.
    sessions = load_sessions()
    state = get_chat_state(sessions, chat_id)
    # Which tier actually answered is what /graduate needs to find the case's
    # history later -- each tier keeps its own, so "the last one" is the only
    # reliable pointer to where the work actually happened.
    sess["last_model"] = model
    state["sessions"][active] = sess
    save_sessions(sessions)

    label = BACKEND_LABELS.get(model, model)
    reply_text = result.get("result") or _t(lang, "(no response)", "(tidak ada respons)")
    reply_text, learned_facts = extract_learned(reply_text)
    reply_text, schedule_proposals = extract_schedules(reply_text)
    reply_text, snapshots_taken = extract_snapshots(reply_text)
    reply_text, needs_write = extract_needs_write(reply_text)
    for snap in snapshots_taken:
        register_snapshot(snap)
    clean_text, media_paths = extract_media_paths(reply_text)
    clean_text, gdrive_requests = extract_gdrive(clean_text)

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
            for req in gdrive_requests:
                await _send_to_gdrive(update, context, req)
            if schedule_proposals and await _may_authorize_group_action(update, context):
                await offer_schedules(update, schedule_proposals)
            elif schedule_proposals:
                logger.warning(
                    "ignored %d SCHEDULE: proposal(s) from an origin not allowed to manage schedules (chat=%s)",
                    len(schedule_proposals), chat_id,
                )
            if newly_learned:
                # Visible, not silent: auto-writes the user can't see are how a
                # brief quietly drifts away from what they think it says.
                bullets = "\n".join(f"• {_tg_escape(f)}" for f in newly_learned)
                await update.message.reply_text(_t(lang,
                    f"🧠 <i>Recorded to environment knowledge ({len(newly_learned)} new):</i>\n{bullets}",
                    f"🧠 <i>Dicatat ke pengetahuan lingkungan ({len(newly_learned)} baru):</i>\n{bullets}",
                ), parse_mode="HTML")
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
                    await update.message.reply_text(_t(lang,
                        "⚠️ The answer finished processing but couldn't be delivered "
                        "(connection issue reaching Telegram). Please resend the same message.",
                        "⚠️ Jawaban sudah selesai diproses tapi gagal dikirim (masalah koneksi "
                        "ke Telegram). Coba kirim ulang pesan yang sama.",
                    ))
                except Exception:
                    logger.exception("even the failure notice couldn't be delivered (chat=%s)", chat_id)

    await _maybe_notify_update(update, context)

    # Refused by the node guard? Offer to unlock (snapshotting first if the
    # operator wants) and re-run. Nothing is guessed from the wording of the
    # request -- the guard already decided; we are only reacting to it.
    if (await _may_authorize_group_action(update, context) and _keys_configured()
            and not write_mode_expires_at()
            and (needs_write
                 or blocked_by_readonly(result.get("result") or "", attempts))):
        await offer_unlock(
            update,
            needs_write or "make a change it was blocked from making",
            text,
            guess_vmid(needs_write or "", text, result.get("result") or ""),
        )

    usage = result.get("usage", {})
    cost = result.get("total_cost_usd")
    logger.info(
        "turn done: chat=%s session=%s model=%s (%s) cost=$%s in=%s out=%s cache_read=%s cache_write=%s | %s",
        chat_id, active, model, label, cost,
        usage.get("input_tokens"), usage.get("output_tokens"),
        usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"),
        " -> ".join(attempts),
    )
    # The same turn, as data. Everything above is for a human reading a log;
    # this is what /spend can add up without spending a token to do it.
    answered = next((t for t in trace if t.get("outcome") == "ok"), {})
    _ledger_append({
        "chat": chat_id,
        "session": active,
        "model": model,
        "label": label,
        "provider": answered.get("provider"),
        "in": _int(answered.get("in")),
        "out": _int(answered.get("out")),
        "think": _int(answered.get("think")),
        "cache_read": _int(answered.get("cache_read")),
        "cache_write": _int(answered.get("cache_write")),
        "total": _int(answered.get("total")),
        "cost_usd": answered.get("cost_usd"),
        # Tokens burned by tiers that failed before this one answered -- the
        # number that was previously only reachable by reading log prose.
        "wasted": sum(_int(t.get("wasted")) for t in trace),
        "tiers_tried": len(trace),
        "secs": round((_dt.datetime.now() - started).total_seconds(), 1),
    })


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last line of defence.

    Without this, python-telegram-bot logs an uncaught exception and carries on,
    which from the user's side looks exactly like being ignored. Better to say
    plainly that something broke than to leave them watching a chat that never
    answers.
    """
    logger.exception("unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update):
        target = _msg(update)
        if target is not None:
            lang = _chat_lang(update)
            # Retry once, same principle as the main turn's own delivery loop:
            # a transient network blip talking to Telegram (this notice exists
            # BECAUSE of one) must not also swallow the notice itself. Found
            # live: a plain command reply (e.g. /new) failing with
            # httpcore.ReadTimeout is the single most common reason this
            # handler runs at all, so it is exactly the failure mode this
            # retry needs to survive.
            for attempt in (1, 2):
                try:
                    await target.reply_text(_t(lang,
                        "⚠️ Something went wrong handling that. It is logged -- "
                        "try again, and if it keeps happening the logs will say why.",
                        "⚠️ Ada yang salah waktu memproses itu. Sudah tercatat di log -- "
                        "coba lagi, dan kalau terus terjadi, log-nya akan menunjukkan sebabnya.",
                    ))
                    break
                except Exception:
                    if attempt == 1:
                        await asyncio.sleep(2)
                    else:
                        logger.warning("could not even deliver the error notice", exc_info=True)


def main() -> None:
    # Create the read-only/write keypair before anything can ask whether the
    # write gate is configured. Without this the gate is inert on a fresh
    # clone -- no approval button, no PIN -- and nothing says so out loud.
    if ensure_write_mode_keys():
        logger.info("write-mode keys ready (locked by default)")
    else:
        logger.error(
            "WRITE GATE INERT: could not create %s / %s. Destructive requests "
            "will NOT be gated by an approval button or PIN.", SSH_RO_KEY, SSH_RW_KEY)
    if not SYSTEM_PROMPT_FILE.exists():
        logger.error("System prompt file not found: %s", SYSTEM_PROMPT_FILE)
        sys.exit(1)
    if not GEMINI_PROMPT_FILE.exists():
        logger.warning(
            "GEMINI.md not found at %s -- agy will run with no environment "
            "context and will likely under-perform. Copy GEMINI.md.template "
            "and fill it in.", GEMINI_PROMPT_FILE,
        )

    async def _announce_update(application) -> None:
        """One shot, on startup, only if an update just restarted us."""
        if not UPDATE_ANNOUNCE_FILE.exists():
            return
        try:
            info = json.loads(UPDATE_ANNOUNCE_FILE.read_text())
        except Exception:
            logger.warning("update_announce.json unreadable", exc_info=True)
            UPDATE_ANNOUNCE_FILE.unlink(missing_ok=True)
            return
        UPDATE_ANNOUNCE_FILE.unlink(missing_ok=True)
        lang = info.get("lang", DEFAULT_LANGUAGE)
        try:
            await application.bot.send_message(
                chat_id=info["chat_id"],
                text=_t(lang,
                    f"\u2705 <b>Back up on {_tg_escape(str(info.get('to', '?')))}.</b>\n\n"
                    f"<i>Was {_tg_escape(str(info.get('from', '?')))}. Everything else -- your "
                    "settings, briefs, sessions and PIN -- is untouched.</i>",
                    f"\u2705 <b>Sudah jalan lagi di {_tg_escape(str(info.get('to', '?')))}.</b>\n\n"
                    f"<i>Sebelumnya {_tg_escape(str(info.get('from', '?')))}. Sisanya -- setting, "
                    "brief, sesi, dan PIN -- tidak tersentuh.</i>",
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("could not deliver the post-update notice", exc_info=True)

    # Without concurrent_updates the guards above are moot: python-telegram-bot
    # hands updates to handlers strictly one at a time by default, so a long
    # turn would still stall every other chat and every zero-token command.
    #
    # request: PTB's own HTTPXRequest defaults to a 5-second read/connect/write
    # timeout and a 1-second pool timeout -- checked directly against the
    # installed library, not assumed. That is tight for any link with real
    # jitter, and confirmed live: a run of `httpcore.ReadTimeout` (14 of them
    # in about an hour, every single one on a plain command reply like /new or
    # /usemodel, none on the turns that actually reached a model) surfaced as
    # "unhandled error while processing an update" -- the reply the user was
    # waiting for was already composed and simply never made it out. Raised
    # here rather than wrapping some subset of the 130+ reply_text call sites
    # individually, since the timeout applies to every outgoing Bot API call
    # this process makes, in one place.
    request = HTTPXRequest(
        connect_timeout=20.0, read_timeout=20.0, write_timeout=20.0, pool_timeout=10.0,
    )
    app = (Application.builder()
           .token(TELEGRAM_BOT_TOKEN)
           .request(request)
           .post_init(_announce_update)
           .concurrent_updates(True)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(cmd_setup_button, pattern="^setup:"))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CallbackQueryHandler(cmd_logout_button, pattern="^logout:"))
    app.add_handler(CallbackQueryHandler(cmd_start_lang_button, pattern="^startlang:"))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("secure", cmd_secure))
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
    app.add_handler(CommandHandler("setgrouppin", cmd_setgrouppin))
    app.add_handler(CommandHandler("rmgrouppin", cmd_rmgrouppin))
    app.add_handler(CallbackQueryHandler(cmd_pin_key, pattern="^pin:"))
    app.add_handler(CommandHandler("boundaries", cmd_boundaries))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CallbackQueryHandler(cmd_update_button, pattern="^upd:"))
    app.add_handler(CommandHandler("setbrief", cmd_setbrief))
    app.add_handler(CommandHandler("setscope", cmd_setscope))
    app.add_handler(CommandHandler("setownerscope", cmd_setownerscope))
    app.add_handler(CommandHandler("spend", cmd_spend))
    app.add_handler(CommandHandler("addboundary", cmd_addboundary))
    app.add_handler(CommandHandler("rmboundary", cmd_rmboundary))
    app.add_handler(CommandHandler("snapshots", cmd_snapshots))
    app.add_handler(CommandHandler("addserver", cmd_addserver))
    app.add_handler(CommandHandler("servers", cmd_servers))
    app.add_handler(CommandHandler("removeserver", cmd_removeserver))
    app.add_handler(CommandHandler("addmcp", cmd_addmcp))
    app.add_handler(CommandHandler("rmmcp", cmd_rmmcp))
    app.add_handler(CommandHandler("mcpservers", cmd_mcpservers))
    app.add_handler(CommandHandler("gdrivestatus", cmd_gdrivestatus))
    app.add_handler(CallbackQueryHandler(cmd_server_button, pattern="^srv:"))
    app.add_handler(CallbackQueryHandler(cmd_needwrite_button, pattern="^nw:"))
    app.add_handler(CommandHandler("agentstatus", cmd_agentstatus))
    app.add_handler(CommandHandler("providers", cmd_providers))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("unschedule", cmd_unschedule))
    app.add_handler(CommandHandler("adopt", cmd_adopt))
    app.add_handler(CallbackQueryHandler(cmd_schedule_decision, pattern="^sched_(ok|no):"))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("usemodel", cmd_usemodel))
    app.add_handler(CommandHandler("gdrive", cmd_gdrive))
    app.add_handler(CommandHandler("connectgdrive", cmd_connectgdrive))
    app.add_handler(CommandHandler(["lang", "language"], cmd_lang))
    app.add_handler(CallbackQueryHandler(cmd_gdrive_button, pattern="^gdrv:"))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("learned", cmd_learned))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_error_handler(on_error)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Lite Agent starting (allowed users: %s)", ALLOWED_USER_IDS or "ANY (no allowlist!)")
    # drop_pending_updates=False: a transient crash (network blip, etc.) is
    # recovered by systemd's Restart=on-failure in seconds, but with the old
    # True setting, ANY message sent during that gap was silently discarded
    # on restart -- the user got no response and no error, just silence.
    # False means a message sent during a brief outage is still processed
    # once the service comes back, instead of vanishing.
    #
    # allowed_updates: only the three types this bot actually registers a
    # handler for (see the CommandHandler/MessageHandler/CallbackQueryHandler
    # calls above) -- restricting this at the source is what actually fixes a
    # real crash, not just this one call site. Telegram's own edited_message
    # updates were being delivered too (the default when this isn't set), and
    # python-telegram-bot's CommandHandler matches those exactly like a fresh
    # message -- but update.message is None for one (the content lives in
    # update.edited_message instead). Confirmed live: a user editing an
    # already-sent "/usemodel ..." crashed cmd_usemodel with
    # AttributeError: 'NoneType' object has no attribute 'reply_text', twice
    # in the same minute. That same raw `update.message.reply_text` pattern
    # appears at over a hundred call sites in this file, so the fix belongs
    # here, at the source, rather than patched into each one by hand.
    app.run_polling(drop_pending_updates=False,
                    allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
