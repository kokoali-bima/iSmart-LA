#!/usr/bin/env python3
"""
Friendly wrapper around the Antigravity CLI's OAuth login.

WHY THIS EXISTS
    `agy` has no `login` subcommand -- signing in means running the full-screen
    interactive TUI, which needs a real TTY. That is fine when you are sitting at
    a terminal, and awkward everywhere else: over a plain SSH command, or from an
    installer, it dies with "could not open TTY", and even with a TTY it takes
    over the screen right in the middle of a scripted install.

    So this runs `agy` inside tmux (which gives it the real TTY it insists on),
    reads the rendered screen, pulls out the sign-in URL, and hands the whole
    thing back as two plain prompts: open this URL, then paste the code.

    Nothing here bypasses or fakes the login -- it is the same OAuth flow, just
    not shouted through a full-screen UI mid-install.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SESSION = "ismart-agy-login"
URL_RE = re.compile(r"https://\S*(?:google|antigravity)\S*", re.I)
# Words agy prints once a session exists.
SUCCESS_HINTS = ("logged in", "signed in", "authenticated", "welcome", "ready")


def tmux(*args: str, capture: bool = False) -> str:
    cmd = ["tmux", *args]
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True).stdout
    subprocess.run(cmd, capture_output=True, text=True)
    return ""


def pane() -> str:
    return tmux("capture-pane", "-p", "-t", SESSION, capture=True)


def kill() -> None:
    tmux("kill-session", "-t", SESSION)


def wait_for(predicate, timeout: int, poll: float = 1.0) -> str:
    """Poll the rendered pane until `predicate` likes what it sees."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = pane()
        if predicate(last):
            return last
        time.sleep(poll)
    return last


def main() -> int:
    ap = argparse.ArgumentParser(description="Sign in to Antigravity (agy).")
    ap.add_argument("--agy", default=os.environ.get("AGY_BIN", str(Path.home() / ".local/bin/agy")))
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for you to finish in the browser")
    args = ap.parse_args()

    if not shutil.which("tmux"):
        print("! tmux is required for this helper (apt install tmux).", file=sys.stderr)
        print(f"  You can also just run `{args.agy}` directly and log in there.", file=sys.stderr)
        return 1
    if not Path(args.agy).exists():
        print(f"! agy not found at {args.agy}", file=sys.stderr)
        return 1

    kill()  # a leftover session from an interrupted attempt would confuse us
    tmux("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", args.agy)

    print("Starting Antigravity sign-in...")
    screen = wait_for(lambda s: bool(URL_RE.search(s)), timeout=45)
    match = URL_RE.search(screen)
    if not match:
        # Already signed in? Then there is nothing to do and no URL to show.
        if any(h in screen.lower() for h in SUCCESS_HINTS):
            print("\n✓ Already signed in -- nothing to do.")
            kill()
            return 0
        print("\n! Could not find a sign-in URL on agy's screen. What it showed:\n", file=sys.stderr)
        print(screen[-1500:], file=sys.stderr)
        print(f"\n  Falling back: run `{args.agy}` yourself and log in there.", file=sys.stderr)
        kill()
        return 1

    url = match.group(0).rstrip(").,'\"")
    print("\n" + "=" * 70)
    print("1. Open this URL in a browser (any device -- it does not have to be this one):\n")
    print(f"   {url}\n")
    print("2. Sign in with the Google account that has the AI Pro / Ultra plan.")
    print("3. Copy the code it gives you and paste it below.")
    print("=" * 70)

    try:
        code = input("\nCode (or press Enter to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        code = ""
    if not code:
        print("Cancelled -- no changes made.")
        kill()
        return 1

    tmux("send-keys", "-t", SESSION, code, "Enter")
    screen = wait_for(
        lambda s: any(h in s.lower() for h in SUCCESS_HINTS) or "error" in s.lower(),
        timeout=args.timeout,
    )
    kill()

    low = screen.lower()
    if any(h in low for h in SUCCESS_HINTS) and "error" not in low:
        print("\n✓ Signed in to Antigravity.")
        return 0
    print("\n! Sign-in did not clearly succeed. Last screen:\n", file=sys.stderr)
    print(screen[-1200:], file=sys.stderr)
    print(f"\n  Try running `{args.agy}` directly to finish.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
