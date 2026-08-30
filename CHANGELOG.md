# Changelog

## v0.2b.17 -- tools/registry.json was runtime data tracked as if it were source

Found while converting VM175 to a git checkout for /update: its live
`tools/registry.json` held a real graduated skill (`cluster-snapshot`, created
by an actual `/graduate` on production use), while the repo tracked the same
path as an empty starter template. Same class of bug as v0.2b.16 -- runtime
state living where git could touch it -- except this one is worse in kind: it
was already committed, so any `/update` on a deployment that had graduated a
skill risked a merge conflict (git protects against overwriting local changes,
so worst case is a failed update, not silent data loss -- but "your graduated
skills can block updating" is still a real problem).

`tools/registry.json` is now gitignored, like the other runtime json files.
The starter shape lives at `tools/registry.json.example` instead, referenced
from `/graduate`'s own instructions to the model (create the file from the
example if it does not exist yet). `tools/list_tools.py` already treated a
missing registry.json as empty, so no code change was needed there.

241/241 tests (all nine suites) re-run with no regressions.


## v0.2b.16 -- runtime state was not gitignored, including pin.json

Found immediately after shipping `/update`, by asking what a clone-based
deployment's `git status` would actually look like. Ten runtime files were
untracked but not ignored -- so they showed up as noise, and a single
`git add -A` on the server would have committed them. `pin.json` is the one
that matters: it holds the PIN's salt and hash.

Now ignored: `pin.json`, `servers.json`, `schedules.json`, `snapshots.json`,
`setup_state.json`, `model_overrides.json`, `chat_language.json`,
`gdrive_room_accounts.json`, `update_state.json`, `update_announce.json`, and
`*.log`. Verified that none of them were ever actually committed -- the gap was
closed before it was hit.

This only became reachable when deployments started being git clones, which
`/update` requires; the older copy-the-files deployments had no repository to
commit into.


## v0.2b.15 -- `/update`: check GitHub, see what changed, install it from chat

`/update` shows the running version against the repository's, lists the commits
in between, and after a PIN fast-forwards and restarts. A new version also
announces itself: checked when you send a message, at most once every six hours,
never on a timer, and never twice for the same version -- bounded automation with
a real trigger, not the background poller this project refuses to have.

The refusals are the interesting part, and each has a test:

- **Not a git clone?** It says so and stops. A deployment installed by copying
  files has no remote; guessing would be worse than admitting it. (VM175 is
  exactly this case today.)
- **Local commits?** It reports the divergence and changes nothing rather than
  fast-forwarding over somebody's deliberate work.
- **New build does not compile?** Checked before the restart, and the checkout
  is reset to the previous commit if it fails -- otherwise systemd restarts the
  service into the same crash every five seconds. Verified by pushing a
  deliberately broken commit and confirming HEAD came back untouched.

PIN-gated, because this replaces the code the bot itself runs -- a strictly
larger capability than `/unlock`, which only widens an SSH credential for
minutes. And the confirmation is sent by the process that comes *back*, not the
one about to die: a message sent before restarting proves nothing about whether
the new version starts.

Bilingual from the first commit rather than retrofitted, per the lesson of the
v0.2b.6-10 series.

31/31 new tests, run against real git repositories built per case -- nothing
mocked at the git layer, so fast-forward, up-to-date, diverged, broken-build and
not-a-checkout all behave as git actually behaves. All eight earlier suites
(246 tests) re-run with no regressions.


## v0.2b.14 -- the installer asks two questions; /addboundary explains itself

**Install is down to two questions: the bot token and your Telegram user ID.**
Everything else moved to where the operator already is. Removed from the
terminal: the wkhtmltopdf prompt (now just installed with the other packages --
the brief tells the agent to deliver PDF/JPEG reports, so a missing renderer is
a broken feature, not a preference), the Antigravity sign-in prompt (`/start`
does both sign-ins properly), and the whole `bootstrap.py` step.

That last one was the real blocker. bootstrap.py asked five questions, but four
already had dedicated Telegram commands -- access is `/addserver`, boundaries
and "anything else needing approval" are `/addboundary`, quirks are just
`LEARN:` facts. Only "what should this agent look after?" had no home in chat,
which is the sole reason an interactive Python script still had to run during
install. So it became the fourth item on the `/start` setup card, alongside the
two sign-ins and the PIN, plus a `/setbrief` command for people who would
rather type than tap.

