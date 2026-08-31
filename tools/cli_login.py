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
# A URL ending mid-percent-escape ("...%" or "...%3") is unmistakably cut off,
# never a real terminator -- see wait_for_url()'s use of this.
_TRUNCATED_TAIL_RE = re.compile(r"%[0-9A-Fa-f]?$")
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
# Found live on 10.10.63.11: a code that Google genuinely accepted still got
# reported to the user as "didn't accept that code -- may have expired".
# Real cause, captured directly from the server: agy's FIRST launch after a
# fresh sign-in doesn't land on a "you're signed in" message at all -- it
# lands on a first-run "Choose your color scheme" wizard (a completely
# separate, unrelated onboarding step). That screen's own preview pane
# demonstrates its styling with literal sample lines like "error: compilation
# failed" and "warning: deprecation warning" -- which matched FAILURE_HINTS
# ("error", "failed") before SUCCESS_HINTS ever got a chance, so a genuinely
# accepted code was misreported as rejected, near-instantly, every time.
# Reaching this screen is only possible once the code was accepted, so it is
# unambiguous proof of success.
FIRST_RUN_HINTS = ("choose your color scheme",)


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
    # Width matters more than it looks: agy's OAuth URL alone regularly runs
    # 500-600+ characters, and at 400 columns it hard-wraps mid-parameter with
    # no continuation marker. capture-pane's -J (meant to rejoin a soft-wrapped
    # line) does NOT undo this -- confirmed live, byte for byte, both with and
    # without -J -- almost certainly because agy's TUI draws its bordered
    # panel via cursor-positioned redraws rather than plain sequential output,
    # which is what tmux's own wrap-tracking actually watches for. So the URL
    # that reached the user was silently missing everything after the wrap
    # point: e.g. redirect_uri and scope alone are ~230 characters before the
    # more distinguishing part of the query string even starts. 2000 columns
    # comfortably fits any realistic OAuth URL on one real line, sidestepping
    # the whole rejoin question rather than trying to solve it after the fact.
    def start(self) -> None:
        self.kill()  # a leftover from an abandoned attempt would confuse us
        self._tmux("new-session", "-d", "-s", self.session, "-x", "2000", "-y", "60",
                   *self.command)

    def wait_for_url(self, timeout: int = 45) -> Optional[str]:
        deadline = time.time() + timeout
        sent_menu_default = False
        while time.time() < deadline:
            screen = self.pane()
            for candidate in URL_RE.findall(screen):
                url = candidate.rstrip(").,'\"…")
                if "oauth" in url.lower() or "auth" in url.lower():
                    # Defense in depth against the class of bug the 2000-column
                    # pane width above fixes at the source: a URL cut off
                    # mid-percent-escape ("...%", "...%3") is unmistakably
                    # truncated, not a real terminator. Found live: the wide
                    # pane fixes this specific wrap, but nothing here should
                    # go on trusting a URL that still looks cut off, from
                    # whatever cause, rather than silently handing the human a
                    # broken sign-in link the way this did before.
                    if _TRUNCATED_TAIL_RE.search(url):
                        continue
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
            elif any(h in low for h in FIRST_RUN_HINTS):
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
        # An already-signed-in agy can still land on the first-run color-scheme
        # wizard (see FIRST_RUN_HINTS above) instead of printing a URL -- that
        # screen only exists to appear post-auth, so it counts as "done" here
        # too, not just in wait_for_result().
        return any(h in low for h in SUCCESS_HINTS) or any(h in low for h in FIRST_RUN_HINTS)


def tmux_available() -> bool:
    return shutil.which("tmux") is not None
