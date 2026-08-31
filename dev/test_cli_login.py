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

A THIRD bug, found only after both of the above were fixed and a real user
still couldn't sign in: the URL itself was silently truncated. agy's OAuth
URL runs 500-700+ characters; at the pane's old 400-column width it hard-wraps
mid-parameter with no continuation marker, and capture-pane's -J (meant to
rejoin a soft-wrapped line) does not undo it -- confirmed live, byte for
byte, with and without -J, almost certainly because agy's TUI draws its
bordered panel via cursor-positioned redraws rather than plain sequential
output, which is what tmux's own wrap-tracking watches for. Fixed at the
source (pane width raised to 2000 columns, comfortably fitting any realistic
OAuth URL on one real line) -- not testable here, since mocking pane() bypasses
the actual mechanism where wrapping happens. What IS added and tested here:
a defense-in-depth check that refuses a URL match ending mid-percent-escape
("...%", "...%3") rather than trusting it, in case some other, not yet
anticipated wrap scenario produces the same class of silent truncation again.

The FIRST version of this suite's own "reaches the real OAuth URL" check
(matching only 'accounts.google.com' and 'oauth' as substrings) was itself a
false positive of exactly this kind -- it never noticed the mocked test URL
it was checking was, coincidentally, complete, and would have passed just as
happily against the truncated one a real user actually hit. Rewritten to
check the URL's full, unbroken shape instead of a substring.

subprocess.run is mocked to replay a scripted tmux pane sequence rather than
spinning up real tmux -- the actual live end-to-end proof (a real, complete
OAuth URL reached through the real binary, verified down to the state=
parameter surviving to the very end) was run directly on the server as part
of fixing this, and is recorded in this project's own CHANGELOG rather than
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
    # Full-shape check, not a substring match -- a substring check is exactly
    # what let the real truncation bug through this suite once already.
    EXPECTED_TAIL = "response_type=code&scope=z&state=w"
    check("wait_for_url reaches the real OAuth URL despite the misleading banner",
          url is not None and url.startswith("https://accounts.google.com/o/oauth2/auth?")
          and url.endswith(EXPECTED_TAIL))
    check("wait_for_url sent exactly one Enter to get past the menu",
          sum(1 for c in calls if c and c[0] == "send-keys") == 1)

    # A URL match cut off mid-percent-escape must be refused, not returned --
    # the defense-in-depth guard for whatever future wrap scenario isn't
    # caught by the pane-width fix alone.
    TRUNCATED_URL_SCREEN = (
        "\n Open the URL below in your browser:\n ---\n"
        " https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=x"
        "&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&scope=https%"
        "\n ---\n"
    )
    h4, calls4 = make_handle([TRUNCATED_URL_SCREEN, TRUNCATED_URL_SCREEN, REAL_URL_SCREEN])
    url4 = h4.wait_for_url(timeout=10)
    check("a URL match ending mid-percent-escape ('...%') is refused, not returned",
          url4 is not None and not url4.endswith("%"))
    check("...and it keeps polling until a real, complete URL shows up",
          url4 == "https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=x"
                  "&code_challenge=y&response_type=code&scope=z&state=w")

    h5, _ = make_handle([TRUNCATED_URL_SCREEN])
    check("a permanently-truncated URL (never completes) times out as None, not a broken link",
          h5.wait_for_url(timeout=0.01) is None)

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