**`/addboundary` now explains what a hard boundary is.** It was one line and one
example, which assumed the reader already knew the term -- for the single most
safety-relevant thing a non-technical operator can set. It now says what it
means, why it exists (the agent has real shell and SSH access; this is the short
list where a misunderstanding becomes an incident), that the words are copied
into the brief verbatim and the agent can never edit them, four concrete
examples, and why "be careful with production" is worse than useless. Same for
the empty state of `/boundaries`, which is where most people meet the idea first.

**Fixed while testing the above:** `set_brief_role()` only replaced the template
placeholder, so it worked once and then silently did nothing -- while the setup
card cheerfully offers "Change" on a completed item. It now rewrites the role
wherever it already sits.

**Also fixed:** `curl | bash` for both CLI installers inherited the installer's
stdin and read ahead over it, swallowing the answers to the prompts below. Under
`set -e` the script then died at the token prompt without printing anything.
Both installers are now fetched to a temp file and run with stdin closed. (The
first attempt at this redirected stdin on the pipe itself, which hands bash an
empty script -- `curl | bash` passes the script *through* stdin. Caught in the
container test.)

Verified end to end in a clean Ubuntu 24.04 container: two answers, seven steps,
zero bootstrap questions, and a `.env` carrying both `AGY_BIN` and `CLAUDE_BIN`.
36/36 new tests for the brief and boundary work; all seven earlier suites
(210 tests) re-run with no regressions.


## v0.2b.13 -- both Claude tiers could never launch under systemd

The service unit gets systemd's minimal PATH, which does not include
~/.local/bin -- where both CLI installers put their binaries. `AGY_BIN`
survived only because install.sh already recorded it absolutely; `CLAUDE_BIN`
falls back to the bare string "claude", which resolves in an interactive shell
and never under systemd. So on a by-the-book install, tiers 3 and 4 of the
fallback chain could not start at all, and only after both Gemini tiers had
already failed would anyone find out.

Caught on the new server before starting the service, with
`systemd-run --pipe /bin/sh -c 'command -v claude'` returning NOTFOUND.
install.sh now resolves claude's real path and records `CLAUDE_BIN=<absolute>`
alongside `AGY_BIN`, and the unit template sets `PATH` explicitly so anything
the agent shells out to by bare name resolves the way it does interactively.


## v0.2b.12 -- fresh installs shipped a brief missing three whole features

bootstrap.py generated the environment brief from its OWN embedded template,
which was never updated as features landed. `SOUL.md.template` had all five
agent conventions; the generated brief had two. Every install that ran
bootstrap -- the normal path, since install.sh called it -- produced an agent
that could not use `NEEDS_WRITE:` (so the read-only wall never triggers and the
unlock card never appears), `SCHEDULE:` (no scheduled tasks) or `GDRIVE:` (no
Drive uploads). The correct template was reached only in the FAILURE path, when
bootstrap was skipped and install.sh fell back to copying it; success produced
the worse artifact.

Separately, in both templates the whole "When you are blocked by read-only
mode" section sat BELOW the LEARNED_ZONE marker -- a zone `append_learned()`
and `cmd_forget()` rewrite, keeping only lines starting with "- " and
discarding everything else. Proven by running the real `append_learned()`
against the pre-fix template: one call, and the NEEDS_WRITE instruction was
gone from both briefs. Moved into the protected half.

`examples/proxmox/*.example` have no marker at all, which is fine and
documented: `_split_zones()` treats a marker-less brief as entirely protected.


## v0.2b.11 -- installer fixed: it still installed 9Router and wrote broken model IDs

Found by actually deploying to a fresh server following our own documented
Quickstart, rather than by reading the code. Three things the v0.2b.0 9Router
removal never reached:

- **`install.sh` step 4/8 still installed 9Router**, pulling in Node.js via
  NodeSource, npm and pm2 with it, and then asked for a gateway API key -- all
  mandatory, with no skip path. Exactly the four dependencies v0.2b.0 claimed
  to have removed. Step deleted; the installer is 7 steps now.
