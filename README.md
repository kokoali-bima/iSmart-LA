# iSmart-LA (Lite Agent)

A lightweight Telegram bridge to **Claude Code** and **Antigravity CLI (agy)**, for
infrastructure monitoring and investigation -- built to be dramatically cheaper to run
than a full agent framework, while staying just as capable for real operational work.

> **Status: v0.2b.33 -- early/beta.** Built and battle-tested against a real production
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

**Connecting an account is a one-time step done directly on the host, not through
Telegram** — the same reasoning as the SSH keypair: an OAuth flow needs a real
browser, and this is infrequent enough that a guided chat wizard isn't worth
building. Uses [rclone](https://rclone.org/drive/) with `scope=drive.file`, so a
connected account only ever exposes files rclone itself creates — never its
existing Drive contents.

```bash
# on any machine with a browser (does not have to be the server):
rclone authorize "drive" --drive-scope drive.file
# paste the resulting {"access_token": ...} JSON into a file on the server, then --
# pick a remote name: "gdrive" for the first account, "gdrive_<something>" for
# every one after that (e.g. gdrive_company, gdrive_clienta) -- that naming
# convention IS how the bot discovers which accounts exist, nothing else to register:
python3 -c '
from pathlib import Path
import json, sys
name = "gdrive"  # or gdrive_<something> for a second/third/... account
token = Path("/tmp/gdrive_token.json").read_text().strip()
json.loads(token)  # sanity-check
conf = Path.home() / ".config/rclone/rclone.conf"
conf.parent.mkdir(parents=True, exist_ok=True)
with conf.open("a") as f:
    f.write(f"\n[{name}]\ntype = drive\nscope = drive.file\ntoken = {token}\n")
conf.chmod(0o600)
'
# IMPORTANT: check for an existing root folder before creating one -- if this
# remote turns out to be the SAME underlying Google account as one already
# connected (a typo'd name, or re-authorizing the same account by mistake),
# `mkdir` would silently create a SECOND "iSmart-LA Data" folder side by side
# with the first, and nothing would notice until files start landing in the
# wrong one:
rclone lsd gdrive: | grep "iSmart-LA Data" || rclone mkdir "gdrive:iSmart-LA Data"
```

Once at least one account is connected, `/gdrive` in any chat shows a picker —
tap one to make it that chat's default. **With exactly one account connected,
every chat uses it automatically** — no ambiguity to ask about, so there's nothing
to pick. The moment a *second* account is connected, any chat that hasn't
explicitly chosen yet must pick one via `/gdrive` before uploading — deliberately
no silent fallback to "whichever account connected first" once there's a real
choice, since that's exactly the kind of default that sends a file to the wrong
place unnoticed. A chat that was already auto-using the sole account keeps working
unchanged; only chats that never uploaded anything are asked to choose. Adding a
further account is still the host-side step above; `/gdrive` only lets a chat
choose among accounts that already exist.

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
bot only ever *sees* a command, an `@botname` mention, or a reply to one of its own
messages -- Telegram's **Privacy Mode**, ON by default for every new bot. Everyday
group chatter stays invisible to it, and invisible costs nothing: **this is what
you want for most groups**, so the bot answers when asked and stays out of the
conversation otherwise. Turning Privacy Mode off (**@BotFather** → `/mybots` →
this bot → *Bot Settings* → *Group Privacy* → *Turn off*) makes it read and
reply to every message like a full participant -- worth it only for a group that
actually wants that, since every message it then answers spends this
deployment's shared subscription quota.

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
