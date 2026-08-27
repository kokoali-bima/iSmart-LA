# Changelog

## v0.2b.1 -- group admins can manage schedules, credit hardened, stale header fixed

**A registered group's own admin can now install and remove scheduled tasks**, not
just the bot owner. Scheduling already carried the same two safety nets `/addserver`
relies on -- the proposal is always shown first, and a PIN confirms a human is
actually approving it -- so there was no reason to trust it less. The real blocker
was one level deeper than it looked: the PIN keypad's own "who may drive this"
check was hardcoded to the literal owner for every action, regardless of what the
per-action allow-list said about *where* it could be confirmed. A non-owner group
admin could never complete any PIN flow at all, even ones nominally open to a group.
`/unlock` stays owner-and-DM-only on purpose -- it opens write access to whatever the
agent decides to touch next, which needs the strictest gate available.

**Credit line hardened against accidental removal.** Not technically unremovable --
anyone with full source access can always delete a string in code they control --
but now redundant across `LICENSE`, a "do not remove" header at the top of
`lite_agent.py`, and a marker comment directly above the credits string itself, so
stripping it requires a deliberate act across several files rather than one
unnoticed edit.

**Fixed:** production's own module docstring was still the original pre-rename
text ("Lite Agent... routed through 9Router") -- never resynced through the agy
integration, the rename to iSmart-LA, or dropping 9Router entirely, even though
every functional change had been kept in sync. Replaced with the current, accurate
header; verified byte-for-byte parity between production and the repo afterward.

11/11 tests pass for the group-scheduling permission matrix (owner anywhere,
registered-group admin, unregistered-group admin, group member, DM stranger --
each checked against both the proposal gate and the keypad gate).


## v0.2b.0 -- a real security boundary, self-service onboarding, no more 9Router

The biggest change since initial packaging. Driven by dropping a dependency that
turned out to be unnecessary, and by a long live-fire test against the real
cluster that found (and fixed) several bugs no synthetic test would have caught.

**9Router is gone.** Claude Code signs in to a Claude Pro/Max subscription directly
(`claude auth login`) -- verified end to end, not assumed. That removes Node.js, npm,
pm2, a second service to keep alive, and a separate dashboard login. The gateway path
still works for anyone who wants it (set `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`),
it's just no longer required. The 4-tier fallback chain (Gemini Flash -> Pro-low ->
Claude Haiku -> Sonnet) is expressed as one `TIERS` setting now, tried briefly as
freely configurable, then locked back to those four on request -- this deployment's
own needs, not a general knob.

**`/start` replaces manual setup.** The installer's job shrinks to system deps, a
venv, both CLIs, and a systemd service. Everything needing a browser or a human
decision -- signing in to Gemini, signing in to Claude, setting the PIN -- happens in
a Telegram wizard instead: a URL to open, a code to paste back (never a password or a
key), stoppable and resumable. Groups get a self-service path too, gated to the owner
and that group's own admins.

**A real security boundary, not a polite request.** The agent now runs read-only by
default: an SSH key whose `authorized_keys` entry forces a guard script that
allowlists read-only verbs and refuses everything else (`pve-ro-guard` for Proxmox
nodes, `vm-guest-guard` for generic VMs) -- enforced by sshd, not by asking the model
nicely. `/unlock [minutes]` opens a second, unrestricted key for a bounded window,
gated by a 6-digit PIN entered on an inline keypad -- the digits ride in
`callback_data` and never become a chat message, so they're safe to enter even in a
group. Changing an existing PIN always requires the current one first, so a stolen
Telegram session can't quietly replace it.

**The agent asks -- it doesn't tell the user to type a command.** Blocked by
read-only mode, it describes the needed change and emits a line the bot recognises,
rather than attempting something already known to fail. That surfaces an inline card:
snapshot-then-unlock or unlock alone (the snapshot button is skipped entirely for a
plain reboot/start/stop -- nothing a power-cycle could touch would need rolling back),
confirmed by the same PIN keypad. Once open, the SAME conversation continues
automatically with a short "go ahead" nudge -- not a re-ask, so the model doesn't
re-investigate or re-explain what it already found.

**Server inventory: `/addserver`, `/servers`, `/removeserver`.** Registers a machine
through a step-by-step chat flow -- kind, address, user, port -- and shows only the
agent's own **public** key with the one-line command to install it. No private key or
password ever passes through Telegram. Proxmox targets get an optional read-only
cluster scan (which nodes, how many guests) folded straight into the learned zone.

**Scheduled tasks under the same visibility rule: `/schedules`, `/unschedule`,
`/adopt`.** The agent proposes a schedule, a human confirms with the PIN before
anything is installed, and only a clearly-marked block of the crontab is ever
touched -- an operator's own unrelated cron entries are left alone. `/adopt` brings a
pre-existing entry (installed before this feature existed) under the same management.

