#!/usr/bin/env python3
"""Tests for tools/cli_login.py, anchored on the REAL text captured live from
agy's own startup banner and menu on 10.10.63.11:

  "Welcome to the Antigravity CLI. You are currently not signed in."
  "Select login method:\n > 1. Google OAuth\n   2. Use a Google Cloud project"

The bug this fixes: that banner contains "welcome" AND "signed in" -- both
were SUCCESS_HINTS -- on a line explicitly saying the opposite, so /start
reported success within a couple of seconds for a session directly confirmed
signed OUT (a non-interactive agy call returned "authentication required").
Separately, nothing ever sent the one keypress the menu needs before a real
URL appears -- unpatched, that also silently fed the same false "done".

subprocess.run is mocked to replay a scripted tmux pane sequence rather than
spinning up real tmux -- the actual live end-to-end proof (a real OAuth URL
reached through the real binary) was run directly on the server as part of
fixing this, and is recorded in this project's own CHANGELOG rather than
repeated here as an automated test.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cli_login as cl

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

REAL_BANNER = (
    "\n     ▄▀▀▄\n    ▀▀▀▀▀▀\n"
    " Welcome to the Antigravity CLI. You are currently not signed in.\n"
)
REAL_MENU = REAL_BANNER + (
    "\n Select login method:\n > 1. Google OAuth\n   2. Use a Google Cloud project\n"
    "\n [Use arrow keys to navigate, Enter to select]\n"
)
REAL_URL_SCREEN = (
    "\n Open the URL below in your browser:\n ---\n"
    " https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=x"
    "&code_challenge=y&response_type=code&scope=z&state=w\n ---\n"
    "\n After authenticating, copy the code displayed in the browser and paste it below:\n"
)
REAL_SUCCESS_SCREEN = "\n Successfully authenticated. Welcome back!\n"

# --- 1. already_done() must not false-positive on the real banner ----------
check("already_done() is False on the real 'not signed in' banner (THE bug)",
      cl.LoginHandle.already_done(REAL_BANNER) is False)
check("already_done() is False on the menu screen too (still not signed in)",
      cl.LoginHandle.already_done(REAL_MENU) is False)
check("already_done() IS True on a real success screen",
      cl.LoginHandle.already_done(REAL_SUCCESS_SCREEN) is True)
check("already_done() is False on an empty/blank screen",
      cl.LoginHandle.already_done("") is False)

# --- 2. wait_for_url: reaches the URL, sending the menu keypress along the way
def make_handle(pane_sequence):
    """A LoginHandle whose .pane() replays pane_sequence one screen per call,
    holding on the last entry once exhausted, and whose tmux calls are
    recorded instead of actually run."""
    h = cl.LoginHandle(session="test", command=["agy"])
    calls = []
    seq = list(pane_sequence)
    def fake_tmux(*args):
        calls.append(args)
        return MagicMock(stdout="")
    def fake_pane():
        return seq.pop(0) if len(seq) > 1 else seq[0]
    h._tmux = fake_tmux
    h.pane = fake_pane
    return h, calls

with patch.object(cl.time, "sleep", lambda s: None):  # don't actually wait in tests
    h, calls = make_handle([REAL_BANNER, REAL_BANNER, REAL_MENU, REAL_URL_SCREEN])
    url = h.wait_for_url(timeout=10)
    check("wait_for_url reaches the real OAuth URL despite the misleading banner",
          url is not None and "accounts.google.com" in url and "oauth" in url)
    check("wait_for_url sent exactly one Enter to get past the menu",
          sum(1 for c in calls if c and c[0] == "send-keys") == 1)

    # A session that's genuinely already signed in (no menu ever appears,
    # banner says something actually positive) must still resolve to "done".
    h2, calls2 = make_handle([REAL_SUCCESS_SCREEN])
    url2 = h2.wait_for_url(timeout=10)
    check("a genuinely-already-signed-in session still resolves as done (url=None)",
          url2 is None)
    check("...without ever sending a menu keypress (there was no menu)",
          not any(c and c[0] == "send-keys" for c in calls2))

    # The menu keypress is sent only ONCE even if the menu is still on screen
    # on later polls (must not spam Enter every second while, say, a real
    # human is also looking at their own browser).
    h3, calls3 = make_handle([REAL_MENU, REAL_MENU, REAL_MENU, REAL_URL_SCREEN])
    h3.wait_for_url(timeout=10)
    check("the menu keypress is sent at most once, not once per poll",
          sum(1 for c in calls3 if c and c[0] == "send-keys") == 1)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
