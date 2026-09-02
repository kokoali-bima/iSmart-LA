# Changelog

## v0.2b.48 -- the module would not import at all on Python 3.10 or 3.11

Found by an external architecture review, verified here, and it is the kind of
bug that is invisible from any machine new enough to run the code:

    f"{'✅ ' if a == effective else ''}{a}"      # lite_agent.py:5013

A backslash escape INSIDE an f-string expression only became legal in Python
3.12 (PEP 701). On 3.10 and 3.11 that is a SyntaxError at import time -- so
nothing runs at all. Not a degraded feature: no bot, no commands, and
/update's own compile-check would have refused the deploy too.

The versions that matters for: `install.sh` advertises "python3 (3.10+)",
Ubuntu 22.04 ships 3.10, and Debian 12 -- which Proxmox VE 8 is built on --
ships 3.11. Those are the most ordinary hosts imaginable for this bot.

Honest note on verification: this could NOT be reproduced by running it. Every
interpreter reachable from this project is already too new (dev box 3.14, bot
host 3.12.3, Proxmox node 3.13.5), and `ast.parse(..., feature_version=(3,11))`
is no substitute -- it does not emulate the older f-string tokenizer and
happily accepts the broken line. So the fix rests on code inspection plus PEP
701, and the report to the operator said exactly that rather than rounding it
up to "confirmed".

Fixed by binding the literal to a name first, which is valid on every version:

    tick = "✅ "
    f"{tick if a == effective else ''}{a}"

Checked for siblings while in there: this was the ONLY such f-string in the
whole project, and there is no other 3.11-or-3.12-only construct anywhere
(`str.removeprefix` is 3.9, builtin generics are 3.9, and the module already
uses `from __future__ import annotations`). So install.sh's existing "3.10+"
claim becomes TRUE with this one line -- it needed no edit, it needed the code
to catch up to it.

New: `dev/test_py_compat.py`, 7 tests. Rather than asking an interpreter it
cannot have, it walks the AST and reads the source text of every f-string
expression directly -- identical behaviour on every Python version, no matrix
and no CI required to be useful today. Verified both ways: the detector fires
on the pre-fix file (pointing at lite_agent.py:5013 by name) and stays quiet
on the fixed one, and it deliberately does NOT false-positive on a backslash
sitting in the harmless LITERAL half of an f-string. It also cross-checks that
what install.sh advertises is a version the code can actually meet.

Full suite: 301/301 across 18 files.


## v0.2b.47 -- /setownerscope: extra scope, owner-only, DM-only

Asked for directly, after discussing that a scoped-down agent refuses general
questions by design: the team wants ONE shared broadened scope for every
group (a general-purpose assistant, jokes included, not just infrastructure),
plus something EXTRA the owner alone gets -- and confirmed explicitly that the
extra part must apply ONLY in the owner's own DM, never a group, "bukan di
group" even when the owner is the one typing there.

/setscope already covers the first half -- one shared brief, identical
everywhere. This adds the second half as an independent layer rather than a
second brief to keep in sync: `/setownerscope <text>` records extra
instructions in a small new file (`OWNER_SCOPE.md`), injected into the prompt
ONLY when BOTH are true for the message actually being answered -- the sender
is the owner, and the chat is their own private DM.

Checked fresh on every single turn, the same way MEMORY.md and
write_mode_notice() already are -- deliberately NOT tied to whether a
conversation is fresh or resumed (unlike the environment brief, which is only
sent once per conversation). That matters for two failure modes a
once-per-conversation design would have hit: a non-owner continuing the
owner's own resumed DM conversation must not inherit it from history, and the
owner showing up partway through an existing conversation must get it
immediately, not only on a conversation that happens to start after they set
it.

Wired through both backends: agy gets it folded into the prompt text exactly
like the environment brief and MEMORY.md already are (agy has no
--append-system-prompt equivalent); Claude Code CLI gets it combined into the
SAME --append-system-prompt call as MEMORY.md, rather than a second one --
whether the CLI accumulates repeated flags or lets the last one win was never
verified, and combining avoids depending on either answer.

`run_combo()` gained an `owner_dm` parameter threaded through from
`_run_turn_inner`, computed once as `_is_owner(update) and
update.effective_chat.type == "private"` -- the exact condition asked for,
confirmed explicitly by a live test case: the owner speaking in a group does
NOT get the extra scope, only the owner in their own DM does.

