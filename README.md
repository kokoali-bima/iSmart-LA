# iSmart-LA (Lite Agent)

A lightweight Telegram bridge to **Claude Code** and **Antigravity CLI (agy)**, for
infrastructure monitoring and investigation -- built to be dramatically cheaper to run
than a full agent framework, while staying just as capable for real operational work.

> **Status: v0.1b.0 -- early/beta.** Built and battle-tested against a real production
> Proxmox VE cluster over several days of iteration. Works well; still has known rough
> edges (see [Known limitations](#known-limitations)).

## Why this exists

Full-featured AI agent frameworks (memory graphs, background review loops, multi-agent
orchestration) are powerful, but every one of those extra layers is also an extra place
for token spend to run away silently. In this project's own testing, one such
background-review feature burned **~900,000 tokens in a single incident** retrying a
broken tool call in a loop nobody was watching.

iSmart-LA takes the opposite approach: instead of building more agent scaffolding, it
shells out to two CLIs that Anthropic and Google already built and already do the hard
part well (tool execution, reasoning, safety self-gating via a system prompt), and adds
the smallest possible layer on top -- a Telegram relay, per-chat session bookkeeping,
and a manual (never automatic) memory file. Nothing in this codebase decides on its own
to re-run, review, or "improve" a past turn.

## Architecture

```
Telegram message
      |
      v
 lite_agent.py --- tries 4 tiers, cheapest first, falls through on failure ---
      |
      +-- 1. agy (Antigravity CLI) -- Gemini Flash        "mini"       (fixed-price, Google AI Pro/Ultra)
      +-- 2. agy (Antigravity CLI) -- Gemini Pro-low       "mini pro"   (fixed-price, Google AI Pro/Ultra)
      +-- 3. claude (Claude Code CLI) via 9Router -- Haiku "dede iku"   (fixed-price, Claude Pro/Max)
      +-- 4. claude (Claude Code CLI) via 9Router -- Sonnet "dede nnet" (fixed-price, Claude Pro/Max)
```

**Both sides are fixed-price subscriptions, not pay-per-token API billing.** Gemini runs
on a Google AI Pro/Ultra plan via agy (native, first-party CLI -- not an OAuth
credential borrowed through a third-party relay). Claude runs on a Claude Pro (or
higher) plan via 9Router's own Claude Code OAuth connection. Gemini is tried first not
to dodge per-token cost (there isn't any here), but to spread routine load across a
**separate** subscription and keep the Claude plan's own usage quota in reserve for
when it's genuinely needed -- useful because that Claude quota is often shared with a
human's own interactive Claude Code usage on the same account.

- **Claude Code** runs through **9Router** (a local multi-provider LLM gateway) as the
  fallback for when Gemini can't handle something. 9Router is a separate project this
  repo does not include -- see [9Router setup](#9router-setup) below.
- Every reply ends with a small tag (`— by mini`, `— by dede nnet`, etc.) showing which
  tier actually answered, so escalations away from the cheap default are visible at a
  glance without digging through logs.
- Each of the 4 tiers keeps its **own** conversation history. They don't share context
  with each other -- if a turn falls through from Gemini to Claude, Claude answers that
  turn cold. (Trade-off found necessary in testing: letting one tier resume a
  conversation another tier started produced a single turn costing several times more
  than a fresh conversation would have.)

## Design principles

- **No agentic/retry-capable background processes.** Every token spent is because a
  human directly asked for something, right now. Nothing runs on a timer, nothing
  reviews past turns on its own, nothing retries a failed tool call in a loop.
- **Bounded automation is fine; open-ended automation is not.** A single deterministic
  operation with a clear trigger condition (e.g. a CLI's own built-in context
  auto-compaction) is a fundamentally different risk class from an agent freely
  deciding what to retry.
- **Full context every time, not silent distillation.** SOUL.md / GEMINI.md is passed
  in full on every call. MEMORY.md is appended on top, but is only ever edited by an
  explicit `/remember` command.
- **Named sessions**, not one ever-growing thread. `/session <name>` lets a chat hold
  multiple independent, resumable conversations, so picking up yesterday's case doesn't
  drag in today's unrelated context.

## Quickstart

```bash
git clone <this-repo> lite-agent
cd lite-agent
./install.sh
```

The installer is interactive and walks through: system dependencies, Python venv,
Claude Code CLI, Antigravity CLI (agy) + its OAuth login, 9Router (use an existing
instance, or install one fresh), your Telegram bot token + admin user ID, the
environment brief, and a systemd service. Full detail: [`install.sh`](./install.sh)
is heavily commented -- read it before running if you want to know exactly what it
does first.

**Not just for Proxmox.** Nothing in the core assumes Proxmox, or infrastructure at
all. [`examples/proxmox/`](./examples/proxmox/) is one worked example, not a required
shape -- a Kubernetes cluster, a fleet of web servers, or a CI estate all work the
same way.

### The environment brief, and how it fills itself in

The one genuinely per-deployment thing is what the agent is looking after, how it
reaches it, and what it must never touch. That lives in `SOUL.md` (Claude's brief)
and `GEMINI.md` (agy's). You don't hand-author them:
[`bootstrap.py`](./bootstrap.py) asks a few plain-language questions during install
and writes both. Re-run it any time to start over.

From then on the brief maintains itself. Both files are split in two by a marker:

```
  ... persona, how to reach things, HARD BOUNDARIES ...   <- yours, humans only
  <!-- LEARNED_ZONE -->
  ... facts the agent verified for itself ...             <- appended automatically
```

When the agent works out something durable ("wkhtmltoimage is the only working
HTML-to-image tool here", "that host answers on .37, the hostname points at a
proxy"), it emits a `LEARN:` line, which is stripped from the reply and appended
below the marker -- so the next conversation starts already knowing it. `/learned`
lists what it has picked up; `/forget <n>` removes anything it got wrong.

**The model is never given write access to these files.** It can already run shell
commands, so "please don't edit above the marker" would be a request, not a boundary
-- and above the marker is exactly where the rules about what must never be touched
live. Instead the model only supplies the fact text via `LEARN:`, and the bot decides
where it lands: always inside the learned zone, never anywhere else. The boundary is
enforced by code, not by the model's cooperation. Bootstrap follows the same rule --
your hard boundaries are copied in verbatim, never paraphrased by a model.

### 9Router setup

[9Router](https://github.com/decolua/9router) is a separate open-source project (not
included here) that iSmart-LA uses as the gateway for the Claude Code fallback tiers --
it exposes a native Anthropic-compatible endpoint backed by a Claude Code OAuth login.
The installer can install a fresh instance for you (asks a yes/no question up front),
or you can point iSmart-LA at an existing instance if you already run one (e.g. shared
across multiple iSmart-LA deployments). Either way, **the actual OAuth login to Claude
happens in 9Router's own web dashboard** -- no installer can automate a browser login
on your behalf, so this one manual step is unavoidable regardless of which path you
choose.

## Commands

| Command | Cost | What it does |
|---|---|---|
| `/status` | **0 tokens** | Instant status check straight from a script (see `tools/`), no model involved |
| `/tools` | **0 tokens** | List of "graduated" skills (see below) |
| `/graduate <name>` | 1 call | Turn the case you *just* solved into a reusable script |
| `/new` | free | Reset the active session's conversation history (MEMORY.md untouched) |
| `/session <name>` | free | Create/switch to a named session, for keeping cases separate |
| `/sessions` | free | List saved sessions |
| `/remember <fact>` | free | Save a fact permanently, read in every session & every tier |
| `/memory` | free | View current memory contents |
| `/learned` | free | What the agent worked out about this environment by itself |
| `/forget <n>` | free | Delete one wrong learned fact (numbers from `/learned`) |
| `/chatid` | free, no auth needed | Reveal the current chat's ID (for group/access setup) |
| `/registergroup` | admin only | Open this Telegram group to every member, no restart needed |
| `/unregistergroup` | admin only | Revoke a group's access |
| `/help` | free | Full in-chat guide -- bilingual, pick EN or ID (or `/help en` / `/help id` directly) |

### Graduated skills (`/graduate`)

Some questions get asked repeatedly with only the target changing ("what's node X's
status", "is VM Y backed up"). The first time you solve one manually, `/graduate <name>`
turns that investigation into a small parameterized Python script, registered so future
identical-class questions get answered by running the script instead of re-deriving the
answer with a model every time. `tools/cluster_snapshot.py` (in the Proxmox example) is
the seed case: a single question class ("how's the cluster") that used to take a dozen-
plus exploratory tool calls now takes one script execution and ~600 tokens to summarize.

This is deliberately **not** automatic -- an agent deciding on its own what's "worth
saving" is exactly the kind of open-ended background decision this project avoids. You
say `/graduate`, once, when you're confident the case is a genuine reusable pattern.

### Group access

By default the bot only answers `ALLOWED_USER_IDS` (set at install time). To let a
whole Telegram group use it without whitelisting each member:

1. Invite the bot to the group.
2. Someone in `ALLOWED_USER_IDS` runs `/registergroup` inside that group.

That's it -- takes effect immediately, no restart. Only accounts in the original
`ALLOWED_USER_IDS` list can register (or unregister) a group; being authorized via an
already-registered group is deliberately **not** enough to grant a new one, so trust
can't cascade sideways from one group to another. See `/help` in-chat for the
member-facing explanation (Telegram's own group privacy setting also matters here --
covered there).

### Why automatic *learning* but not automatic *memory*?

These look like the same thing and aren't. `MEMORY.md` is injected into **every single
turn**, so anything that grows it raises the floor cost of every future message --
which is exactly how token spend runs away quietly. It stays manual (`/remember`).

The learned zone is injected **once per conversation**, is capped at 60 entries with
the oldest pruned, only accepts short self-contained facts, and reports in chat what
it just recorded. Bounded input, bounded size, bounded frequency, and visible -- a
different risk class from an agent freely deciding to rewrite its own instructions.

### Why not automatic memory?

Considered and deliberately rejected. An agent that decides on its own what's worth
remembering is one bug away from the same failure mode that burned ~900k tokens in this
project's own history (see "Why this exists" above) -- a broken automatic step retrying
itself with nobody watching. `/remember` requires a human to explicitly say "this is
worth keeping," which is a small amount of friction in exchange for zero risk of
runaway background cost.

## Repository layout

```
lite_agent.py              the bot itself
install.sh                 interactive installer
bootstrap.py               generates SOUL.md / GEMINI.md from a few questions
requirements.txt
.env.example                every setting, documented
SOUL.md.template            fallback blank brief (bootstrap.py normally writes this)
GEMINI.md.template          same, for agy (same content, different tool names)
tools/
  list_tools.py              prints the graduated-skill registry
  registry.json              starts empty; /graduate appends to it
examples/proxmox/           filled-in reference against a real Proxmox VE cluster
  SOUL.md.example
  GEMINI.md.example
  cluster_snapshot.py        seed "graduated skill" -- one-call cluster status
systemd/
  lite-agent.service.template
```

## Known limitations

- **`/graduate` is Claude-only.** It needs Claude Code's own conversation history to
  know what was "just solved" -- if the last turn was answered by Gemini (`mini` /
  `mini pro`), `/graduate` can't see it yet. Workaround: keep asking until Claude
  answers, or just re-describe the case to `/graduate` directly.
- **`MEMORY.md` is global**, shared across every chat (all DMs and all registered
  groups) that talk to this bot instance. There is no per-chat memory isolation yet.
  If you register multiple groups that shouldn't see each other's remembered facts,
  be aware of this before relying on it.
- **agy's OAuth token needs periodic re-login** (roughly hourly in testing, though this
  looked more like an intermittent internal race condition on Google's side than a hard
  expiry -- the automatic Claude fallback absorbs this gracefully when it happens, so
  it costs a failed attempt, not a broken response).
- **No built-in usage cap.** The 4-tier fallback and `/new` discipline keep normal usage
  cheap, but nothing currently stops a single very large, very exploratory request from
  eating a large chunk of a plan's usage quota in one turn (both subscriptions are
  fixed-price, but still rate/usage-limited, not unlimited). A hard per-turn budget
  ceiling is a reasonable thing to add before trusting this with a wide-open Telegram
  group.
- **`Bash`/`command(*)` access is effectively unrestricted** on both sides (this
  mirrors Claude Code's own default Bash tool, which has no built-in per-command
  scoping either). Anyone this bot answers to has, in effect, shell access on the host
  it runs on and SSH access to whatever `~/.ssh/config` reaches. Access control
  (`ALLOWED_USER_IDS` / `ALLOWED_GROUP_IDS`) is the only gate -- treat it accordingly.

## Credits

🚀 **Designed by Koko Ali & Dede**
💻 **Developed by Infrasoft.cloud & BSCloud.id Team**

Happy smart working! ✨
