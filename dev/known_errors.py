#!/usr/bin/env python3
"""Every failure this project has actually shipped, and what stops it recurring.

This is a REGISTRY, not prose. Each entry names the guard test that would fail
if the bug came back, and `error_index.py` refuses to pass when an entry points
at a test that no longer exists -- because a regression quietly losing its guard
is how the same bug ships twice.

Rules for adding an entry:
  * Only failures that REACHED a user or a production host. Bugs caught in
    development are ordinary work, not history worth carrying.
  * `symptom` is what was actually seen -- the log line, the message, the
    silence. Not the diagnosis.
  * `guard` names a real file in dev/. If a bug genuinely cannot be tested,
    say so in `guard` as "none: <why>" and it will be listed as unguarded.
"""

ERRORS = [
    {
        "id": "E001",
        "date": "2026-09-04",
        "area": "Google Drive",
        "symptom": "⚠️ Upload ke Drive gagal ... timed out talking to "
                   "Google Drive -- but the report was already in Drive.",
        "cause": "_gdrive_upload ran `rclone copyto` and `rclone link` inside one "
                 "try block, so a slow share-link call reported the upload as "
                 "failed. Measured: link normally ~5s, seen at 43.3s against a "
                 "30s ceiling, roughly one run in five.",
        "fix": "Split the two calls. Once copyto returns the file is in Drive and "
               "the upload is reported successful whatever the link call does.",
        "guard": "test_gdrive_rclone_auth.py",
        "release": "v0.2b.71",
    },
    {
        "id": "E002",
        "date": "2026-09-04",
        "area": "Release process",
        "symptom": "/update reported v0.2b.70 after v0.2b.71 was pushed and running.",
        "cause": "current_version() is `git describe --tags`, and the release was "
                 "committed and pushed without ever creating the tag.",
        "fix": "dev/test_release_consistency.py fails when release notes are "
               "committed with no matching tag.",
        "guard": "test_release_consistency.py",
        "release": "v0.2b.72",
    },
    {
        "id": "E003",
        "date": "2026-09-04",
        "area": "Dev tooling",
        "symptom": "The symbol index reported nothing on Linux, where the full "
                   "suite actually runs.",
        "cause": "Its output directory is a Windows path; on Linux it took the "
                 "'unusable path' branch and skipped itself entirely, so the "
                 "duplicate-name check it exists for did nothing.",
        "fix": "Falls back into the checkout instead of skipping, run_all prints "
               "its output, and a duplicate top-level name fails the run.",
        "guard": "test_run_all.py",
        "release": "v0.2b.72",
    },
    {
        "id": "E004",
        "date": "2026-09-04",
        "area": "Model brief",
        "symptom": "Asked for a video clip, the bot answered that it \"can only "
                   "send local media files stored on disk\" and offered links.",
        "cause": "GEMINI.md/SOUL.md are written once by install.sh and never "
                 "refreshed by /update, so every capability shipped after the "
                 "install date was invisible. Measured on the live host: "
                 "GDRIVE_DELETE, GDRIVE_MOVE, yt-dlp and ffmpeg all appeared "
                 "zero times.",
        "fix": "CAPABILITIES_BRIEF lives in code and is injected at conversation "
               "start, so it ships with /update.",
        "guard": "test_capabilities_brief.py",
        "release": "v0.2b.73",
    },
    {
        "id": "E005",
        "date": "2026-09-04",
        "area": "Cost control",
        "symptom": "One conversation consumed 46.4% of a week's tokens -- 56.7M "
                   "over 11 turns -- with a single cost warning at the start.",
        "cause": "The expensive-conversation hint fired once per session and then "
                 "stayed silent through the entire expensive part.",
        "fix": "cost_hint_due() warns again on every doubling of the per-turn cost.",
        "guard": "test_cost_hint.py",
        "release": "v0.2b.74",
    },
    {
        "id": "E006",
        "date": "2026-09-04",
        "area": "Media",
        "symptom": "A 32MB AV1 clip re-encoded to H.264 came back at 67MB and was "
                   "still treated as a shrink.",
        "cause": "shrink_video_to_fit compared its output only against Telegram's "
                 "50MB ceiling, never against the file it was given.",
        "fix": "Refuse any result that is not actually smaller than the input.",
        "guard": "test_cost_hint.py",
        "release": "v0.2b.74",
    },
    {
        "id": "E007",
        "date": "2026-09-05",
        "area": "Write gate",
        "symptom": "After entering the PIN, the bot asked for the PIN again, then "
                   "crashed: AttributeError 'NoneType' object has no attribute "
                   "'reply_text' in offer_unlock. 14 unlocks in one day.",
        "cause": "Two faults compounding. The write window (10 min in a group) "
                 "expired while the turn it was opened for was still running -- "
                 "unlocked 23:06:50, turn finished 23:25:01, eighteen minutes, "
                 "because agy failed over mid-turn. The end-of-turn check then "
                 "saw a closed window and re-offered the unlock; offer_unlock "
                 "used update.message, which is always None inside a button "
                 "callback, so the whole turn died after doing the work.",
        "fix": "Default window 30 min and ceiling 6h; the end-of-turn re-offer is "
               "suppressed when the window was open at turn start; offer_unlock "
               "goes through _msg().",
        "guard": "test_unlock_window.py",
        "release": "v0.2b.75",
    },
    {
        "id": "E008",
        "date": "2026-09-05",
        "area": "Telegram plumbing",
        "symptom": "delivery to Telegram failed (attempt 1/2 and 2/2), then "
                   "\"even the failure notice couldn't be delivered\".",
        "cause": "_msg() handled typed messages and button callbacks but not "
                 "EDITED messages, where both update.message and "
                 "update.callback_query are None. _authorized() lets edited "
                 "messages through on purpose, so the turn ran and then had "
                 "nowhere to reply.",
        "fix": "_msg() falls back to update.effective_message.",
        "guard": "test_reply_target.py",
        "release": "v0.2b.75",
    },
    {
        "id": "E009",
        "date": "2026-09-05",
        "area": "Servers",
        "symptom": "/addserver failed repeatedly for the Kota Bima Proxmox with "
                   "\"pve-ro-guard: refused -- this key is read-only\", although "
                   "the SSH key was installed correctly.",
        "cause": "The probe was `echo ISMART_OK && uname -sr`, and our own "
                 "read-only guard denies any `&` before it looks at the verbs. "
                 "Reproduced on both hosts. The one server that ever registered "
                 "got in before the guard was installed.",
        "fix": "Probe with a bare `uname -sr`, which needs no sentinel and no "
               "shell operators.",
        "guard": "test_addserver_probe.py",
        "release": "v0.2b.75",
    },
    {
        "id": "E010",
        "date": "2026-09-05",
        "area": "Google Drive",
        "symptom": "CRITICAL: Failed to create file system ... googleapi: Error "
                   "403: Quota exceeded for quota metric 'Queries'.",
        "cause": "rclone's shared Google client_id is used by everyone who never "
                 "made their own, so its project-wide query quota runs out. "
                 "Nothing wrong with the account, file or config -- the same "
                 "remote listed fine five hours later.",
        "fix": "Detect the 403 quota shape and say it is temporary and "
               "self-clearing, instead of pasting rclone's CRITICAL line.",
        "guard": "test_gdrive_rclone_auth.py",
        "release": "v0.2b.75",
    },
]