**Hard boundaries are editable from chat now: `/boundaries`, `/addboundary`,
`/rmboundary`.** Previously only settable once, at install time, by hand-editing a
file over SSH -- which meant the most safety-critical setting in the system was also
the hardest to keep current. Adding one is unrestricted (it only narrows the agent);
removing one needs the PIN, since that's the direction worth slowing down.

**`/agentstatus`:** a real, parallel liveness probe of every configured tier --
distinct from `/providers`, which only reflects what recent real usage happened to
reveal. Green/red per tier, a sliver of quota rather than a full turn, feeding the
same cooldown table `/providers` reads.

**Fixes**, several found live against the real cluster during today's testing:
- Chunking could split a code block or table across two messages, breaking the
  formatting entirely; it now tracks structure and keeps them intact.
- Secrets (API keys, tokens) are now redacted at the point every outbound message is
  built, not left to each caller to remember; facts learned from an untrusted origin
  (a group message) are no longer written to the permanent brief.
- Hand-built HTML (server lists, schedule cards, boundary lists) was being escaped by
  the same converter meant for untrusted model Markdown, so the tags showed up
  literally instead of rendering.
- `/adopt` left the original raw cron line in place after adopting it, so the
  adopted job would have run twice.
- Three bugs found in sequence during one live test of the new unlock flow: the
  read-only notice told the model to tell the user to run `/unlock` themselves
  (bypassing the whole card+PIN+snapshot flow entirely); once fixed, the card's
  trigger condition still required an actual guard refusal that the model was now
  correctly avoiding causing; once fixed, a `context` parameter was missing one level
  up the call chain, crashing the very last step of a completed PIN entry with a
  `NameError`. All three shipped within the same short debugging session, each
  caught by testing the actual live flow rather than a synthetic case.


## v0.1b.1 -- self-configuring deployments, formatted replies

Driven by a production audit plus real usage on a live cluster.

**Deployment is no longer hand-configured.** `bootstrap.py` asks a few plain-language
questions at install time and writes both environment briefs, so a new server needs no
hand-authored config. Nothing in the core is Proxmox-specific any more -- Kubernetes,
a server fleet, or a CI estate work the same way (verified end-to-end against a
Kubernetes scenario).

**The brief now maintains itself, within bounds.** SOUL.md/GEMINI.md are split by a
`<!-- LEARNED_ZONE -->` marker: everything above it is human-only, everything below is
appended automatically when the agent verifies something durable via a `LEARN:` line.
The model gets no write access to these files at all -- it supplies fact text, the bot
decides placement, so the zone holding the hard boundaries is protected by code rather
than by asking the model nicely. Capped at 60 entries, deduped, reported in chat, and
reversible with `/forget`. New: `/learned`, `/forget <n>`.

**Replies are formatted.** Model Markdown is converted to the HTML subset Telegram
supports (headings->bold, `-`->•, tables->`<pre>`, rules->a line), escaped first, code
spans protected, chunked on line boundaries so tags never split across messages, with a
plain-text fallback if Telegram still rejects the entities. Reports previously arrived
showing their raw Markdown source.

**Fixes:**
- GEMINI.md is now injected into the prompt explicitly rather than relying on agy's
  undocumented working-directory auto-load, which demonstrably was not being honoured.
- Long/slow document uploads no longer report a false "failed to send" (explicit
  120s upload timeouts; the default ~5s tripped on multi-MB files that had in fact
  arrived).
- A network blip during delivery no longer silently swallows a completed answer --
  delivery is retried once, then reported.
- Image exports: PNG forbidden outright (full-page PNGs were landing at 59-89MB, over
  Telegram's limit), replaced by a real 1920x1080 slide-deck format, zipped past ~3
  files.


## v0.1b.0 -- initial packaging

First packaged release, extracted from an internal deployment against a production
Proxmox VE cluster after several days of iterative fixes. Beta: functional and useful,
some rough edges remain (see README "Known limitations").

Highlights:
- 4-tier fallback: agy (Gemini Flash -> Gemini Pro-low) -> Claude Code via 9Router
  (Haiku -> Sonnet), each tier explicit and independently trackable.
- Per-backend reply tag (`mini` / `mini pro` / `dede iku` / `dede nnet`) for at-a-glance
  health monitoring.
- Named sessions, per-tier session isolation (cross-tier resume was found to multiply
  token cost several times over in testing -- never allowed).
- Manual-only memory (`/remember`, `MEMORY.md`) -- deliberately no automatic write path.
- "Graduated skill" pattern (`/graduate`, `tools/`, `/status`, `/tools`) for turning
  repeated question classes into zero-token scripts.
- File delivery: detects both the `MEDIA:<path>` convention (Claude Code) and raw
  `file://` links (agy's actual habit), with content-hash dedup so the same report
  linked at multiple paths is only sent once.
- Telegram group support: `/registergroup` / `/unregistergroup` (admin-only,
  self-service, no restart needed), `/chatid` for setup.
- Interactive installer (`install.sh`) covering system deps, both CLIs, 9Router
  (existing-or-fresh), and a systemd service.
