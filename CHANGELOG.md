# Changelog

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