New: `dev/test_owner_scope.py`, 28 tests -- storage round-trip, both backends'
prompt building with the gate on and off, `run_combo` threading it through to
whichever tier answers, all four sender/location combinations (owner+DM only
applies; owner+group, other+DM, other+group all correctly don't), and the
full `/setownerscope` command (permission gating, DM-only gating, show
current value, set, clear, clearing twice reports honestly rather than
pretending). Two pre-existing test files mocked `run_combo()` with the OLD
signature and needed their fakes updated to accept the new keyword --
harness-only, no behaviour affected. Full suite: 294/294 across 17 files.


## v0.2b.46 -- editing a sent command used to crash its handler

Asked for a health check across both deployments -- errors today, anything
stuck, any wasted tokens. Found a real, reproducible crash on bscloud, twice
in the same minute:

    File "lite_agent.py", line 4437, in cmd_usemodel
        await update.message.reply_text(_t(lang,
    AttributeError: 'NoneType' object has no attribute 'reply_text'

Root cause: a user edited an already-sent `/usemodel ...` message (fixing a
typo, say). Telegram delivers that as an `edited_message` update, not a
`message` one -- the content lives in `update.edited_message`, and
`update.message` is `None`. python-telegram-bot's `CommandHandler` matches an
edited_message exactly like a fresh one by default (it reads
`effective_message`, which resolves to whichever of the two is present), so
the handler fires anyway and crashes on the very first attribute access.

That same `update.message.reply_text` pattern, with no None-check, appears at
over a hundred call sites in this file -- so the same user action (edit any
already-sent command) could crash any of them, not just `/usemodel`. Patching
`cmd_usemodel` alone would have left the other ~129 exactly as exposed.

Fixed at the source instead: `run_polling(allowed_updates=...)` is now
restricted to exactly the update types this bot has handlers for --
`message` and `callback_query`, confirmed by scanning every registered
handler (`CommandHandler`, `MessageHandler`, `CallbackQueryHandler`; nothing
needs `edited_message`, `chat_member`, polls, or inline queries). Telegram
then never delivers an edited_message update at all, so `update.message` can
no longer be `None` for any command-triggered callback -- one change closes
the whole class of bug rather than each call site individually.

Also investigated as part of the same health check, and NOT a bug: ~841,000
tokens wasted today on bscloud across 7 events where agy reported
`status=SUCCESS` (or `CANCELED`) with an empty response -- real tokens spent,
nothing usable returned. Traced the exact conversation ids through the log:
`_handle_survives()` correctly does NOT recognise "(empty response)" as a
survivable failure, so the resume handle was discarded every time (confirmed:
the very next attempt in the chain always shows `conversation_id=None`), the
tier cooldown from v0.2b.2x-era engaged as designed (a `SKIPPED (cooldown)`
attempt appears in the very next turn each time), and every affected turn
still completed via Claude failover. This is Gemini/agy occasionally
returning nothing despite reporting success -- an external reliability cost,
not a local defect -- and the existing detect/failover/cooldown/log chain is
already handling it correctly; no code change was warranted or made for it.

New: `dev/test_edited_message_crash.py`, 11 tests -- runs with no server
dependency (pure AST inspection of the deployed source, no `telegram` import
needed), verifying the exact `allowed_updates` list, that it excludes
`edited_message` specifically, and that it matches the bot's actual
registered handler types. Full suite: 266/266 across 16 files.


## v0.2b.45 -- connecting a Google Drive account now goes through Telegram

Asked directly: why did gdrive already show a connected account without ever
going through a consent flow the way Gemini/Claude's sign-in does? Checking
confirmed the account itself was genuinely working -- a real upload, link
fetch, and cleanup all succeeded live -- but nothing about HOW it got
connected, by whom, or when was ever visible from Telegram. It was a step
done directly on the host, exactly as documented, which is precisely the
problem being raised: unlike Gemini/Claude, there was no explicit, auditable
moment of consent inside the tool the operator actually uses.

Replicating Gemini/Claude's exact UX -- one link, paste a short code -- turned
out not to be possible, and this was verified live rather than assumed: running
`rclone authorize` directly on the server printed

    please go to the following link: http://127.0.0.1:53682/auth?state=...
    NOTICE: Waiting for code...

a URL pointing at the SERVER's own localhost. Opened from any other device it
simply fails to connect. Google's OAuth for rclone's Drive backend waits for a
network redirect back to a local listener, not a portable code -- a real
platform constraint agy/claude's OOB-style flow does not share.

So the one unavoidable manual step -- running `rclone authorize` once, on a
machine the operator controls with a browser -- stays. Everything that used to
be the risky, error-prone part after that moves into Telegram:

- **`/connectgdrive`** starts the flow, gated the same as `/addserver` (owner
  anywhere, or a registered group's own admin).
- The bot picks a collision-free name (`gdrive` for the first account, asks
  for a short label for a second+ one -- `gdrive_company`, not `gdrive_2`).
- Registers via `rclone config create` -- verified live, with a harmless probe
  remote, not to disturb any existing account's config.
- Checks whether the SAME underlying account already has the shared
  `iSmart-LA Data` root folder before creating one -- the exact duplicate-folder
  risk the old manual README steps could only warn about, now actually
  prevented rather than just documented.
- **Verifies with a real Drive listing before ever reporting success** -- the
  same "prove it, don't assume it" principle the node guard just got in
  v0.2b.43/44. "Connected" now always means an actual API call worked.
- **Rolls back cleanly on any failure** (`rclone config delete`) so a bad paste
  never leaves a half-configured remote lying around to confuse `_list_gdrive_
  accounts()` later.
- Deletes the pasted token from the chat immediately, the same treatment an
  OAuth code gets -- it is a credential, however short-lived.
- Every connect is logged with who did it and when.

Verified twice: the full mocked suite, and separately end to end against the
REAL production Drive account on 10.10.59.40 -- extracted the existing
account's own token, ran it through `connect_gdrive_account()` under a throwaway
name, got a real listing back (which even showed a leftover folder from an
earlier, unrelated live test in this same session, confirming it was really
talking to the same live account), then cleaned up with zero residue.

New: `dev/test_connectgdrive.py`, 32 tests. Full suite: 255/255 across 15 files.


## v0.2b.44 -- securing a host must not lock the agent out of it

Follow-up to v0.2b.43, and a bug in v0.2b.43 itself, found while confirming
what the operator actually wants from this agent: full maintenance capability
-- create, change, delete -- gated by verification rather than removed.

That IS the design (locked by default, /unlock opens an unrestricted key after
a PIN, the original request then resumes on its own), and v0.2b.43 made it
real. But it retired the old unrestricted key inside secure_server(), while
`~/.ssh/config` still pointed at that very key: `_rebuild_ssh_config()` only
ever runs when a server is added or removed, never at startup. So on an
existing deployment the sequence would have been:

  1. the guard installs and verifies -- fine
  2. the legacy key is deleted from the node
  3. ~/.ssh/config is still pointing at the legacy key
  4. the agent can no longer reach the host it just secured

The same gap also made lock/unlock cosmetic there: /unlock swaps the
`agent_active` symlink, and nothing was using it.

Fixed by splitting the steps and ordering them properly. `secure_server()` now
installs and verifies only. `cmd_secure()` then rebuilds `~/.ssh/config` to
point at the active-key symlink -- which is also what finally makes
lock/unlock swap the credential actually in use -- and only after that calls
the new `retire_legacy_key()` per host that verified.

Also worth stating plainly, because "read-only guard" reads like a
restriction and is not one: the guard applies to the read-only key only. After
/unlock the agent holds an unrestricted key and can create, change and delete
normally until the window closes by itself. What the guard removes is the
ability for a *sentence* to be sufficient -- the credential for a change only
exists after a human confirms on the PIN keypad, which no prompt, log line or
group message can reach. `/secure`'s report now says this outright.

The one remaining piece from the incident is also explained by v0.2b.43's
finding: `write_mode_notice()` returns an empty string when the keys are
missing, so the model was never told the protocol at all -- no instruction to
emit NEEDS_WRITE:, no mention of a button. It invented one. With the keys
present it is told, every turn, in whichever mode it is in.

`dev/test_node_guard.py`: 17 -> 21 tests, covering that a fully successful
secure_server() still does NOT retire the old key, and that retire_legacy_key()
is the separate later step that does. Full suite: 223/223 across 14 files.


## v0.2b.43 -- the write gate was never actually installed

A live incident, reproduced deliberately by the operator to test exactly this.
"tolong hapus vps dengan vmid 8006" in a group produced a reply saying "click
the approval button below" -- no button appeared, no PIN was asked, and the
next message caused the VM to be destroyed. The Proxmox task log settles what
happened:

    qmstop     vmid=8006  root@pam  2026-09-01 09:56:53 WIB
    qmdestroy  vmid=8006  root@pam  2026-09-01 09:57:00 WIB

against the bot log:

    09:56:44  running agy: ... prompt_len=37   <- "tombol persetujuan tidak ada terlihat"
    09:57:08  turn done: ... OK

The destroy ran inside that turn. Telling the bot the approval button was
missing was itself enough to make it delete the VM.

Two independent failures, and every one of them silent:

  1. `~/.ssh/agent_readonly` and `agent_write` did not exist, so
     `_keys_configured()` was False, so the ENTIRE approval/PIN gate was inert.
     No button could ever be rendered regardless of what the model emitted.
     The docstring even says it: "If not, this whole mechanism is inert and we
     say so rather than pretending a boundary exists that doesn't" -- but
     nothing said so where an operator would see it.
  2. The node's authorized_keys entry carried no `command=` restriction, so
     the agent's key was ordinary unrestricted root over the whole cluster
     (120 VMs). Proven at the time with a harmless write:

         === HARMLESS write test as the AGENT key ===
         WRITE_SUCCEEDED_NOT_BLOCKED

Root cause of both: they were **manual README steps**, while `/addserver` --
the path the bot itself tells operators to use, and the one `/start` links --
generated ONE unrestricted key and told the user to append it plainly. The
dangerous configuration was the default, and the safe one required finding a
README section the bot never points at. The model-side `NEEDS_WRITE:` marker
was then the only thing left standing, and a model that writes prose instead
of the marker removes even that.

Now, with no manual steps:

- **`ensure_write_mode_keys()`** runs at startup and creates the read-only /
  write pair plus the `agent_active` symlink (pointed at read-only: locked is
  the default). A deployment cloned from GitHub has a live gate on first boot.
  If it cannot, that is logged as an error naming the consequence, not passed
  over.
- **`install_node_guard()`** uploads `pve-ro-guard` to the node and authorises
  the read-only key behind `command=`, over SSH, from the bot. This is what
  made it a step nobody performed: it meant pasting a 7KB script into a
  terminal by hand.
- **`verify_node_guard()`** then attempts a real write with the read-only key
  and requires it to be refused. "Protected" is never reported on trust --
  only on an observed refusal. This is precisely the check whose absence let
  the incident look fine.
- **`secure_server()`** ties them together and retires the legacy unrestricted
  key, but strictly AFTER the replacement verifies, so a failure anywhere
  leaves the operator with working access instead of a locked-out node.
- **`/addserver`** now hands over the WRITE key for the operator to authorise,
  then installs and verifies its own restricted key. If protection cannot be
  established the server is NOT quietly added: it warns, names the
  consequence, and requires an explicit "Add unprotected" tap.
- **`/secure`** re-runs install-and-verify across every configured host, and
  bootstraps through the old unrestricted key when the write key is not
  authorised yet -- so an existing deployment migrates without being rebuilt.

New: `dev/test_node_guard.py`, 17 tests, written around the incident: the gate
being inert before setup and live after, verification failing when the
read-only key can still write, the legacy-key migration path, and the ordering
rule that the old key is never removed on a failed verify. Full suite: 219/219
across 14 files.


## v0.2b.42 -- /graduate can finally see Gemini's history

Asked for directly, after the previous release pointed out that /graduate is
the only command that genuinely REDUCES future token spend -- it turns a
solved case into a script that costs zero model tokens to reuse -- and then
had to add that it usually would not work.

It only ever read the primary Claude session, and admitted as much in its own
refusal:

    "if the last turn was answered by Gemini/'mini', /graduate can't see that
     history -- current limitation, each tier keeps its own history"

Since the default chain answers with Gemini FIRST, the cheapest and most
common path was exactly the one that could not be graduated. The cost-saving
feature was unavailable for most cases, which is backwards.

Verified behaviourally against both versions, same Gemini-only session:

    v0.2b.41  did it graduate? -> NO -- refused
              "Belum ada percakapan Claude (dede iku) di sesi ini..."
    v0.2b.42  routes to the agy tier and makes a real call

Two parts to the fix:

- Each completed turn now records **which tier answered** (`last_model` on the
  session). Each tier keeps its own history, so that is the only reliable
  pointer to where the work actually happened.
- `_graduate_target()` resolves the session to `(provider, model,
  conversation_id)`: the tier that answered last, else any Claude
  conversation, else any agy one. /graduate then runs the same instruction
  through whichever provider that is.

Also here, since it is the same class of problem v0.2b.40 fixed for turns:
/graduate's CLI call now goes through an executor instead of blocking the
event loop, and the reply is tagged `— graduated from <tier>` so it is
visible which history it was built from.

New: `dev/test_graduate.py`, 16 tests -- target resolution in every
precedence order, the Gemini-only case running end to end through the agy
tier with the id stored back on the agy side, a clean refusal when there is
no history at all, and a turn recording `last_model`. Full suite: 202/202
across 13 files.


## v0.2b.41 -- a new chat could inherit another chat's conversation

Found while working on /graduate, and more serious than the thing being
worked on. Sessions were built with `dict(EMPTY_SESSION)` off a module-level
constant:

    EMPTY_SESSION = {"claude": {}, "agy": {}}

`dict()` is a SHALLOW copy, so the two inner dicts were the SAME objects in
every session ever created that way -- and run_combo writes resume handles
straight into them (`sess.setdefault("agy", {})[model] = conversation_id`).
The first chat to answer anything therefore wrote its conversation id into
the shared constant, and every session created afterwards started life
already pointing at it.

Confirmed against the real `get_chat_state`, not reasoned about:

    chatB brand-new session -> {'claude': {}, 'agy': {'m': 'A-CONV', ...}}
    CROSS-CHAT LEAK?        -> True

Two consequences, the second being the one that matters:

  * **/new did not actually start fresh.** It handed back a session still
    pointing at the previous conversation -- quietly defeating "`/new` every
    time the topic changes", the single biggest cost-saving habit this
    project's own README teaches. And because /new then saves, the inherited
    id was persisted, so it outlived the process.
  * **A brand-new chat could resume a DIFFERENT chat's conversation.** In a
    deployment shared between groups -- which this project supports
    deliberately, per-group PINs and all -- that is a context leak between
    tenants, not just a billing surprise.

`EMPTY_SESSION` is now `_empty_session()`, a function returning a fresh
block every call, so there is no shared mutable default left to reintroduce
this. All five copy sites use it.

New: `dev/test_session_isolation.py`, 10 tests -- fresh sessions don't share
inner dicts, a new chat doesn't inherit another chat's ids, /new really
resets both sides, and two named sessions in one chat stay independent. Full
suite: 186/186 across 12 files.


## v0.2b.40 -- one long turn no longer freezes the entire bot

Asked whether the bot could handle several prompts at once, each on a
different tier. Checking turned up something more basic: it could not handle
two prompts at all, and the reason was one missing wrapper.

`_run_turn` called `run_combo` directly. `run_combo` is a plain synchronous
function that shells out to agy/claude and blocks for minutes -- one live turn
on bscloud ran 00:31:11 -> 00:35:46, **four minutes thirty-five seconds**, and
for every second of it the asyncio loop was blocked. Not just that chat: the
bot could not answer /status, could not accept /cancel, could not serve any
other chat or group. `Application.builder()` also left `concurrent_updates`
unset, so python-telegram-bot serialised updates on top of that.

Fourteen other blocking calls in this file already went through
`run_in_executor`. The single longest-running one did not.

Now: `run_combo` runs in an executor, `concurrent_updates(True)` is set, and
two guards bound what that opens up.

- **`_chat_turn_lock`** -- one lock per chat. Turns run in parallel ACROSS
  chats, strictly in order WITHIN a chat. Within a chat that ordering is
  correctness, not politeness: two turns there share one session file and the
  same per-tier conversation ids, so overlapping them would clobber each
  other's resume handles -- the same class of bug v0.2b.39 just fixed by hand.
- **`MAX_CONCURRENT_TURNS`** (default 3, env-overridable) -- a process-wide
  ceiling. Each in-flight turn is a real subprocess, and one agy process was
  measured at ~240MB RSS, so unbounded fan-out is how a busy group becomes an
  OOM. Capping delays turns; it never drops them.

Token cost of all this: **zero**. Same prompts, same cheapest-first ladder,
same conversations -- only the waiting changed.

Deliberately NOT done: pinning a different tier per prompt (mini here, sonnet
there). It reads like load-spreading but bills like the opposite -- it skips
the cheapest-first ladder that makes the chain cheap, and since each tier
keeps its own conversation, it multiplies both brief injections and cold
caches. This deployment's own numbers make the point: a warm conversation
showed `cache_read=1,379,979`, while a cold start re-sends the brief at
`prompt_len=11823`.

New: `dev/test_concurrency.py`, 7 tests -- the loop stays responsive during a
turn (this one FAILS against v0.2b.39, verified), different chats overlap, the
same chat never does and strictly alternates start/end, the cap is a real
ceiling, and capped turns still all complete. Full suite: 176/176 across 11
files.


## v0.2b.39 -- "continue this" was starting over instead of continuing

Reported from a real session: the same task was being re-explained to the
model over and over, at ~12,000 characters of brief each time. The user's own
log said exactly why -- a turn timed out, and the very next message, which
literally said "Lanjutkan yang ini" ("continue this one"), opened a brand new
conversation:

  running agy: model=gemini-3.7-flash-medium conversation_id=5dd5b5ca-... prompt_len=18
  turn done: ... FAILED/failover (agy exited 1: {"conversation_id":"5dd5b5ca-...",
             "status":"ERROR","error":"timeout waiting for response",
             "duration_seconds":207.15738437,"num_turns":2, ...})
  running agy: model=gemini-3.7-flash-medium conversation_id=None prompt_len=11823

`conversation_id=None` and `prompt_len` jumping from 18 to 11823 is the whole
bug in two numbers: the resume handle was gone, so the brief went out again.

Root cause, one line in `run_combo`'s error path, comment and all:

    # Don't try to resume a conversation that just errored.
    (agy_convs if provider == "agy" else claude_sessions)[model] = None

That is right for a conversation the model can no longer load, and wrong for
the case that actually happens most: a **timeout**, where the conversation is
intact and sitting on exactly the work the next message wants continued.
Discarding it there throws away the accumulated context AND pays to re-send
the brief -- the most expensive thing this bot can do.

Now the handle is only dropped when the handle itself is implicated
(`_handle_survives`): timeouts, network drops, rate limits and 5xx keep it;
anything else -- "conversation not found", a malformed id -- still clears it,
so a genuinely dead conversation is not retried forever. Tier cooldowns are
unchanged and still bound the retries either way.

Second, smaller fix in the same path: a run that started FRESH and then failed
had its conversation id ONLY inside the error payload agy returns
(`{"conversation_id": "...", "status": "ERROR", ...}`), so nothing had stored
it yet and the conversation was orphaned along with all the work it had
already done. `_conversation_id_from_error` now picks it up.

New: `dev/test_resume_handle.py`, 13 tests, built on the exact error string
this deployment produced rather than a synthetic one -- including the case
that proves the saving, asserting the resumed turn carries the kept id and
does NOT re-send the brief, and the counter-case that a genuinely fresh
conversation still does. Full suite clean: 169/169 across 10 files.


## v0.2b.38 -- docs: the re-invite step v0.2b.37 needs to work at all

v0.2b.37 shipped the mention gate but not the one operational step that makes
it function, and the omission cost a full live debugging round to rediscover:
**Telegram applies a bot's privacy setting at JOIN time.** Turning Privacy
Mode off in BotFather does nothing for a group the bot is already in -- that
group keeps the old setting indefinitely, silently dropping every mention,
while `getMe` cheerfully reports `can_read_all_group_messages: true`.

Proven rather than assumed: temporary debug logging was added as the very
first statement of `handle_message`, before `_authorized()` and before the
new gate, and a mention sent with Privacy Mode already off produced **zero**
log output -- the message never reached the bot at all, so no in-app gate
could have been responsible. After removing and re-inviting the bot to the
same group, the same kind of mention logged normally and ran a full turn.

Captured live in one continuous session, all five in the same group:

- `"apakah anda kenal @robirama93 @bscloud_agent_bot ?"` -> ran, agy OK
- `"hahaha kita masukin lagi dia"` -> reached the bot, dropped, no model call
- `"...provinsi NTB? @bscloud_agent_bot"` -> ran, agy OK
- `"ngerii mini"` -> reached the bot, dropped, no model call
- `"akhirnya mau dia"` -> reached the bot, dropped, no model call

That is both halves confirmed at once: with Privacy Mode off every message
now genuinely arrives, and only the mentions cost anything. Two designed
behaviours also showed up in the real data unprompted -- `@robirama93` and
`@robi` (other people's mentions) neither woke the bot nor were stripped from
the question, and the bot's own mention was stripped before the model saw it
(110-character message -> `prompt_len=93`, exactly the 18-character
`@bscloud_agent_bot` removed).

Docs only, no behaviour change: README's Group access section and both
/help texts now carry the remove-and-re-invite step, why it is required, and
that `/registergroup` does NOT need re-running afterwards (the chat ID and
any group PIN survive the re-invite).


## v0.2b.37 -- an @mention in a group can now wake the bot up

Asked directly: how to make the bot activate on @mention, not just a reply or
a command -- and, live testing to answer it, found something worth fixing
regardless: a plain "@bscloud_agent_bot" typed mid-sentence in a group never
reached the bot at all, confirmed by tailing the server log in real time
while it was sent. Only slash commands and replies to the bot's own messages
were actually getting through -- Telegram's own Privacy Mode does not treat a
bare mention as an exception, despite it looking like a valid mention in the
client. Live-tested a third case too (a plain unrelated message, no mention,
no reply) to rule out the group having simply gone unregistered -- it had
not; that message also left zero trace.

Getting a real @mention to work at all requires Privacy Mode OFF (BotFather
-> bot -> Group Privacy -> Turn off), which then forwards every group
message to the bot instead of just commands and replies. Added the gate that
used to be Privacy Mode's job back in-app, in `handle_message`, so ordinary
group chatter still never reaches the wizard/server-input capture or the
model: a message only proceeds if it replies to the bot's own message, or
contains a real @mention entity of the bot's username. The @mention itself is
then stripped out of the text before it reaches the model, so a question
reads clean rather than carrying "@bscloud_agent_bot" as part of the prompt.

Entity offsets from Telegram are UTF-16 code-unit based, not Python codepoint
indices -- naive string slicing misaligns whenever a character earlier in the
message (an emoji, say) sits outside the BMP. `_entity_text`/`_strip_entity`
round-trip through UTF-16 to get this right, tested against a message with an
emoji placed deliberately before the mention.

New: `dev/test_group_mention.py`, 16 tests -- entity offset correctness
(including the emoji case), mention detection (the bot's own username vs.
someone else's, a real entity vs. a literal "@name" substring Telegram itself
did not parse as one), reply detection, and the gate end to end through
`handle_message` (plain chatter dropped, a mention of someone else dropped, a
real mention processed with the mention stripped, a reply processed
unconditionally, a private DM never gated). Full suite re-run clean: 156/156
across 9 files.

Requires a manual step per deployment this code change cannot make for you:
Privacy Mode must be turned off in BotFather for @mentions to reach the bot
at all -- replies and commands work either way.


## v0.2b.36 -- a genuinely-accepted sign-in code was reported as rejected

Reported live: the user pasted a code, got "Antigravity (Gemini) tidak
menerima kode itu. Mungkin sudah kedaluwarsa" (didn't accept that code, may
have expired) -- then said directly "tapi udah login" (but it's already
signed in). The code had, in fact, been accepted.

Root cause, found by launching the real `agy` binary (already holding a
valid token) fresh in a scratch tmux session directly on 10.10.63.11: agy's
first launch right after a fresh sign-in does not print any "signed in"
message at all. It lands on a one-time "Choose your color scheme" wizard --
completely unrelated to authentication -- whose own preview pane demonstrates
its styling with literal sample lines: `error: compilation failed` and
`warning: deprecation warning`. Those are exactly two of the words in
`FAILURE_HINTS` (`"invalid", "expired", "failed", "error", "denied"`), and
nothing recognized the wizard screen for what it was, so it matched
FAILURE_HINTS before SUCCESS_HINTS ever got a chance -- reporting a
genuinely-accepted code as rejected, almost instantly, every single time.

The same screen also broke the *other* direction: an already-signed-in agy
launched by "Ganti Gemini" (tapping to re-check/re-link an account that's
already fine) lands on the same wizard instead of a URL, so `already_done()`
didn't recognize it either -- would have shown a confusing "couldn't find a
sign-in URL" after the full 45s timeout, on an account that was never broken.

Fixed by adding `FIRST_RUN_HINTS = ("choose your color scheme",)` and
checking it in both `wait_for_result()` (before FAILURE_HINTS) and
`already_done()` -- reaching that screen is only possible once a code has
already been accepted, so it counts as unambiguous proof of success.

`dev/test_cli_login.py` gained 3 tests built on the real captured screen text
(12 -> 15), including a genuine-failure-screen case to confirm the fix
doesn't swallow real rejections along with the false one. While re-running
the full suite for this fix, found and fixed an unrelated pre-existing gap:
`dev/test_group_pin.py` and `dev/test_setscope.py`'s `fresh_module()` copied
`lite_agent.py` into an isolated scratch dir but never put `tools/` on
`sys.path`, so both suites had been unable to run at all since `cli_login`
became an import of `lite_agent.py` (v0.2b.30) -- no behavior changed, only
the harness's own module resolution. All 8 suites re-run clean: 140/140.


## v0.2b.35 -- /logout could leave /start showing green forever

Reported directly: "seharusnya ketika sudah logout dari gemini/claude status
di menu /start tidak hijau lagi" (the status should stop showing green after
logout) -- and it wasn't, no matter how many times /logout ran. Confirmed on
the live server: `setup_state.json` still had `"agy"` marked done from
hours earlier, while no OAuth token file existed at all.

Root cause: `logout_agy()`/`logout_claude()` only cleared the stale flag
*after* successfully removing a real credential -- if there was nothing to
remove (exactly the state a session that died on its own leaves behind, the
whole reason /logout exists), the function returned "already signed out"
before ever reaching `_unmark_setup()`. Confirmed live, directly: called
`logout_agy()` against the exact broken state and watched it return without
touching the flag.

Both functions now clear the flag unconditionally, before any early return --
whether there was a real credential to remove or not, /logout should always
leave `/start` telling the truth afterward.

18/18 tests in `dev/test_logout.py` (up from 15): two new cases reproducing
the exact reported scenario for each provider (the flag set, no live
credential present) and confirming the flag is gone afterward either way.
All fifteen earlier suites (365 tests) re-run with no regressions; 380
total.


## v0.2b.34 -- the actual root cause: the OAuth URL was silently truncated

The real one, found only after v0.2b.30, v0.2b.32, and v0.2b.33 had each
fixed something genuine but none of them had fixed THIS: the sign-in URL
handed to the user was missing its own tail end. Confirmed by having the
user paste the literal URL they'd tried to open -- it ended mid-word,
`...cloud-platform+https%`, with `state=` (always the last parameter) never
appearing at all.

Root cause, confirmed byte for byte on the live server: agy's OAuth URL
alone runs 500-700+ characters. The tmux pane driving it was 400 columns
wide, so the URL hard-wraps mid-parameter with no continuation marker.
`capture-pane -J` is supposed to rejoin a soft-wrapped line back into one --
it did not, tested explicitly both with and without `-J`, identical
truncation either way. Near-certain reason: agy's TUI draws its bordered
panel via cursor-positioned redraws rather than plain sequential character
output, which is specifically what tmux's own wrap-tracking watches for --
so tmux never marked this line as wrapped in the first place, and had
nothing to rejoin.

Every earlier verification in this saga -- including this project's own --
checked the captured URL contained `"accounts.google.com"` and `"oauth"` as
substrings and called that a pass. A truncated URL contains both. Every one
of those checks would have passed against the exact broken URL a real human
was failing to sign in with.

Fixed at the source: the tmux pane width is now 2000 columns, comfortably
fitting any realistic OAuth URL on one genuine unwrapped line. Verified live
against the real binary with a check that actually matters this time -- the
full URL, ending in a real `state=` value, not a percent-escape cut in half:
704 characters, complete, confirmed identical across two separate live runs.

Also added, as defense in depth for whatever wrap scenario isn't the one just
fixed: `wait_for_url()` now refuses a matched URL that ends mid-percent-escape
(`...%`, `...%3`) rather than trusting it, and keeps polling instead.

12/12 tests in `dev/test_cli_login.py` (up from 9) -- including a rewrite of
the original "reaches the URL" check from a substring match to checking the
URL's complete, unbroken shape, since the substring version was itself the
exact class of false-positive that let this ship undetected three times in a
row. All fifteen earlier suites (365 tests) re-run with no regressions; 377
total.

This entire chain -- v0.2b.30 through v0.2b.34 -- exists because a real user
kept pushing past "should be fixed now" and pasted the actual failing
artifact each time instead of accepting a plausible-sounding explanation.


## v0.2b.33 -- v0.2b.32's own fix caused a WORSE version of the bug it fixed

v0.2b.32's theory (a bare auto-linked URL is fragile) was correct in spirit
but wrong in fix: switching to a real `<a href>` anchor did not solve
anything -- it introduced a new, more damaging failure. Confirmed with the
actual broken URL pasted from a real failed attempt: `redirect_uri` had gone
from `...%3A%2F%2F...` to `...%253A%252F%2F...` -- the literal `%` character
itself got percent-encoded AGAIN (`%` -> `%25`) somewhere between the anchor
being sent and the browser opening it. An already-percent-encoded URL,
percent-encoded a second time, no longer matches any redirect_uri Google has
registered for the client -- hence "Error 400: invalid_request", identical
for three different Google accounts across two account types, something no
amount of trying a different account could ever have fixed.

Both the v0.2b.29-and-earlier bare-auto-link and the v0.2b.32 `<a href>`
approach ultimately hand the exact same string to the same platform
"launch this URL" call when tapped -- that call is where the double-encoding
happened, on at least one real Telegram client, and tapping either
construct hits it.

Sidestepped entirely rather than fixed at its root (which would need
knowing which specific client build does this and why): the URL now goes
inside a `<code>` block, with the instructions changed from "tap to open" to
"copy this, then paste it into a browser yourself". Telegram treats a code
block as literal text to copy, not a link to launch -- it never enters the
vulnerable code path at all. Confirmed live via the real Bot API that the
raw message text Telegram stored contains the URL completely unmodified.

Also a live case study in the value of pushing every self-reported "it's
fixed" claim: v0.2b.30's real fix (a genuine OAuth URL reaching the user at
all) got credited with solving a problem it had only partly solved, and
v0.2b.32 shipped on a plausible-sounding but unverified theory about WHY the
remaining failure was happening. Only pasting the actual failed URL settled
it.

8/8 tests in `dev/test_login_link.py` rewritten for the new shape, including
an explicit regression guard that fails if an `<a href>` construct is ever
reintroduced. All fifteen earlier suites (357 tests, `dev/test_cli_login.py`
run separately as it needs no server) re-run with no regressions; 374 total.


## v0.2b.32 -- the sign-in URL was sent as bare text, not a real link

Follow-up to v0.2b.30's fix: with a real OAuth URL now actually reaching the
chat, sign-in still failed -- Google's own consent screen rejected it
("Error 400: invalid_request" / "Access blocked"), for every account tried
(a Workspace account, then a personal Gmail one, both blocked identically),
never when `agy` is run directly in a real terminal.

The URL was placed as plain text in an HTML-parsed message, relying on
Telegram's own heuristic to detect and correctly link a bare URL in message
text -- fragile for one this long, with this many `&`-separated query
parameters. Replaced with a real `<a href="...">` anchor: the client opens
(and copies) the href attribute directly, with no auto-detection guessing
involved.

Verified live, via the real Bot API: sent the exact new construction with a
realistic OAuth-shaped URL to a real chat, then read back Telegram's own
`text_link` entity -- its `.url` matched the original byte-for-byte.

6/6 new tests (`dev/test_login_link.py`): a real `<a href>` anchor is
present (not a bare URL), the href round-trips through HTML-unescaping to
exactly the original URL, the raw URL never appears unlinked anywhere else
in the message, and the same holds in both languages. All fifteen earlier
suites (357 tests) re-run with no regressions; 363 total.


## v0.2b.31 -- replies contained raw LaTeX Telegram can't render

Reported live, with a screenshot: a weather report came back with
`$44^\circ\text{C}$` printed literally, instead of "44°C". Telegram's legacy
Markdown (what every reply here is sent with) has no math/LaTeX rendering at
all -- the model had no reason not to reach for LaTeX notation for a
temperature, since nothing in its brief said Telegram couldn't display it.

Added a short rule to both brief templates, right after the opening persona
paragraph: no LaTeX/KaTeX syntax, ever, plain text or basic Unicode instead
(44°C, 10x, H2O). Applied directly to the two live deployments' already-generated
briefs too (itbutler, bscloud) -- new installs get it from the template, but
those two were already running before this fix existed, and briefs aren't
regenerated by an update. Anchored the live edit on the `## Environment:`
heading rather than exact line position, since itbutler's brief has been
hand-customized into a different shape than the template's; verified HARD
BOUNDARIES and every LEARN:/NEEDS_WRITE:/SCHEDULE:/GDRIVE:/MEDIA: convention
stayed in the protected half on both.

Takes effect on each chat's next new conversation (/new applies it immediately).


## v0.2b.30 -- the Telegram sign-in flow could report success without ever really signing in

The most serious finding of this project so far. Reported live: "every time
I run /start, Google shows as already logged in" -- immediately, no URL, no
code, nothing to approve. Confirmed directly: a raw `agy` call on that same
deployment returned "authentication required" at the exact same time /start
was reporting success.

Root cause, found by replaying the real tmux flow live on the affected
server: Antigravity's own startup banner is *"Welcome to the Antigravity
CLI. You are currently not signed in."* -- and the code that decides whether
a sign-in is already done (`LoginHandle.already_done()`) matched on the
words "welcome" and "signed in" appearing ANYWHERE on screen, with no
awareness that this exact banner line is saying the opposite. The false
match fired within the first second or two of every attempt, well before
agy's actual "pick a login method" menu ever appeared -- so a second, related
bug (nothing ever sent the one keypress that menu needs to reach a real URL)
never even got a chance to matter on its own.

Fixed both, and proved it working end to end for real: `already_done()` now
checks explicitly for "not signed in" / "not logged in" first and treats
that as authoritative regardless of what other words are nearby; a detected
login-method menu gets one automatic Enter (the pre-highlighted default,
Google OAuth -- the entire point of this flow) before continuing to wait for
the URL. Ran the actual patched code against the real `agy` binary on the
affected server afterward: it now reaches a genuine
`https://accounts.google.com/o/oauth2/auth?...` URL instead of a false
"already signed in".

Also added `/logout` (owner or a registered group's admin, Gemini/Claude/
Cancel buttons): clears a provider's stored credentials on request, so a
sign-in that's stuck in whatever state caused this can be given a clean
slate rather than trusting the (now-fixed, but still worth having an escape
hatch for) detection logic. Gemini: removes just the OAuth token file,
nothing else under `~/.gemini/antigravity-cli`. Claude: runs the real,
documented `claude auth logout` subcommand. Either way, /start's own stale-
success flag (the one v0.2b.27 already had to teach the reauth notice to
clear) is cleared too, and the confirmation says plainly what to do next.

24/24 new tests: 9 in `dev/test_cli_login.py` (anchored on the exact real
banner/menu text captured live -- the false positive, the menu keypress sent
exactly once, a genuinely-already-signed-in session still resolving
correctly) and 15 in `dev/test_logout.py` (both providers, the "already
signed out" case, a failed `claude auth logout` surfaced rather than
swallowed, permission gating). All fourteen earlier suites (342 tests, plus
this pair not double-counted) re-run with no regressions; 357 total.


## v0.2b.29 -- /setscope: change what KIND of assistant this is, not just what it manages

Requested after a weather question got politely declined ("out of scope"):
`/setbrief` only fills in what this deployment looks after -- the "...assistant
for <role>" part -- never the "You are a(n) infrastructure assistant" framing
itself, which was hardcoded in the template with no way to touch it short of
editing SOUL.md/GEMINI.md by hand.

`/setscope <phrase>` (owner or a registered group's admin, same gate as
/setbrief) rewrites just that phrase -- `/setscope general-purpose, with
strong infrastructure skills` turns "You are an infrastructure assistant"
into "You are a general-purpose, with strong infrastructure skills
assistant", leaving the role, hard boundaries, and everything else in the
protected zone untouched. Picks "a"/"an" automatically, is fully re-editable
(not a one-shot placeholder swap like /setbrief's), and the no-args form
shows the current scope plus a deliberate reminder: broadening this spends
more of the shared token budget on things that were previously filtered out
for free, which cuts against the whole reason this project exists over a
heavier agent framework.

25/25 new tests (`dev/test_setscope.py`) against the real template content:
first-time set, re-setting it (proving it's not one-shot), interaction with
/setbrief (each survives the other), the HARD BOUNDARIES / learned-zone
protections, article selection, permission gating, bilingual text, and the
refusal path when a brief has been hand-edited away from the template shape
entirely. All thirteen earlier suites (317 tests) re-run with no
regressions; 342 total.


## v0.2b.28 -- per-group PINs

Each registered group can now have its own PIN, separate from the owner's --
confirming `/addserver`, `/update`, `/rmboundary` etc. from inside a group
uses that group's own PIN if it has set one (`/setgrouppin`, admin or
owner). The owner's personal PIN is still a master credential that works
everywhere -- DM, and every group, on top of (never instead of) whatever
that group has set. A group with none of its own falls back to the owner's,
exactly like every group did before this existed, so nothing regresses for
anyone who never sets one up.

Motivation: multiple companies/teams sharing one deployment (bscloud is
already headed this way) means their admins should not have to share the
owner's own secret just to confirm something in their own group. Deciding
who may SET a group's PIN was the one real fork in the design -- landed on
"that group's own admin, not owner-only," matching the trust level already
granted for reaching the PIN prompt in the first place (v0.2b.25).

`pin.json` now holds `{"owner": {...}|None, "groups": {chat_id: {...}}}`
instead of one flat `{salt,hash}`; a file from before this feature is read
transparently as the owner's PIN, no migration step required. `/rmgrouppin`
removes a group's own PIN (no PIN needed to do that -- it only ever narrows
access back to the owner's, never widens it). `verify_pin()` checks a
group's own PIN first, then always the owner's, both constant-time.

35/35 new tests (`dev/test_group_pin.py`): cross-group isolation (group A's
PIN must not work in group B), the fallback and override behavior, full
/setgrouppin and /rmgrouppin flows through the real keypad handlers,
permission gating (a plain member, an unregistered group), and backward
compatibility with an old flat pin.json. All twelve earlier suites (282
tests) re-run with no regressions; 317 total.


## v0.2b.27 -- /start kept showing Gemini as signed in after a proven failure

Follow-up to v0.2b.26, found from a live screenshot: the reauth notice fired
correctly ("Gemini is signed out"), but running /start right after still
showed "✅ Antigravity (Gemini) sudah sign-in." -- as if nothing had
happened. Confusing on its own, and worse: tapping "Change Gemini" to fix it
already works completely through Telegram (`cmd_setup_button` starts a real
OAuth attempt unconditionally, the checkmark was never a gate), so the only
actual bug was the status lying about needing it in the first place.

Root cause: `agy_signed_in()` falls back to a `setup_state.json` flag
written once, the first time /start's sign-in ever reported success -- and
never re-validates it. A live re-auth failure now clears that flag
immediately (unconditionally, not gated by the notice's own spam cooldown),
so /start's card goes back to showing the truth right away.

2 new tests extending `dev/test_reauth_notice.py`: a stale flag reads as
signed-in on its own, and a live failure clears it. All eleven earlier
suites (270 tests) re-run with no regressions; 282 total.


## v0.2b.26 -- a dead Gemini session was invisible until someone happened to notice

Found live: bscloud's Antigravity (Gemini) sign-in had quietly died -- every
reply was actually coming from the Claude fallback, silently, for hours,
discovered only because the operator was watching closely. `agy_signed_in()`
can't catch this by design (it checks the filesystem, or a permanent
`setup_state.json` flag from whenever /start's sign-in last reported success
-- neither ever re-validates), and nothing else was watching either.

Root cause of *why* it dies unevenly: agy's OAuth session apparently needs
actual use to keep renewing. VM175 (constant real traffic) has a token file
touched within the hour; bscloud (freshly installed, still light use) had no
token file at all. An idle deployment can lose it where a busy one doesn't --
worth knowing, not yet fully explained (Antigravity's own token lifetime
isn't something this project controls or has documented specs for).

Now: each tier-chain failure is checked for the OAuth-relogin signature (agy
gets stuck offering an authorize URL it can't complete non-interactively --
the URL survives into the captured stderr tail even though the interactive
prompt text itself never reaches it, going straight to agy's controlling
terminal instead of the pipe this reads). On a match, the chat that hit it
gets a one-time notice -- reply still comes through via Claude, but here's
why, and here's the fix (/start, then Change Gemini). At most once an hour
while it stays broken (`AGY_REAUTH_NOTICE_COOLDOWN_HOURS`), and reset the
moment a real agy success happens, so a later, genuinely new outage isn't
left waiting out a stale cooldown from one that already resolved.

9/9 new tests (`dev/test_reauth_notice.py`), including detection against the
EXACT failure line captured from bscloud's own log, plus the cases that must
NOT fire (an ordinary network failover, a claude-side failure, success) and
the cooldown/reset behavior. All eleven earlier suites (270 tests) re-run
with no regressions; 279 total.


## v0.2b.25 -- /update's PIN dead-ended in a group for an already-vetted admin

Reported live: a group admin ran `/update`, reached the PIN keypad (its own
entry gate already trusts a registered group's admin, same as /addserver),
then entering the PIN itself was refused with "only in a private DM" --
someone the bot had already decided to trust, seconds earlier, hit a wall
with no security actually gained by it.

Root cause: `cmd_pin_key` decided "who may touch the keypad" from a
hardcoded tuple (`"schedule_install", "unlock", "unlock_and_resume"`) that
was never updated when `addserver` and `update` were added, and separately
decided "where may this be confirmed" from `PIN_ACTIONS_ALLOWED_IN_GROUP` --
two lists that were supposed to describe the same thing and had drifted
apart. `update` passed the first (group-eligible, via `_may_authorize_group_action`,
same as `/addserver`) but wasn't in the second, so it failed one check after
passing the other moments before.

Both checks now read from one set. `addserver` and `update` are added to it
-- neither reveals anything sensitive when confirmed in a group (addserver
starts a form the admin is filling in themselves; update just applies code
already published to the repo). `rmboundary` is added too, per an explicit
follow-up decision: if `/addboundary` is already usable by a group admin,
removing one under the same admin-plus-PIN gate is the consistent call.
Anything not deliberately reviewed for this still falls back to
owner-AND-private-DM -- stricter than owner alone, so a future action added
without a decision made about it can't silently open up to a group nobody
meant to include, even for the owner.

11/11 new tests (`dev/test_pin_group.py`): all three now-fixed actions
succeed for a plain (non-owner) registered-group admin, all three still
refuse a non-admin group member, and a stand-in "unreviewed action" proves
the strict owner-AND-DM fallback holds -- including for the owner themselves,
who is correctly told WHY (not just "not permitted") when they try it from a
group instead. All ten earlier suites (259 tests) re-run with no
regressions; 270 total.


## v0.2b.24 -- `/help` crashed with Message_too_long

Real production error, in a fresh group right after `/registergroup`: tap a
language on the `/help` picker, get "Something went wrong processing that."
The actual exception, from the log: `telegram.error.BadRequest:
Message_too_long`. `HELP_TEXT_EN`/`HELP_TEXT_ID` grew past Telegram's 4096
character limit somewhere across this project's own additions (`/usemodel`,
multi-account Drive, `/lang`, `/update`, `/setbrief`, the Group Privacy
paragraph) -- currently 4755 and 4819 chars -- and every send site handed the
whole string to Telegram in one call: the language-picker buttons, and
`/help en` / `/help id` typed directly (same bug, not yet hit live).

Added `_split_for_telegram()`, used at all three send sites. Cuts at the last
paragraph break (blank line) at or before the limit, falling back to a plain
newline, so a cut lands between sections rather than mid-sentence -- and,
since every section here opens and closes its own Markdown markers, between
entities rather than through one (a mid-entity cut would trade one Telegram
rejection for another, "can't parse entities"). The button-tap path edits the
picker message with the first chunk and sends the rest as follow-up messages,
since an edit can only ever hold one message's worth of text.

Fixed for future growth too, not just today's overrun -- the split runs
regardless of how long the text is, so the next feature added to `/help`
can't reintroduce this same failure by making it 6000 characters instead of
4800.

16/16 new tests (`dev/test_help_split.py`) against the REAL current
HELP_TEXT_EN/ID content -- every chunk fits the limit, nothing is lost or
reordered, and (checked explicitly, since a naive split could easily do this)
every chunk's Markdown stays balanced. Verified with an actual live send
through the real bot token to a real chat -- not just the local checks --
confirming Telegram accepts every chunk in both languages. All nine earlier
suites (243 tests) re-run with no regressions; 259 total.


## v0.2b.23 -- v0.2b.22 pointed the wrong way: default Privacy Mode is usually right

Corrected within the hour, from a direct question: v0.2b.22 framed turning
Group Privacy off as "the real fix" -- but `handle_message` has no
mention/reply gate of its own. With Privacy Mode off, every plain message in
a registered group reaches `_run_turn` and gets a model reply, unconditionally
-- for a team's everyday group chat, that is the bot answering things nobody
asked it, spending this deployment's shared subscription quota on every
message.

Privacy Mode ON (Telegram's default, unchanged) is what most groups actually
want: the bot only ever sees a command, an `@mention`, or a reply to its own
message -- ordinary chatter stays invisible to it, and invisible costs
nothing. Turning it off is the right call for a narrower case -- a live-chat
/ support-portal bot that is meant to answer every message -- not the
recommended default v0.2b.22 made it sound like.

`/help` (both languages) and the README's Group access section now present
Privacy Mode ON as the usual choice, explain what turning it off actually
costs (every message it then sees gets answered), and no longer suggest
switching it off as a one-time fix everyone should apply.

All nine suites (243 tests) re-run with no regressions.


## v0.2b.22 -- /help promised a Group Privacy fix it never actually gave

Found answering "how do I add this bot to a group": the README's Group access
section pointed to `/help` for "Telegram's own group privacy setting", but
`/help`'s group section only described the SYMPTOM (bot answers commands, not
plain messages) and a per-message workaround (mention or reply) -- never the
actual one-time fix. A new group would need every message prefixed or the
bot @mentioned forever, which defeats the point of adding a conversational
agent to a group at all.

Both now say plainly what Privacy Mode is (on by default for every new bot,
hides normal group chat entirely), keep the mention/reply workaround, and add
the real fix: @BotFather -> /mybots -> the bot -> Bot Settings -> Group
Privacy -> Turn off. Added to `/help` (both languages) and to the README's
own Group access steps, which no longer just defers to `/help` for content
that wasn't actually there.

All nine suites (243 tests) re-run with no regressions.


## v0.2b.21 -- apply_update() trusted a stale local ref instead of fetching

Found while deploying v0.2b.20 itself: `apply_update()` fast-forwards to
`origin/<branch>`, but never fetched -- it relied on `origin/<branch>`
already being current from a `check_for_update()` call made earlier (that's
what builds the /update card). Called on its own, with no fetch in the same
process first, it silently fast-forwards to whatever was fetched last, which
can be a version behind the one that's actually latest. On a real deployment
this landed a "successful" update one full version short of what had just
been pushed, with no error -- the exact kind of miss this feature exists to
prevent when it's replacing the bot's own code.

apply_update() now fetches for itself before merging, so it's correct
whether or not something fetched recently, and safe to call on its own (as
distinct from a live Telegram /update tap, where check_for_update() always
runs moments before, but which is exactly how this feature is scripted for
verification).

New test builds a real deployment, does one fetch, then pushes a further
commit to the remote with no second fetch in between -- exactly the gap that
produced the miss -- and confirms apply_update() lands on the true latest
regardless. 33/33 in that suite (up from 31), all nine suites (243 tests
total) re-run with no regressions.


## v0.2b.20 -- /start's card never said how to come back for the rest

Reported directly from a real setup run: complete one item on the /start
card (e.g. Gemini sign-in), and the confirmation just says "done" -- nothing
tells you to send /start again to see the other three. First-time sign-in
success already had that hint; four other completion messages didn't:
the "already signed in" branch, the PIN-set confirmation, and both places
the environment brief gets recorded (the wizard's own brief step, and the
standalone /setbrief command).

Deliberately not a bigger fix: no "back to menu" button, no persistent nav.
Just a line telling the operator to send /start again -- each setup item is
its own short-lived flow, not a multi-step form with state to preserve
between them, so a nav system would be solving a problem that doesn't exist.
The incomplete /start card itself now says this convention once, up front,
rather than leaving it to be inferred.

7/7 new checks pass for the parts driven as real calls (the incomplete card's
own text, both /setbrief paths, the wizard's brief step) in both languages;
the PIN-confirm and already-signed-in messages (heavy to drive without tmux
mocking) verified by direct source inspection. All nine earlier suites
(241 tests) re-run with no regressions.


## v0.2b.19 -- install.sh reinstalled Claude Code on every non-interactive re-run

Found while re-running install.sh against a real server that already had
Claude Code installed, testing the documented Quickstart end to end.
Reinstalled anyway.

The check was `command -v claude`, which depends on the script's own PATH
already including `~/.local/bin` -- and it doesn't yet at that point in the
script (the `export PATH=...` that adds it happens further down, after this
check). In an interactive login shell PATH is usually already set up from a
previous install, masking the bug; in a fresh non-interactive SSH session (or
any CI/automation invocation) it isn't, so the check always fails and the
installer always re-downloads and reinstalls Claude Code, even when it's
already there. agy never had this problem -- its check was already an
absolute path test (`[ -x "$HOME/.local/bin/agy" ]`), not `command -v`.

Claude's check now mirrors agy's: absolute path first, `command -v` kept only
as a fallback for a Claude installed somewhere else entirely (e.g. via a
package manager). Verified against the real binary on a live server -- with
PATH deliberately restricted to exclude `~/.local/bin` (the exact
precondition), `command -v claude` fails as expected while the new absolute
check finds it -- and by re-running install.sh itself twice in a row, second
run skipping the reinstall.

Harmless in effect (the reinstall was idempotent, always ended up correct)
but wasteful, and it stood in the way of testing this same Quickstart
repeatedly without noise.


## v0.2b.18 -- pve-ro-guard split pipelines on every '|', including quoted ones

Found while investigating a downed VM on the itbutler cluster: a perfectly
ordinary read-only command --
`journalctl -b -1 --no-pager | grep -i "142\|shutdown\|power"` -- was refused.
The guard's pipeline splitter was `IFS='|' read -ra SEGMENTS <<< "$CMD"`,
which breaks on every `|` character in the raw string, including ones the
caller quoted as literal data (a grep alternation pattern, an `awk -F'|'`
field separator). The mangled fragments don't match any allowed verb, so a
safe command gets refused for a reason that has nothing to do with what it
does.

Not a security hole either way: the allowlist stayed closed either way (the
segments are only used for judgement, never for what actually executes), so
the only failure mode was false refusals, never false approvals.

Replaced the splitter with a quote-aware one that only treats an unquoted `|`
as a pipeline boundary, tracking single/double-quote state and honoring
backslash escapes inside double quotes. Also explicitly denies `||` (chains
commands the same as `;`/`&`, and survives the metacharacter scan since a
bare `|` has to, for piping) rather than relying on the empty segment it
happened to leave behind under the old splitter.

New test suite at `dev/test_pve_ro_guard.sh`, run against the guard script
directly (no Proxmox node needed) -- 23/23 pass, including the exact command
that was refused and a full pass of the chaining/redirect/write cases the
guard exists to block. Deployed to all 7 itbutler cluster nodes (pm2-pm6,
pm-bk, SRV-SA-01), each backed up first and verified with both a
previously-broken command (now allowed) and a chaining attempt (still
denied) before moving on to the next.


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
