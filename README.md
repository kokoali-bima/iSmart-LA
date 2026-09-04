# iSmart-LA (Lite Agent)

A lightweight Telegram bridge to **Claude Code** and **Antigravity CLI (agy)**, for
infrastructure monitoring and investigation -- built to be dramatically cheaper to run
than a full agent framework, while staying just as capable for real operational work.
A report doesn't have to stop at the chat: it can land straight in a shared
[Google Drive](#google-drive-optional) folder too, connected the same explicit way as
everything else here -- through Telegram, not a config file.

> **Status: v0.2b.67 -- early/beta.** Built and battle-tested against a real production
> Proxmox VE cluster over several days of iteration, including a live-fire test of the
> unlock/PIN/snapshot flow against real infrastructure. Works well; still has known
> rough edges (see [Known limitations](#known-limitations)).

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

## Features

What it actually does, at a glance -- details and full command reference are further
down, this is the map.

**Cost that's measured, not guessed**
- Four fixed-price tiers, cheapest first, automatic failover -- no per-token API bill on
  either side (see [Architecture](#architecture)).
- `/spend [days]` reads a real per-turn token ledger straight from disk, **0 model
  tokens** -- including tokens burned by a tier that failed before another answered,
  a number that used to only be reachable by reading log prose one line at a time.
- An optional hard ceiling on what a single turn may burn across the whole chain
  (`TURN_TOKEN_CEILING`), off by default until you've actually measured your own
  traffic with `/spend` -- a number picked before measuring would either never fire
  or kill real work.

**Change access that's really gated, not just asked nicely**
- Read-only by default. A destructive command needs a time-boxed `/unlock` window,
  opened with a PIN entered on an inline keypad -- the digits never appear as chat
  text, so nothing sensitive sits in message history.
- On the machines it manages, the agent's own SSH key is swapped for a locked-down
  one outside that window, enforced by `command=` in `authorized_keys` on the far
  end -- an **allowlist** of read-only verbs, not a denylist of dangerous ones,
  so something spelled a way nobody anticipated is refused, not silently permitted.
- A VM change gets a snapshot first, taken by the bot itself before access opens --
  never left as an instruction the model might skip.

**Built for more than one team on one deployment**
- Every registered group can carry its **own** PIN, independent of the owner's
  master one -- multiple companies can share one deployment without sharing a
  secret.
- `/remember`'s memory is **per chat** -- a fact saved in one group's chat is never
  injected into another chat's next turn.
- The owner alone can grant themselves extra scope that applies **only** in their
  own private DM, never in any group even when they're the one typing there
  (`/setownerscope`).

**Learns about your infrastructure -- inside a boundary it cannot edit**
- The agent can record durable facts about the environment it's managing (a working
  tool invocation, a corrected topology, a naming convention) as it works, so the
  next conversation starts already knowing them -- but only ever by *appending*
  through one narrow, code-enforced channel. The rules above that line -- what must
  never be touched -- are never something the model can rewrite, however it's asked.
- `/graduate` turns a case just solved into a reusable script that costs **0 tokens**
  to run again -- always triggered by a human once, never something the agent
  decides on its own is worth saving.

**Extendable through MCP, without extending this codebase**
- Both CLIs underneath already speak [MCP](#mcp-servers-optional), so a
  registered server's tools become available to the agent with no new agent
  code -- including on the cheap default tier, not just the expensive one.
- A working server ships in the repo: read-only, locked to one folder, pure
  standard-library Python, nothing to install.
- Registering one is PIN-gated like `/addserver`, because an MCP server can
  act on the agent's behalf -- and the model can never register one itself.

**Hardened as part of installing, not as a follow-up task**
- The systemd unit ships sandboxed (`systemd-analyze security`: **5.8 MEDIUM**,
  down from **9.6 UNSAFE**), and `install.sh` locks every secret and state
  file to owner-only -- see [Hardening](#hardening-applied-at-install-time).

**Reports don't have to stay in the chat**
- The agent can write a file and hand it back through Telegram, or -- see
  [Google Drive](#google-drive-optional) -- drop it straight into a shared folder,
  connected the same explicit, Telegram-driven way as everything else here.

**Stays out of the way when nothing's wrong**
- `/status`, `/providers`, `/servers`, `/schedules`, `/boundaries`, `/snapshots`,
  `/tools` -- all **0 model tokens**, answered straight from a script or a file.
- Self-updates on your say-so: `/update` shows what changed and asks before
  installing, and if the new code doesn't even compile, the update rolls itself
  back automatically rather than leaving the bot dead.
- Bilingual replies (`/lang en` / `/lang id`), per chat, independently.

**What this buys you over a full agent framework**
Tried that route first, on the same infrastructure this bot now manages, before
building this (the ~900,000-token incident above is from that attempt) -- and beyond
that one incident, the same 7-node benchmark task cost **3.5x more** there than it
costs here, on ordinary turns with nothing going wrong. iSmart-LA's answer isn't a
smarter loop -- it's **no loop**: every token spent is because a human asked for
something, right now, enforced by what's absent from this codebase, not by a setting
that could drift back on.

## Architecture

```
Telegram message
      |
      v
 lite_agent.py --- tries 4 tiers, cheapest first, falls through on failure ---
      |
      +-- 1. agy (Antigravity CLI) -- Gemini Flash        "mini"       (fixed-price, Google AI Pro/Ultra)
      +-- 2. agy (Antigravity CLI) -- Gemini Pro-low       "mini pro"   (fixed-price, Google AI Pro/Ultra)
      +-- 3. claude (Claude Code CLI) -- Haiku             "dede iku"   (fixed-price, Claude Pro/Max)
      +-- 4. claude (Claude Code CLI) -- Sonnet            "dede nnet"  (fixed-price, Claude Pro/Max)
```

**Both sides are fixed-price subscriptions, not pay-per-token API billing.** Gemini runs
on a Google AI Pro/Ultra plan via agy; Claude runs on a Claude Pro/Max plan via Claude
Code's own sign-in. Both are first-party CLIs signing in to their own accounts -- no
gateway, no relay, no borrowed credentials. Gemini is tried first not to dodge per-token
cost (there isn't any here), but to spread routine load across a **separate**
subscription and keep the Claude plan's quota in reserve for when it's genuinely
needed -- useful because that quota is often shared with a human's own interactive
Claude Code usage on the same account.

- Every reply ends with a small tag (`— by mini`, `— by dede nnet`, etc.) showing which
  tier actually answered, so escalations away from the cheap default are visible at a
  glance without digging through logs.
- Each of the 4 tiers keeps its **own** conversation history. They don't share context
  with each other -- if a turn falls through from Gemini to Claude, Claude answers that
  turn cold. (Trade-off found necessary in testing: letting one tier resume a
  conversation another tier started produced a single turn costing several times more
  than a fresh conversation would have.)
- A reply isn't limited to the chat it started in: the agent can write a file and hand
  it back through Telegram, or -- see [Google Drive](#google-drive-optional) -- drop it
  straight into a shared folder instead, when a report needs to land somewhere a client
  or teammate can browse.

### `/usemodel` -- an opt-in override, not a new default

The 4-tier chain above is fixed on purpose (see "Design principles" below) -- this
deployment's own needs, cheapest first, not a general knob. `/usemodel` doesn't change
that default; it adds two **extra** tiers that sit outside the automatic chain entirely,
reachable only by asking for one by name, for a case that genuinely needs more than the
default chain offers:

```
+-- Claude Opus              "dede opus"      (fixed-price, Claude Pro/Max)
+-- Gemini Pro-high           "mini pro max"   (fixed-price, Google AI Pro/Ultra)
```

`/usemodel dede opus` forces that tier for the rest of that chat's turns; the default
chain still backs it up if it's ever unavailable, rather than hard-failing (the
`— by ...` tag on every reply already shows when that safety net had to fire).
`/usemodel auto` goes back to the default chain. `/usemodel` alone shows what's active.
The override is per-chat -- a group and a DM can each have their own, independently.

Gated the same as `/addserver` (owner anywhere, or a registered group's own admin):
picking Opus or Pro-high spends this deployment's own shared subscription quota, so
it isn't left open to anyone who can merely talk to the bot.

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

**It asks exactly two questions:** your Telegram bot token, and your Telegram user
ID. Everything else it does without asking — system dependencies, a Python venv,
both CLIs, and a systemd service.

**Everything else happens in Telegram.** Send `/start` and a setup card walks through
what's left: signing in to Gemini, signing in to Claude, setting the PIN, and saying
what this agent looks after. Each sign-in is a URL to open and a code to paste back;
you can stop halfway and come back. `/start` again any time to see what's still
outstanding or change something.

Then `/addserver` gives it a machine it may reach, and `/addboundary` records
anything it must never touch. Both are changeable later without touching the server.

That split is deliberate. Anything needing a browser or a decision belongs where the
operator already is, not in a terminal session they have to keep open.

**Not just for Proxmox.** Nothing in the core assumes Proxmox, or infrastructure at
all. [`examples/proxmox/`](./examples/proxmox/) is one worked example, not a required
shape -- a Kubernetes cluster, a fleet of web servers, or a CI estate all work the
same way.


### The environment brief, and how it fills itself in

The one genuinely per-deployment thing is what the agent is looking after, how it
reaches it, and what it must never touch. That lives in `SOUL.md` (Claude's brief)
and `GEMINI.md` (agy's). You don't hand-author them, and none of it is asked during
install — all three come from Telegram:

| | |
|---|---|
| **what it looks after** | the fourth item on `/start`'s setup card, or `/setbrief <one line>` |
| **how it reaches machines** | `/addserver`, which also installs the agent's own key |
| **what it must never touch** | `/addboundary` (run it bare for an explanation) |

[`bootstrap.py`](./bootstrap.py) is still there for anyone who prefers answering a
longer set of questions in a terminal — run it by hand any time to regenerate both
briefs from scratch. The installer no longer calls it.

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

#### `/setownerscope` -- extra scope, owner-only, DM-only

`/setscope` is deliberately **one shared setting** -- every group and every DM sees
the exact same brief, on purpose, so there's one predictable answer to "what is this
bot for" everywhere it's used. `/setownerscope` sits on top of that for one specific
case: the owner wants the bot to also help with general things (a joke, casual
questions) in their own DM, without loosening what every group gets.

It applies only when BOTH are true for the message actually being answered right
now: the sender is the owner, AND the chat is the owner's own private DM -- never a
group, even one the owner happens to be speaking in, and checked fresh on every
turn so a conversation the owner started can't go on granting it to whoever else
continues it, or vice versa. Nothing needs to be reset when the group scope changes
later; the two are independent layers, not a copy that can drift out of sync.

```
/setownerscope also help with general questions and the occasional joke, not just infrastructure
```

`/setownerscope clear` removes it; running it bare shows what's currently set.

### Using a gateway instead (optional)

Claude Code signs in to your subscription directly, so **no gateway is needed** — this
project used to route through [9Router](https://github.com/decolua/9router) and no
longer does. Dropping it removed Node.js, npm, pm2, a second service to keep alive, and
a separate dashboard login.

The path is still there if you want it. Set **both** `ANTHROPIC_BASE_URL` and
`ANTHROPIC_API_KEY` and traffic goes through that gateway instead. Two things to know:

- Model ids may need the gateway's own provider prefix (9Router wants `cc/`; a bare id
  404s there). Adjust `TIERS` accordingly.
- When those two are unset they're actively **removed** from the CLI's environment, not
  just skipped — an inherited value from your shell would otherwise silently override
  the CLI's own sign-in and send traffic somewhere you didn't intend.

A gateway is also the shortest route to models neither CLI speaks natively (a local
Ollama, say), since something has to translate between protocols.

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
| `/schedules` | **0 tokens** | Everything that runs on a timer, and what it does |
| `/unschedule <name>` | owner + DM only | Remove a scheduled task |
| `/adopt` | owner + DM only | Bring pre-existing cron entries under management |
| `/setpin` | owner, DM **or group** | Set/change the OWNER's PIN -- works everywhere |
| `/setgrouppin` | owner/admin, in that group | Set/change THAT group's own PIN |
| `/rmgrouppin` | owner/admin, in that group | Remove that group's own PIN, falls back to the owner's |
| `/update` | owner/admin + PIN | Check GitHub for a newer version and install it |
| `/setbrief <one line>` | owner/admin | Say what this agent looks after (also the 4th item on `/start`) |
| `/setscope <phrase>` | owner/admin | Change what KIND of assistant it is, not just what it manages |
| `/setownerscope <text>` | owner, **own DM only** | Extra scope on top of `/setscope`, for the owner alone, in their own DM only -- never a group, even one the owner is speaking in |
| `/logout` | owner/admin | Clear a sign-in (Gemini or Claude) for a genuinely fresh /start |
| `/boundaries` | **0 tokens** | What the agent must never do |
| `/addboundary <rule>` | owner/admin | Add a hard boundary — run it bare for an explanation of what that means |
| `/rmboundary <n>` | owner/admin + PIN | Remove one |
| `/snapshots` | **0 tokens** | Snapshots taken before changes |
| `/cancel` | free | Abort a multi-step form (/start, /addserver) |
| `/servers` | **0 tokens** | Machines the agent may reach |
| `/addserver` | owner/admin + PIN | Register a new machine, step by step |
| `/removeserver <name>` | owner/admin | Unregister one |
| `/agentstatus` | tiny probe each | Live check: is each tier actually up right now? |
| `/providers` | **0 tokens** | Which AI tiers are configured, and which are healthy |
| `/usemodel [name]` | owner/admin | Force a specific tier for this chat (Opus, Gemini Pro-high, ...); `auto` for the default chain |
| `/addmcp <name> <cmd> [args]` | owner/admin + PIN | Register an MCP server -- run it bare for a ready-to-use, no-install example |
| `/rmmcp <name>` | owner/admin | Withdraw one (no PIN -- it only reduces capability) |
| `/mcpservers` | **0 tokens** | What MCP servers are registered |
| `/gdrivestatus` | **0 tokens** | Is each connected Drive account still working? |
| `/gdrive` (disconnect) | owner/admin | Same card also disconnects an account: revokes access at Google, deletes the local token, deletes nothing in Drive |
| `/gdrive` | owner/admin, **0 tokens** | Pick (or show) which connected Drive account this room uploads to |
| `/lang` (or `/language`) | owner/admin, **0 tokens** | Set/show this chat's language for the bot's own fixed replies (`en`/`id`) |
| `/mode` | **0 tokens** | Read-only right now, or able to change things? |
| `/unlock [min]` | owner/admin | Open a time-boxed window for real changes (capped at 10 min from a group) |
| `/lock` | owner/admin | Close that window early |
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

### The PIN, and why it isn't typed

Sensitive actions — installing a scheduled task, opening write mode — need a
6-digit PIN. **The PIN is entered on an inline keypad, never typed as a message.**
The digits ride in `callback_data`, so nothing lands in the chat history: there is
no message to scroll back to, and none to forget to delete. That is exactly why
this is not a typed password — a password in a chat log is a permanent credential
sitting in cleartext on servers you don't control.

A button tap alone isn't enough for the cases actually worth defending against: a
member's phone being compromised, or someone getting into a group they shouldn't
be in. In both, the attacker *has* the account, so a tap proves nothing. The PIN
is the factor that doesn't come along with a stolen session.

Stored as a salted scrypt hash, never the PIN itself. Five wrong tries locks it
for 15 minutes — six digits is only a million combinations, so that lockout is
doing real work.

**Where each PIN action is allowed** is decided per action, not once for all of
them:

| Action | DM | Group |
|---|---|---|
| `/setpin` — set or change the PIN | ✅ | ✅ |
| `/unlock` — open write mode | ✅ (up to 60 min) | ✅ registered group's own admin (up to 10 min) |
| Installing / removing a scheduled task | ✅ | ✅ (registered group's own admin) |

`/setpin` works in a group because nothing secret is exposed there: the digits
travel in `callback_data`, so the group sees a keypad and a row of dots, never
the PIN. `/unlock` gets the same trust level `/addserver` and scheduling
already have — the bot owner anywhere, or an admin of a **registered** group,
confirmed by that group's own Telegram admin status — but a group-opened
window is capped at 10 minutes regardless of what's asked for, well short of
the 60-minute ceiling a DM gets. It still opens write access for *any* change
the agent decides to make next, so the shorter leash is the trade-off for
reaching it from a group at all. Anyone else's tap is refused, regardless of
which action they're trying to confirm.

**Changing an existing PIN always requires the current one first.** That is what
makes the group case safe: a stolen Telegram session can't quietly replace the
PIN, because it would have to know it. There is no reset path through Telegram at
all — deliberately. (Someone with shell access on the host could delete
`pin.json`; the PIN defends against a compromised Telegram account, not a
compromised server. If the host is owned, the SSH keys are gone anyway.)

### Scheduled tasks

The agent used to create schedules by editing crontab itself. It worked — and the
job existed nowhere else: it couldn't be listed, couldn't be removed from
Telegram, and a second one appearing would go unnoticed. An unattended job nobody
can enumerate is the exact shape of problem this project exists to avoid.

Now the agent only *proposes*, by emitting a `SCHEDULE:` line. Everything after
that belongs to the bot: you get a confirmation card, approve it with your PIN,
and only then is it installed — into a **managed block** of the crontab, so any
cron entry you wrote yourself is left completely alone.

| Command | What it does |
|---|---|
| `/schedules` | Everything on a timer, what it runs, and whether it has write access — **0 tokens** |
| `/unschedule <name>` | Remove one |
| `/adopt` | Pull pre-existing cron entries into the registry so they become visible and removable |

`/schedules` also lists cron entries it does *not* manage, rather than hiding
them — the point is that nothing runs unattended without being visible, and that
has to include things the bot didn't install.

**Write access for a scheduled job** is opt-in per task (`write=yes`), stated on
the confirmation card, and shown with a ⚠️ in `/schedules`. Every cron line calls
`tools/run_scheduled.py <name>`, never the command directly — so the registry
stays authoritative: a task can't change what it does or how much access it has by
editing a crontab line, and a task removed with `/unschedule` stops running even
if a stale cron line survives. Write access is granted for the duration of that
one run, via a temporary `ssh` shim, and discarded afterwards.

### Adding a server

`/addserver` walks through it in chat: kind (hypervisor / single machine /
other), flavour if it's a hypervisor, then a name, address, SSH user and port.

**No private key ever passes through Telegram.** The bot generates the keypair
on the host and shows you only the **public** half, with the one-line command to
install it on the target. A private key sent as a chat message would be a
permanent credential to production sitting in cleartext on servers you don't
control — strictly worse than the password this project already declined, since
a leaked key opens every node until somebody notices. It also means less typing:
address, user, port. Nothing secret.

Then it tests the connection, and for Proxmox offers a read-only cluster scan
(which nodes exist, how many guests) — no write mode needed. Discovered nodes go
into `~/.ssh/config` and into the agent's learned zone, so it can reach them from
the next message on.

`/servers` lists what's registered (0 tokens). `/removeserver <name>` unregisters
one. Both `/addserver` and `/removeserver` are owner-or-group-admin, and
`/addserver` asks for the PIN first — it grants access to a machine.

Only a marked block of `~/.ssh/config` is ever rewritten; anything you put there
by hand is copied through untouched (tested explicitly — clobbering somebody's
own SSH config would be a worse bug than the one this fixes).

### Google Drive (optional)

Lets the agent save a file straight to a Google Drive folder instead of (or in
addition to) sending it through Telegram — useful once reports need to land
somewhere a client or teammate can browse, not just be attached to a chat message.
More than one account can be connected (a personal one, a company one, one per
client...) with each chat picking its own default independently.

**Connecting an account is `/connectgdrive`, in Telegram** — and since v0.2b.54 it
finally works the way signing in to Gemini and Claude already did: open a URL, type
a code, done. No terminal, on any machine, at any point.

```
/connectgdrive
```

The bot replies with a link and a short code. Open the link on anything with a
browser — your phone is fine — enter the code, approve. The bot picks the result
up by itself; there is nothing to paste back.

<details>
<summary>Why this took a Google Cloud client, and why that is a one-time step</summary>

That flow is Google's OAuth **device authorization grant**, and two facts decide
whether it can be used here at all. Both were checked against Google's own
documentation and against the live endpoint, not assumed:

- The device flow supports only a **limited scope list**. Of the Drive scopes,
  just `drive.appdata` and `drive.file` are on it — full `drive` is not.
  `drive.file` is what this project already requested, so **nothing about what
  the bot can reach changes**: it sees files it created, and nothing else.
- `drive.file` is classed **non-sensitive**, so publishing the OAuth client needs
  no Google review.

What it does need is an OAuth client of type **"TV and Limited Input devices"**.
rclone's own built-in client is a Desktop-type one, and Google refuses it outright
(`invalid_client` / "Invalid client type" — confirmed by trying it). So
`/connectgdrive` asks once, on first use, for a client from your own Google Cloud
project, and walks through creating it. It is stored `chmod 600` on the server,
never committed anywhere, and one client serves the whole deployment — every
person still authorises their **own** Google account through it.

One step in that setup genuinely matters: **Google Auth Platform → Audience → Publish app**. Note that choosing User type "External" is NOT the same thing -- an app can be External and still sit in Testing, where Google refuses every account not on its test-user list, including your own, with `Error 403: access_denied`. An app left in "Testing" gets a refresh token that **Google expires after
7 days**, and no amount of keep-alive can prevent that — it is a revocation, and
refreshing requires a live refresh token. Publishing needs no review for a
non-sensitive scope.

</details>

**`/connectgdrive manual` keeps the old paste-a-token path**, for the one case the
device flow cannot serve: `drive.file` only reaches files the bot itself created,
so writing into a folder you made by hand needs a full-`drive` token, which Google
will not issue over the device flow. That path still runs
`rclone authorize drive` on a machine you control and takes the printed
`{"access_token"...}` block pasted back into the chat.

**`/gdrivestatus`** shows whether each connected account still works — 0 model
tokens, one `rclone` call. Worth having because every way a Drive connection dies
is silent: access revoked, the OAuth client deleted, the password changed, or that
7-day expiry above. Without it, the first sign of trouble is a report that never
arrives.

Either way, once a token is in hand the bot:

- picks a collision-free name (`gdrive` for the first account, asks for a short
  label like `company` or `clienta` for a second+ one -- becomes `gdrive_company`)
- registers it with `rclone config create`, never by hand-editing `rclone.conf`
- checks whether this account already has the shared `iSmart-LA Data` root folder
  before creating one, so re-authorizing the same account by mistake (a typo'd
  label, say) can't silently produce a second folder with nothing to notice until
  files start landing in the wrong one
- **verifies** with a real listing before calling it connected -- reported success
  always means an actual Drive call worked, not that a file was written
- rolls back cleanly (removes the half-configured remote) on any failure, so a
  bad paste never leaves something broken lying around
- deletes your pasted message immediately after reading it, same as an OAuth code

Uses [rclone](https://rclone.org/drive/) with `scope=drive.file`, so a connected
account only ever exposes files rclone itself creates — never its existing Drive
contents. `/cancel` stops the flow at any point.

Once at least one account is connected, `/gdrive` in any chat shows a picker —
tap one to make it that chat's default. **With exactly one account connected,
every chat uses it automatically** — no ambiguity to ask about, so there's nothing
to pick. The moment a *second* account is connected, any chat that hasn't
explicitly chosen yet must pick one via `/gdrive` before uploading — deliberately
no silent fallback to "whichever account connected first" once there's a real
choice, since that's exactly the kind of default that sends a file to the wrong
place unnoticed. A chat that was already auto-using the sole account keeps working
unchanged; only chats that never uploaded anything are asked to choose.

From then on, mention "gdrive" or "Google Drive" in a normal message and where you
want it, and the agent handles the rest:

> "save this as report.md to gdrive /client-a/report.md"

The agent creates the file, then emits a line the bot recognises
(`GDRIVE: file=<local path> | to=<relative path>`), which is stripped from what
you see; the bot uploads it (creating any missing subfolder automatically) and
replies with a shareable link. Same secret-scan gate as sending a file through
Telegram — a file containing a credential is refused, not uploaded.

**In a group, uploads land inside that group's own subfolder automatically** —
`iSmart-LA Data/<group name>/...` — without the model needing to know or add the
group's name itself. Asking for the shared root instead (a path starting with `/`)
only works for that group's own admin (or the owner); anyone else's attempt is
quietly kept inside the group's folder rather than refused outright, the same way
an untrusted fact from a group is quietly not remembered rather than erroring. This
matters most when several groups share the *same* connected account (e.g. one
company account used across multiple client rooms) — the folder split is a
convenience default, not a hard permission boundary enforced by Google itself, so
treat the escape hatch as something only a trusted admin should reach for.

**Known limitation:** rclone's shared default `client_id` (used above, since it
needs no Google Cloud project of your own) is being retired sometime in 2026 and
can occasionally hit a shared rate limit under global load (rclone retries with
backoff automatically). If it stops working, the fix is creating your own
`client_id` — see rclone's docs linked above.

### MCP servers (optional)

[MCP](https://modelcontextprotocol.io) is an open protocol for giving a model
new capabilities -- read a database, reach an internal API, browse a document
store -- through a small separate program called an MCP server. **Both** CLIs
underneath this bot already speak it, so registering one here inherits that
whole ecosystem without a line of new agent code.

```
/addmcp reports python3 tools/mcp_readonly_fs.py /root/lite-agent/reports
/mcpservers          what is registered  (0 tokens)
/rmmcp reports       withdraw one
```

**A working default ships with this repo.** `tools/mcp_readonly_fs.py` is a
read-only, path-locked MCP server in pure standard-library Python -- no `pip
install`, no `npx`, no Node.js, nothing to install at all beyond the `python3`
this project already requires. It gives the model two tools (`list_files`,
`read_file`) scoped to exactly one folder. Everything outside that folder is
refused: a `..` segment, an absolute path elsewhere, or a symlink pointing
out are all turned away, and files over 200 KB are refused rather than
silently truncated.

Nothing restricts you to it. MCP is a wire protocol, not a language or a
package -- any server that speaks it works, including the npm-published ones:

```
/addmcp sentry npx -y @sentry/mcp-server        # needs Node.js on this host
```

Node.js is **not** installed for you. This project installs what it actually
uses, and a bot with no npm-based MCP server registered has no reason to carry
a JavaScript runtime.

**How it is gated, and why.** `/addmcp` is locked exactly like `/addserver`:
owner anywhere, or a registered group's own admin, PIN required. That is
deliberate -- an MCP server can read and write on the agent's behalf, so
adding one is handing out real trust, and it is a decision a person makes,
never one the model can make for itself. `/rmmcp` needs no PIN, for the same
reason `/addboundary` doesn't: it only ever *reduces* what the agent can do.
A registered server grants its whole tool set (`mcp__<name>`), the same
granularity `/addserver` already uses for a whole machine.

The registry lives in `mcp_servers.json`, in the exact shape Claude Code's
`--mcp-config` expects, so it is handed over unmodified; agy keeps MCP servers
in its own persistent config instead, so registrations are pushed to it with
`agy mcp add`. If agy is missing or too old for the subcommand, that is logged
and the Claude side still works.

### Hardening (applied at install time)

This host is worth more than any single machine it manages: it holds the
Telegram bot token, the PIN hash, and the SSH keys that reach every managed
node. So hardening is **step 8 of installing**, not a page in the docs someone
gets to eventually.

| | before | after |
|---|---|---|
| `systemd-analyze security lite-agent` | **9.6 UNSAFE** | **5.8 MEDIUM** |
| `sessions.json`, `spend.jsonl` | `644` -- world-readable | `600` |

What the installed systemd unit does: `/usr`, `/boot` and `/etc` are read-only
to the service (`ProtectSystem=full`); no device access; kernel tunables,
modules, logs, cgroups, the clock and the hostname are all off limits; it
cannot create setuid files, schedule realtime, change its execution domain, or
create namespaces; only the socket families it genuinely uses are permitted;
no writable-executable memory; and `UMask=0077`, so the state files it creates
are owner-only rather than world-readable.

What `install.sh` does beyond that: `chmod 600` on every secret and state file
already on disk (`UMask` only governs *new* files, so anything an earlier
version wrote keeps its old mode -- which is exactly how a real deployment
ended up with world-readable conversation state), `chmod 700` on the per-chat
memory directory and `~/.ssh`, and a printed note that this bot needs **no
inbound ports at all**, with the two-line `ufw` command if the host is exposed.

Three directives are deliberately left **off**, and the unit file says why
next to each. Briefly: `NoNewPrivileges` removes exactly the setuid escalation
`/update`'s `sudo -n systemctl restart` depends on (the bot would update itself
and never come back on a non-root install); `ProtectHome` was measured to break
both the install directory and the `~/.ssh` write-mode key swap outright on a
root deployment; and `PrivateTmp` hides the sign-in tmux sessions from an
operator trying to see what a stuck login is actually showing.

Every directive here -- including the ones that are on -- was checked against
the real workload on a live host before shipping, with a probe that wrote to
the install directory, rewrote `~/.ssh`, resolved DNS, opened outbound TLS,
spawned both CLIs, reached tmux and made the `sudo` hop: 10/10 under the final
unit. Two initial assumptions turned out to be wrong and the measurements
overruled them -- `MemoryDenyWriteExecute` was assumed to break the Node-based
CLIs and does not (a real Claude turn *and* a real agy turn both completed
under it, so it is enabled), while `ProtectHome` was assumed merely awkward and
in fact breaks the install.

**Existing deployments get it automatically, at startup.** Hardening is applied
every time the bot starts, not only during `/update` -- because an update is
carried out by the *old* version's code, so a fix to the update path can never
apply itself and always lands one release late. That is not hypothetical: it
happened here, and a server sat on the newest release with an unhardened unit
while the fix for it was installed and idle. If the running unit turns out to
be out of date, the bot refreshes it and restarts **once** (the service, not
the machine) so the sandbox actually takes effect; that restart is conditional
on the unit having genuinely changed, so it cannot loop.

**`/update` applies it too.** The unit systemd actually runs
is the copy in `/etc/systemd/system`, not the template in the repo, so
`/update` re-renders and reinstalls it before restarting -- otherwise a
release that hardens the unit would land in the checkout while the service
kept running unprotected, and you would have no way to tell. If the render
comes out malformed, or `daemon-reload` fails, the previous unit is restored
rather than leaving a bot that cannot come back up. Re-running `./install.sh`
does the same thing and is safe to repeat.

### Updating (`/update`)

`/update` compares this deployment against the repository and, after a PIN,
fast-forwards to the newer version and restarts. It also mentions a new version
on its own — checked when you send a message, at most once every six hours,
never on a timer, and never twice for the same version.

What it refuses to do matters more than what it does:

- **It will not run at all unless the deployment is a `git clone`.** A copy
  installed by moving files has no remote to compare against, and `/update`
  says exactly that instead of guessing.
- **It will not discard local commits.** If this copy has work the repository
  does not, it reports the divergence and changes nothing. Sorting that out by
  hand is better than throwing away whatever those commits were.
- **It will not ship a build that does not parse.** The new code is
  compile-checked before the restart; if it fails, the checkout is reset to the
  previous commit. Without that, systemd would restart the service into the
  same crash every five seconds.

The confirmation comes from the process that comes *back*, not the one that
died — which is the only way to show the new version actually starts. Your
`.env`, briefs, sessions, PIN and registered servers are untracked by git and
are never touched.

It needs a PIN because it replaces the code the bot runs, which is a strictly
larger capability than `/unlock` (that only widens an SSH credential for a few
minutes).

### Per-chat language (`/lang`)

The bot's own fixed text can reply in English or Indonesian, picked per chat with
`/lang en` or `/lang id` (`/language` works too, identically — `/lang` is just
shorter to type). `/lang` alone shows what's currently set. New chats get asked
once, the first time `/start` shows its setup card; existing deployments fall
back to `DEFAULT_LANGUAGE` (`id` unless set otherwise in `.env`) until a chat
picks explicitly.

**Every command and flow is migrated** — every reply, every wizard step
(`/start`'s own setup card, `/addserver`'s step-by-step form), the whole PIN
system and keypad, and the schedule/unlock confirmation cards all respect
whichever language the chat picked. Nothing in the bot's own fixed text is
locked to one language any more. A follow-up audit swept every
`reply_text`/`edit_message_text`/`answer` call in the file specifically
looking for anything the per-command migration passes missed — it found six
(file-delivery errors, one exception handler each in `/graduate` and the
server wizard, a permission-denial message, the turn-level error/retry/LEARN
messages, and the last-resort global error handler), all now fixed too.

This is separate from two other things it's easy to conflate: **the agent's own
answers** already mirror whatever language you write your prompt in, being an LLM
— nothing to configure there. **`/help`** has always had its own EN/ID choice
(`/help en`, `/help id`, or the button), picked per-message rather than stored per
chat, and is untouched by `/lang`.

The pattern behind it, if it's ever useful again: `lang = _chat_lang(update)`,
then wrap each reply string in `_t(lang, "English", "Indonesian")`.

### Write mode

This agent is meant to do real work — create VMs, repair them, tweak node and
cluster config, run security audits. Cutting it down to read-only would make it
useless for the jobs it exists for, so capability is not the thing that gets
restricted. **Timing is.**

Nearly every turn is a question, and a question needs no write access. The rare
turn that changes something is one a human deliberately started. So:

| | |
|---|---|
| **Locked** (default) | `~/.ssh/agent_active` → a restricted key. Reads, audits, monitoring, reports all work exactly as normal. |
| **Unlocked** (`/unlock 20`) | Same symlink → the full root key, for N minutes, then it flips back on its own. |

What makes this hold is *who can flip it*. `/unlock` is owner-or-group-admin, same
as `/addserver` and scheduling — a registered group's own admin can reach it, but a
window opened from a group is capped at 10 minutes regardless of what's requested,
against 60 for a DM. Whichever chat opens it, it is not something the model can do,
request, or be talked into by text it read in a log file, a web page, or a group
message. Prompt injection can make the agent *try* to change something; it cannot
make the credential exist. Expiry is checked on read, so there is no timer and no
background task — consistent with this project's rule about background work. Any
failure in the swap leaves the restricted key in place.

Commands: `/mode` (what can it do right now, 0 tokens), `/unlock [minutes]`, `/lock`.

**Setup** (until you do this, the mechanism is inert and says so — the agent keeps
using whatever single key it has today):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/agent_readonly -N ''
ssh-keygen -t ed25519 -f ~/.ssh/agent_write    -N ''
ln -sf ~/.ssh/agent_readonly ~/.ssh/agent_active
# point ~/.ssh/config's IdentityFile at ~/.ssh/agent_active
```

On each managed node, install [`node-guard/pve-ro-guard`](./node-guard/pve-ro-guard)
as `/usr/local/bin/pve-ro-guard` (chmod 755), then in `/root/.ssh/authorized_keys`:

```
command="/usr/local/bin/pve-ro-guard",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA...  agent-readonly
ssh-ed25519 AAAA...  agent-write
```

`command=` means sshd runs the guard *instead of* whatever was requested — the client
cannot opt out, because it is the only thing that key can run. The guard is an
**allowlist** of read-only verbs, not a denylist of dangerous ones: something spelled
a way nobody anticipated gets refused rather than silently permitted. It is also
scoped carefully enough not to block genuine audit work — `grep -i failed auth.log`,
`last`, `ufw status`, `openssl x509` and friends all pass, while `qm stop`, `sed -i`,
`find -delete`, redirects, command substitution and pipes into a writer do not.

### Group access

By default the bot only answers `ALLOWED_USER_IDS` (set at install time). To let a
whole Telegram group use it without whitelisting each member:

1. Invite the bot to the group.
2. Someone in `ALLOWED_USER_IDS` runs `/registergroup` inside that group.

That's it -- takes effect immediately, no restart. With nothing else changed, the
bot only ever *sees* a command or a reply to one of its own messages -- Telegram's
**Privacy Mode**, ON by default for every new bot, which does *not* actually forward
a plain `@botname` mention typed mid-sentence despite it looking like a valid
mention in the client (confirmed live: it left zero trace on the server even while
the log was being watched in real time as it was sent). Everyday group chatter
stays invisible either way, and invisible costs nothing: **this is what you want
for most groups**, so the bot answers when asked and stays out of the conversation
otherwise.

To also make a real `@botname` mention wake it up -- not just a reply:

1. Turn Privacy Mode off: **@BotFather** → `/mybots` → this bot → *Bot Settings* →
   *Group Privacy* → *Turn off*.
2. **Remove the bot from the group, then invite it back.** This step is not
   optional and is the one that looks like the feature is broken when skipped:
   Telegram applies a bot's privacy setting **at join time**, so a group the bot
   was already in keeps the OLD setting no matter what BotFather says afterwards.
   `getMe` will report `can_read_all_group_messages: true` while that group still
   silently drops every mention -- verified live, with debug logging on the very
   first line of the handler showing *zero* incoming messages until the bot was
   re-added, and every mention arriving normally the moment it was.

The group does **not** need `/registergroup` again -- the chat ID doesn't change,
so its registration and PIN survive the re-invite.

Privacy Mode off makes Telegram forward every group message to the bot, but it does
**not** turn the bot into a full participant: the same gate that used to be Privacy
Mode's job now runs in the bot itself, so ordinary chatter still never reaches the
model or spends any quota -- only a reply to the bot or an actual `@botname` mention
does (the mention text itself is stripped out before the model sees it, and a
mention of *someone else* in the group is left alone and doesn't wake the bot).
Skip both steps for a group that's fine with reply-only.

Only accounts in the original
`ALLOWED_USER_IDS` list can register (or unregister) a group; being authorized via an
already-registered group is deliberately **not** enough to grant a new one, so trust
can't cascade sideways from one group to another. See `/help` in-chat for the
member-facing explanation.

**Multiple companies sharing one deployment** don't have to share a PIN either:
`/setgrouppin` (that group's own admin, or the owner) gives a registered group its
own PIN, used instead of the owner's when confirming something from inside it.
The owner's own PIN keeps working everywhere regardless, as a master credential.
`/rmgrouppin` removes a group's own PIN again, falling back to the owner's.

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

### /agentstatus vs /providers

`/providers` is passive: it shows the cooldown table built from real usage, so a
tier that has not been touched since the last outage still reads "ready" even if
it is down right now. `/agentstatus` is active -- it sends a tiny real probe to
every configured tier, in parallel, right now, with no environment brief and no
memory attached (cheap and fast on purpose). 🟢 online, 🔴 down with the actual
error. Results feed the same cooldown table `/providers` reads, so a real outage
found this way also protects the next real turn from wasting a full attempt on a
tier that just proved to be down. A 20-second cache guards against an accidental
double-tap; `/agentstatus force` bypasses it.
