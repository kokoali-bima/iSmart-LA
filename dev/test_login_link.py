#!/usr/bin/env python3
"""Test for how the OAuth sign-in URL is presented.

History, in order, each disproven by the next real attempt:
  v0.2b.29 and earlier: bare URL in message text, relying on Telegram's own
    auto-linkifier. Never actually reached (LoginHandle.already_done() false-
    positived on agy's own "Welcome... you are currently not signed in"
    banner before a URL was ever found -- fixed in v0.2b.30).
  v0.2b.32: switched to a real <a href> anchor, theorizing the bare-URL
    auto-linkifier was the fragile part. WRONG theory: with the real broken
    URL in hand from a live failure, redirect_uri had gone from ...%3A%2F%2F...
    to ...%253A%252F%2F... -- the literal "%" character itself re-escaped to
    "%25", i.e. an already-percent-encoded URL got percent-encoded AGAIN when
    at least one real Telegram client launched a browser from a tapped <a
    href>. Google's OAuth server correctly rejects that as invalid_request:
    it no longer matches any registered redirect_uri.

Current (this version): the URL goes inside a <code> block instead. Telegram
treats code-block content as literal text to COPY, not a link to open --
sidestepping the "launch a URL" platform call that both earlier approaches
funneled through (a bare auto-linked URL and an explicit anchor tap the same
underlying mechanism). This test can't reproduce a specific client's launch-
time re-encoding bug (that needs a real device, and was), so it verifies what
IS testable here: no launchable link construct is present at all, the raw URL
inside the <code> block is exactly the original once HTML-unescaped, and it
appears nowhere else unlinked/differently-formed in the message.
"""
import asyncio, importlib.util, os, re, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
scratch = Path(tempfile.mkdtemp(prefix="isla_link_"))
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

OWNER = 111
results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

# The exact shape of a real agy OAuth URL -- long, several '&'-joined params,
# already percent-encoded (redirect_uri and scope both contain %3A/%2F).
REALISTIC_URL = (
    "https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=X"
    "&code_challenge=Y&code_challenge_method=S256&prompt=consent"
    "&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
    "&response_type=code&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email&state=ABC123"
)

def qupd():
    msg = SimpleNamespace(text="x", reply_text=AsyncMock())
    q = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(message=None, effective_message=msg, callback_query=q,
                           effective_user=SimpleNamespace(id=OWNER),
                           effective_chat=SimpleNamespace(id=OWNER, type="private", title=None)), q

def html_unescape(s: str) -> str:
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

async def run_login(provider: str, lang: str | None = None):
    if lang:
        mod._write_chat_languages({str(OWNER): lang})
    upd, query = qupd()
    with patch.object(mod, "tmux_available", return_value=True), \
         patch.object(mod, "LoginHandle") as LH:
        handle = LH.return_value
        handle.start = lambda: None
        handle.wait_for_url = lambda timeout=45: REALISTIC_URL
        await mod._begin_cli_login(upd, query, provider)
    if lang:
        mod._write_chat_languages({})
    return query.edit_message_text.call_args

async def main():
    call = await run_login("agy")
    text = call[0][0]

    check("the message uses parse_mode=HTML", call[1].get("parse_mode") == "HTML")

    # Regression guard: never go back to a launchable <a href> construct --
    # that specific shape is what triggered the double-encoding bug.
    check("no <a href> anchor is used (that path double-encoded on a real client)",
          "<a href" not in text.lower())

    m = re.search(r"<code>(.*?)</code>", text, re.DOTALL)
    check("the URL is presented inside a <code> block", m is not None)
    if m:
        recovered = html_unescape(m.group(1))
        check("the <code> block content, HTML-unescaped, is exactly the real URL",
              recovered == REALISTIC_URL)

    check("instructs copying, not tapping to open",
          "copy" in text.lower() or "salin" in text.lower())
    check("warns that tapping/opening directly can mangle the URL",
          "mangle" in text.lower() or "merusak" in text.lower())

    # The raw URL must not additionally appear somewhere else in a form that
    # would still be tappable (only inside the one escaped <code> block).
    escaped = mod._tg_escape(REALISTIC_URL)
    check("the escaped URL appears exactly once (only inside the <code> block)",
          text.count(escaped) == 1)

    # Bilingual: same structural guarantee in Indonesian, other provider.
    call2 = await run_login("claude", lang="id")
    text2 = call2[0][0]
    m2 = re.search(r"<code>(.*?)</code>", text2, re.DOTALL)
    check("[ID] claude sign-in also uses a <code> block, no <a href>",
          m2 is not None and "<a href" not in text2.lower()
          and html_unescape(m2.group(1)) == REALISTIC_URL)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