- **The `.env` it wrote was functionally broken**: `CLAUDE_MODEL_PRIMARY=cc/claude-haiku-4-5-20251001`
  and a `TIERS` line with the same `cc/` prefixes. That prefix is 9Router's own
  provider tag -- against Claude Code's direct sign-in a `cc/`-prefixed model ID
  simply 404s. A fresh install following the README would have produced a bot
  whose Claude tiers could never answer. It also wrote `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_API_KEY` unconditionally, which is what *switches* the bot to
  gateway mode. Now writes a minimal `.env` and lets the (correct) code defaults
  stand.
- **`.env.example` had both bugs too**, defaulting `ANTHROPIC_BASE_URL` to
  9Router's `http://127.0.0.1:20128` and the same `cc/` model IDs.

Separately, `.env.example` claimed to document "every setting" while missing ten
of them -- including `TIERS`, the single most important knob (`/providers` tells
you to edit it) and the entire write-mode family (`SSH_*_KEY`,
`WRITE_MODE_*_MINUTES`), plus `DEFAULT_LANGUAGE`, `GDRIVE_ROOT`, `CLAUDE_BIN`
and `RCLONE_BIN`. All 24 settings the code actually reads are now documented,
verified by diffing `os.environ.get(...)` against the file.


## v0.2b.10 -- audit sweep closes six gaps the per-command migration missed

Requested verification pass after v0.2b.9 claimed the `/lang` migration
complete: wrote a sweep script that scans every `reply_text` /
`edit_message_text` / `answer` call in the file and flags whichever ones
aren't routed through `_t(lang, ...)`. It found six genuine gaps the
command-by-command passes had walked past:

- **`_send_media_file` was never touched at all** across the whole series --
  every MEDIA: delivery error (not found, too large, contains a credential,
  upload failed) was still hardcoded Indonesian in production, English in
  repo. The single largest miss, since it fires on the same delivery path as
  every report the agent hands back.
- `cmd_graduate`'s exception handler -- previously left alone on the
  reasoning "identical in both files already," which doesn't actually mean
  it was translated, just that neither copy had been.
- `cmd_start_lang_button`'s permission-denial message.
- `_handle_server_input`'s SSH-key-preparation exception.
- **`_run_turn` itself** -- the combo-run-failure error, the "(no response)"
  fallback, the LEARN: confirmation notice, and the delivery-retry-failure
  message. This is the core per-turn handler underlying every single AI
  conversation, so despite being high-traffic it had simply never come up in
  any of the command-scoped migration batches.
- The global `on_error` handler (the last-resort catch-all when something
  raises an uncaught exception).

Audit sweep script now lives at `dev/audit_lang.py`, kept for reuse if
`/lang` coverage ever needs re-checking after future changes -- it's
read-only, makes no edits. 12/12 new tests pass for the six fixes; all six
earlier language/feature suites (162 tests total) re-verified with no
regressions. Deployed and restarted with 0 errors.


## v0.2b.9 -- `/lang` migration complete: every command and wizard

Final follow-up to the v0.2b.6-8 series: migrated the last two pieces --
`/start`'s own setup wizard (`_wizard_text`/`_wizard_keyboard` now take a
`lang` argument, `cmd_start`, `cmd_start_lang_button`, `cmd_setup_button`,
`_begin_cli_login`, `_handle_wizard_input`) and `/addserver`'s step-by-step
form (`cmd_addserver`, `_begin_addserver`, `cmd_server_button`, `_srv_prompt`,
`_handle_server_input`, `_finish_addserver`).

**Every command and flow now respects `/lang`.** Nothing in the bot's own
fixed text is locked to one language any more -- what started as an
infrastructure-plus-a-few-commands change in v0.2b.6 reached full coverage
after four incremental batches, each tested and deployed on its own before
moving to the next.

33/33 new tests pass (wizard steps and confirmation prompts checked in both
languages, `_srv_prompt`'s EN/ID dictionaries checked for actually differing
per step); all five earlier language/feature suites re-verified with no
regressions across the whole series. Deployed and restarted with 0 errors.


