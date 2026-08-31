#!/usr/bin/env python3
"""
Drive a CLI's interactive OAuth login from somewhere that isn't a terminal.

Both CLIs this project uses sign in the same way: run the tool, it prints a URL,
you approve in a browser, you paste a code back. Neither offers a headless path
-- `agy` has no login subcommand at all, and `claude auth login` expects a real
terminal. Run either over a plain SSH command and it fails on the TTY; run it
from a bot and there is nobody at the keyboard.

So both are driven through tmux, which supplies the real TTY they insist on,
and reduced to the two steps that genuinely need a human: here is a URL, paste
the code. Nothing is bypassed -- it is the same OAuth flow, just legible from a
Telegram chat.

Used by tools/agy_login.py (standalone) and by the /start wizard in the bot.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

# Matches the sign-in URL either CLI prints. Kept deliberately broad -- these
# URLs change shape between releases, and a pattern that is too specific fails
# closed in a way that looks like "login is broken".
URL_RE = re.compile(r"https://\S+", re.I)
SUCCESS_HINTS = ("logged in", "signed in", "login successful", "authenticated",
                 "you're all set", "success")
# Antigravity's own startup banner is: "Welcome to the Antigravity CLI. You
# are currently not signed in." -- which contains "signed in" AND (before this
# was found) "welcome", both SUCCESS_HINTS, on a line that is explicitly
# saying the opposite. Checked first and wins outright: a screen saying "not
# signed in" is authoritative regardless of what other words are nearby.
# Found live: this made /start ALWAYS report success within a couple of
# seconds without ever reaching a real OAuth flow, for a session that was
# provably signed out (confirmed separately via a direct, non-interactive
# agy call returning "authentication required").
NOT_SIGNED_IN_HINTS = ("not signed in", "not logged in", "currently not")
FAILURE_HINTS = ("invalid", "expired", "failed", "error", "denied")
# Antigravity's OAuth flow starts on a "Select login method" menu -- one
# keypress (Enter, accepting the pre-highlighted default: Google OAuth, which
# is the entire point of this flow) away from the URL that used to be the
# very next thing checked for. Nothing sent it that keypress before, so this
# menu was just watched forever until the 45s timeout, then reported (via the
# bug above) as "already signed in" instead of "timed out".
MENU_HINTS = ("select login method", "use arrow keys")


@dataclass
class LoginHandle:
    session: str
    command: list[str]

    # -- tmux plumbing ----------------------------------------------------
    def _tmux(self, *args: str) -> str:
        return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout

    def pane(self) -> str:
        # -J re-joins lines tmux wrapped at the pane width. Without it a long
        # OAuth URL comes back cut into pieces and is useless.
        return self._tmux("capture-pane", "-p", "-J", "-t", self.session)

    def alive(self) -> bool:
        out = subprocess.run(["tmux", "has-session", "-t", self.session],
                             capture_output=True, text=True)
        return out.returncode == 0

    def kill(self) -> None:
        self._tmux("kill-session", "-t", self.session)

    # -- flow -------------------------------------------------------------
    def start(self) -> None:
        self.kill()  # a leftover from an abandoned attempt would confuse us
        self._tmux("new-session", "-d", "-s", self.session, "-x", "400", "-y", "60",
                   *self.command)

    def wait_for_url(self, timeout: int = 45) -> Optional[str]:
        deadline = time.time() + timeout
        sent_menu_default = False
        while time.time() < deadline:
            screen = self.pane()
            for candidate in URL_RE.findall(screen):
                url = candidate.rstrip(").,'\"…")
                if "oauth" in url.lower() or "auth" in url.lower():
                    return url
            if self.already_done(screen):
                return None
            # A "pick a login method" menu, still needing the one keypress a
            # human would give it (Enter, taking the pre-highlighted default)
            # before a URL ever appears. Sent once per attempt.
            if not sent_menu_default and any(h in screen.lower() for h in MENU_HINTS):
                self._tmux("send-keys", "-t", self.session, "Enter")
                sent_menu_default = True
            time.sleep(1)
        return None

    def send_code(self, code: str) -> None:
        self._tmux("send-keys", "-t", self.session, code, "Enter")

    def wait_for_result(self, timeout: int = 120) -> tuple[bool, str]:
        """-> (succeeded, last screen). Treats the process exiting cleanly as
        success too: some CLIs just quit rather than printing a confirmation."""
        deadline = time.time() + timeout
        screen = ""
        while time.time() < deadline:
            screen = self.pane()
            low = screen.lower()
            if any(h in low for h in NOT_SIGNED_IN_HINTS):
                pass  # authoritative: not a success, whatever else is on screen
            elif any(h in low for h in SUCCESS_HINTS):
                return True, screen
            elif any(h in low for h in FAILURE_HINTS):
                return False, screen
            if not self.alive():
                return True, screen
            time.sleep(1)
        return False, screen

    @staticmethod
    def already_done(screen: str) -> bool:
        low = screen.lower()
        if any(h in low for h in NOT_SIGNED_IN_HINTS):
            return False
        return any(h in low for h in SUCCESS_HINTS)


def tmux_available() -> bool:
    return shutil.which("tmux") is not None
