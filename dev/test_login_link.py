#!/usr/bin/env python3
"""Test for the OAuth sign-in URL being sent as a proper <a href> anchor,
not bare text relying on Telegram's own auto-linkifier -- found live: sign-in
failed with Google's "Error 400: invalid_request" for every account tried,
only through this bot, never running agy directly in a real terminal where
none of this HTML rendering exists to go wrong. A real anchor's href is what
the client actually opens/copies verbatim; a bare URL in message text is a
heuristic Telegram applies to what it thinks looks like a link.

Verified independently, live, via the real Bot API (not part of this
automated suite): sending this exact <a href> construction with a realistic
OAuth-shaped URL (many '&'-joined params) came back with a text_link entity
whose .url matched the original byte-for-byte.
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

async def main():
    upd, query = qupd()
    with patch.object(mod, "tmux_available", return_value=True), \
         patch.object(mod, "LoginHandle") as LH:
        handle = LH.return_value
        handle.start = lambda: None
        handle.wait_for_url = lambda timeout=45: REALISTIC_URL
        await mod._begin_cli_login(upd, query, "agy")

    final_call = query.edit_message_text.call_args
    text = final_call[0][0]
    check("the message uses parse_mode=HTML",
          final_call[1].get("parse_mode") == "HTML")

    m = re.search(r'<a href="(.*?)">', text, re.DOTALL)
    check("the message contains a real <a href=...> anchor, not a bare URL", m is not None)
    if m:
        href = html_unescape(m.group(1))
        check("the anchor's href, once HTML-unescaped, matches the real URL exactly",
              href == REALISTIC_URL)
        check("nothing else in the message repeats the raw bare URL "
              "(no second, unlinked copy that could be tapped/copied by mistake)",
              text.count(REALISTIC_URL) == 0)  # only ever appears HTML-escaped, inside the href

    check("the link text itself is human-readable, not the raw URL as link text",
          "Tap here" in text or "Tap di sini" in text)

    # Bilingual: same structural guarantee in Indonesian.
    mod._write_chat_languages({str(OWNER): "id"})
    upd2, query2 = qupd()
    with patch.object(mod, "tmux_available", return_value=True), \
         patch.object(mod, "LoginHandle") as LH2:
        handle2 = LH2.return_value
        handle2.start = lambda: None
        handle2.wait_for_url = lambda timeout=45: REALISTIC_URL
        await mod._begin_cli_login(upd2, query2, "claude")
    text2 = query2.edit_message_text.call_args[0][0]
    m2 = re.search(r'<a href="(.*?)">', text2, re.DOTALL)
    check("[ID] claude sign-in also uses a real anchor",
          m2 is not None and html_unescape(m2.group(1)) == REALISTIC_URL)
    mod._write_chat_languages({})

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