## v0.2b.8 -- `/lang` migration reaches the PIN system and confirmation cards (37 total)

Follow-up to v0.2b.7: migrated the PIN system itself -- `request_pin`,
`cmd_pin_key` (the keypad handler), `_pin_capture`, `_pin_verified` (every
action branch: rmboundary, schedule_install, unlock), `_begin_new_pin`,
`/setpin` -- plus the schedule and write-mode confirmation cards:
`offer_schedules`, `cmd_schedule_decision`, `/adopt`, `offer_unlock`,
`cmd_needwrite_button`, `_do_unlock_and_resume`. 37 commands total now
respect `/lang`; what's left is specifically `/start`'s own setup wizard and
`/addserver`'s wizard.

This was deliberately its own batch rather than folded into v0.2b.7: the PIN
system is what every other sensitive action (`/unlock`, `/rmboundary`,
`/addserver`, scheduling) confirms through, so getting it right mattered more
than moving fast through it.

22/22 new tests pass (PIN flows and confirmation cards checked in both
languages); all four earlier language/feature suites re-verified with no
regressions. Deployed and restarted with 0 errors.


## v0.2b.7 -- `/lang` migration extended to 21 more commands (29 total)

Follow-up to v0.2b.6: migrated the rest of the single-message commands to the
`_t(lang, en, id)` pattern -- sessions/memory (`/new`, `/session`, `/sessions`,
`/remember`, `/memory`), utility/admin (`/tools`, `/graduate`, `/chatid`,
`/registergroup`, `/unregistergroup`, `/cancel`), and registry-listing
(`/learned`, `/forget`, `/servers`, `/removeserver`, `/boundaries`,
`/addboundary`, `/rmboundary`, `/snapshots`, `/schedules`, `/unschedule`). 29
commands total now respect `/lang`; what's left is specifically the multi-step
flows (`/start`'s wizard, `/setpin` + the PIN keypad, `/addserver`'s wizard,
`/adopt`, the schedule/unlock confirmation cards) -- deliberately deferred since
they touch more shared state and are riskier to migrate carelessly.

42/42 new tests pass (each migrated command checked in both languages); the
existing `/usemodel`, Google Drive, and v0.2b.6 language suites re-verified
with no regressions. Deployed and restarted with 0 errors -- interrupted partway
through by an unrelated ~10-minute network outage to the host (ping/SSH both
timed out; the VM itself never rebooted and `lite-agent.service` stayed active
throughout with zero log entries, so the running bot was very likely unaffected
-- the outage only blocked *this* deploy from reaching the host, not the host
serving live traffic).


## v0.2b.6 -- per-chat language (`/lang`), Google Drive auto-picks a sole account

**`/lang` (or `/language`) sets which language the bot's own fixed text replies
in, per chat** -- English or Indonesian, independent of `/help`'s own separate
EN/ID choice and independent of the agent's actual answers, which already mirror
whatever language a prompt is written in on their own. A new chat gets asked once,
the first time `/start` shows its setup card; an already-set-up deployment keeps
replying in `DEFAULT_LANGUAGE` (`id` unless set otherwise) until a chat picks
explicitly. Migrated the 8 commands with the most hardcoded text so far --
`/usemodel`, `/gdrive` (+ its picker button), `/mode`, `/providers`,
`/agentstatus`, `/status`, `/unlock`, `/lock` -- via a small `_t(lang, en, id)`
helper; the rest still reply in whatever language their source currently has
(mostly Indonesian in this deployment). This also fully collapses those 8
functions' old repo=English/production=Indonesian fork into one shared,
bilingual source -- both copies are now byte-identical there (docstrings aside).

**Google Drive: with exactly one account connected, every chat uses it
automatically** -- no ambiguity to ask about, so `/gdrive`'s picker only becomes
mandatory the moment a *second* account is connected. A chat that was already
auto-using the sole account keeps working unchanged when a second account
appears; only chats that never uploaded anything get asked to choose.

**Also fixed:** `/help`'s `/gdrive` line still described the pre-multi-account
single-status version -- missed when `cmd_gdrive` itself was rewritten for
v0.2b.5's picker. Synced in both languages, in both repo and production copies.

