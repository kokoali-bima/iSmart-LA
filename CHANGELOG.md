# Changelog

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