19/19 new tests pass for the language system (gating, persistence, the
confirmation message's own language, migrated commands actually flipping output);
the existing `/usemodel` (20/20) and Google Drive (26/26, two new cases added for
the auto-default behavior) suites re-verified with no regressions.


## v0.2b.5 -- multiple Drive accounts, per-room default, group auto-scoping; /help fully synced

**More than one Google Drive account can be connected now**, each its own rclone
remote (`gdrive`, `gdrive_company`, `gdrive_clienta`, ...) -- still a one-time
host-side step, just repeatable. `/gdrive` in any chat now shows a picker instead of
a plain status line; tap one to make it that chat's default. **A chat must
explicitly pick an account before anything uploads there** -- no silent fallback to
"whichever account connected first," since that's exactly the kind of default that
sends a file to the wrong place unnoticed. `/gdrive` is now gated like `/usemodel`
(owner anywhere, or a registered group's own admin) since it's a room-wide setting,
not a per-person one.

**Uploads from a group land inside that group's own subfolder automatically** --
`iSmart-LA Data/<group name>/...` -- without the agent needing to know or add the
group's name itself. A path starting with `/` asks for the shared root instead, but
that escape only actually works for the room's own admin (or the owner); anyone
else's attempt is quietly kept inside the group's folder rather than refused
outright -- same precedent as an untrusted fact from a group being quietly not
remembered rather than erroring. This matters most when several groups share the
*same* connected account (e.g. one company account across multiple client rooms) --
the folder split is a convenience default, not a hard permission boundary Google
itself enforces, so the escape hatch is deliberately not open to everyone.

README's setup instructions now check for an existing root folder before creating
one -- connecting a second remote that turns out to be the *same* underlying Google
account (a typo'd name, or re-authorizing the same account by mistake) would
otherwise silently create a second "iSmart-LA Data" folder side by side with the
first.

**Also: `/help` fully synced across all four copies** (repo EN/ID, production
EN/ID). Audit turned up three independent drifts: repo's own Indonesian copy was
missing ~12 commands (the same gap fixed for production's copy back in v0.2b.3,
never applied to repo's own); production's English copy had the identical gap in
the other direction, never fixed either way; and all four copies still described
`/unlock` as "owner-only, DM-only", stale since v0.2b.2. Production's title also
still read "Lite Agent" in both languages, pre-dating the rename -- fixed to
"iSmart-LA" in both. (Command *replies* themselves -- `/usemodel`, `/gdrive`, `/mode`,
etc -- are still each deployment's fixed language, not switched at runtime; only
`/help` offers an explicit EN/ID choice. Making every reply follow a per-chat
language preference was considered and deliberately deferred -- a much larger
change than fits alongside everything else here.)

25/25 tests pass for the multi-account behavior: account discovery from
`rclone.conf`, per-room storage, group-folder scoping and its admin-only escape
hatch (including the silent-confinement case for a non-admin), and the picker's
permission gating. Verified end-to-end against the real connected account: refused
before a room picked one, uploaded successfully once it did.


## v0.2b.4 -- Google Drive delivery

**The agent can now save a file straight to Google Drive**, not just send it through
Telegram. Mention "gdrive" (or Google Drive) and where you want it in a normal
message -- the agent creates the file, then emits `GDRIVE: file=<path> | to=<relative
path>`, which the bot picks up: uploads via [rclone](https://rclone.org/drive/)
(creating any missing subfolder automatically -- nothing needs pre-registering),
replies with a shareable link, and strips the marker line from what you see. New
`/gdrive` (0 tokens) reports whether it's connected and where files land.

Connecting the account is a **one-time step done directly on the host, not through
Telegram** -- same reasoning as the SSH keypair setup: OAuth needs a real browser,
and this is infrequent enough that a guided chat wizard isn't worth building yet.
Uses `scope=drive.file`, so the connected account only ever exposes files rclone
itself creates -- never the rest of that Drive. Same secret-scan gate already used
for Telegram delivery applies here too: a file containing a credential is refused,
not uploaded.

One root folder ("iSmart-LA Data" by default) holds everything; per-request
subfolders (e.g. one per client) are named in the message itself, not configured
ahead of time -- `/usemodel`'s "ask for it by name" shape applied to a different
problem.

Verified with a real end-to-end upload against the actual connected account (not
just mocked) -- file landed in the right nested subfolder, correct shareable link
returned, cleaned up after. 16/16 mocked tests pass otherwise: extraction, the
credential gate, not-configured handling, and rclone's copyto/link subprocess shape.
Also documented (in passing): rclone's shared default OAuth client is being retired
sometime in 2026 and can hit a shared rate limit under load -- noted in the README,
not yet worked around with a dedicated client_id.


## v0.2b.3 -- `/usemodel`: two extra tiers, opt-in only

**`/usemodel` adds Claude Opus and Gemini Pro-high as extra tiers, without touching
the default chain at all.** The 4-tier fallback (Gemini Flash -> Gemini Pro-low ->
Claude Haiku -> Claude Sonnet, cheapest first) stays exactly as it's been since it was
deliberately locked to fixed defaults -- this deployment's own needs, not a general
knob. The two new tiers sit outside that chain entirely: reachable only by asking for
one by name (`/usemodel dede opus`, `/usemodel mini pro max`), for a case that
genuinely needs more than the default chain offers. `/usemodel auto` reverts;
`/usemodel` alone shows what's active. The override is per-chat, persisted, and gated
the same as `/addserver` -- owner anywhere, or a registered group's own admin -- since
picking a heavier tier spends this deployment's own shared subscription quota.

A forced tier is tried first but the default chain still backs it up on failure rather
than hard-erroring -- the `— by ...` tag on every reply already surfaces whenever that
safety net had to fire, so silently falling back is more useful than leaving the user
with nothing.

**Also fixed while in the area:** the Indonesian `/help` text (what's actually shown
to users, since production defaults to it) was missing roughly ten commands that the
English version already listed -- `/schedules` through `/forget` were absent from the
repo's copy, though production's own deployed copy already had the fuller list.
Synced both to the same complete set, and refreshed `/unlock`'s stale "owner-only,
DM-only" line to reflect what v0.2b.2 actually shipped.

Investigated first and explicitly ruled out this round: a `/quota` command to check
remaining Claude/Gemini usage. Neither `claude` nor `agy` exposes real quota or
remaining-usage numbers through any CLI subcommand (`claude auth status` / `agy
models` confirm login and list models, nothing about usage) -- building one would mean
guessing from our own partial usage tracking and presenting it as authoritative, which
isn't worth doing.

20/20 tests pass: extra tiers stay disjoint from the default chain, alias matching,
override persistence, the full owner/group-admin/member/stranger permission matrix,
and that a forced tier's failure genuinely falls through to the default chain rather
than erroring out.


## v0.2b.2 -- group admins can open write mode too, on a shorter leash

**`/unlock` now works for a registered group's own admin**, not just the bot
owner in a DM -- the same trust level `/addserver` and scheduling already
carry. It stayed DM-only through v0.2b.0/v0.2b.1 on purpose: it opens write
access to *any* change the agent decides to make next, not one visible
pre-approved action like a schedule install, so it got the strictest gate
available. Rather than keep it locked out of groups entirely, a window opened
from a group is now capped at **10 minutes** regardless of what's requested
(`/unlock 45` in a group still only opens for 10) -- well under the 60-minute
ceiling a DM gets, and under the 15-minute default too. A DM's own default and
ceiling are unchanged.

This also fixes the same interactive card the read-only wall offers when the
agent hits a wall (`NEEDS_WRITE:`) -- previously that card, its buttons, and
the resulting PIN prompt were DM-owner-only outright, so a group admin got
nothing but the agent's prose when a group-sent prompt needed a change. Now
the card appears and the PIN keypad accepts a registered group's own admin
there too, capped the same way.

`_may_manage_schedules` (the permission check scheduling introduced in
v0.2b.1) is renamed to `_may_authorize_group_action` -- it now gates `/unlock`
as well, so a scheduling-specific name no longer fit what it actually checks.

15/15 tests pass for the extended permission matrix (owner / registered-group
admin / unregistered-group admin / plain member / DM stranger) plus explicit
enforcement that a 45-minute request from a group is actually clamped to 10
minutes while the same request from a DM is not.


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
