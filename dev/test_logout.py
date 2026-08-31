#!/usr/bin/env python3
"""Tests for /logout: the command, its Gemini/Claude/Cancel button choices,
permission gating, and that a successful logout clears the same stale
setup_state.json flag the reauth-notice fix (v0.2b.27) already had to clear
once (agy_signed_in()'s fallback) -- /logout is a second, deliberate way to
reach that same clean state.
"""
import asyncio, importlib.util, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
scratch = Path(tempfile.mkdtemp(prefix="isla_logout_"))
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

def upd():
    msg = SimpleNamespace(reply_text=AsyncMock(), text=None)
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=OWNER),
                           effective_chat=SimpleNamespace(id=OWNER, type="private", title=None))
def ctx():
    return SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock()), args=[])
def qupd(data):
    msg = SimpleNamespace(text="x", reply_text=AsyncMock())
    q = SimpleNamespace(data=data, message=msg, answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(message=None, effective_message=msg, callback_query=q,
                           effective_user=SimpleNamespace(id=OWNER),
                           effective_chat=SimpleNamespace(id=OWNER, type="private", title=None)), q

async def main():
    # --- /logout shows the choice card ---
    u = upd()
    await mod.cmd_logout(u, ctx())
    txt = u.message.reply_text.call_args[0][0]
    kb = u.message.reply_text.call_args[1].get("reply_markup")
    check("/logout shows a card", "which one" in txt.lower() or "yang mana" in txt.lower())
    check("/logout offers Gemini/Claude/Cancel buttons",
          kb is not None and len(kb.inline_keyboard) == 2
          and len(kb.inline_keyboard[0]) == 2 and len(kb.inline_keyboard[1]) == 1)

    # --- Cancel does nothing ---
    u_c, q_c = qupd("logout:cancel")
    await mod.cmd_logout_button(u_c, ctx())
    check("cancel confirms without acting",
          "Cancelled" in q_c.edit_message_text.call_args[0][0]
          or "Dibatalkan" in q_c.edit_message_text.call_args[0][0])

    # --- Gemini: no token file yet -> "already signed out" ---
    u_a1, q_a1 = qupd("logout:agy")
    await mod.cmd_logout_button(u_a1, ctx())
    check("agy logout with nothing stored says 'already signed out'",
          "already signed out" in q_a1.edit_message_text.call_args[0][0].lower()
          or "sudah logout" in q_a1.edit_message_text.call_args[0][0].lower())

    # --- THE real bug reported live: setup_state says "agy" done, but no
    # token file exists (the session died on its own -- exactly what /logout
    # exists to recover from). An earlier version returned before ever
    # clearing the stale flag in precisely this case, so /start kept
    # showing green no matter how many times /logout ran. ---
    mod._mark_setup("agy", OWNER)
    check("setup: 'agy' flag set with no token file present (the exact bug scenario)",
          "agy" in mod._setup_state())
    u_a1b, q_a1b = qupd("logout:agy")
    await mod.cmd_logout_button(u_a1b, ctx())
    check("logout still clears the stale flag even when there was no token file to remove",
          "agy" not in mod._setup_state())

    # --- Gemini: a real token file exists -> removed, flag cleared, told to /start ---
    token_dir = scratch / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True)
    token_file = token_dir / "antigravity-oauth-token"
    token_file.write_text("fake-token-material")
    mod._mark_setup("agy", OWNER)  # simulate /start having marked it done once
    check("setup: agy flag is set before logout", "agy" in mod._setup_state())

    u_a2, q_a2 = qupd("logout:agy")
    await mod.cmd_logout_button(u_a2, ctx())
    txt_a2 = q_a2.edit_message_text.call_args[0][0]
    check("agy logout removes the token file", not token_file.exists())
    check("agy logout clears the stale setup_state flag", "agy" not in mod._setup_state())
    check("agy logout confirms success and points to /start",
          "logged out" in txt_a2.lower() or "sudah logout" in txt_a2.lower())
    check("agy logout mentions Change Gemini as the next step",
          "start" in txt_a2.lower())

    # --- other files under antigravity-cli survive (only the token is removed) ---
    (token_dir / "settings.json").write_text("{}")
    (token_dir / "antigravity-oauth-token").write_text("fake-token-material-2")
    mod.logout_agy()
    check("logout_agy leaves OTHER files in the credential dir alone",
          (token_dir / "settings.json").exists())

    # --- Claude: mock the real subprocess boundary, not the whole function ---
    with patch.object(mod.subprocess, "run") as run_mock:
        run_mock.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(mod, "claude_signed_in", return_value=True):
            mod._mark_setup("claude", OWNER)
            u_cl, q_cl = qupd("logout:claude")
            await mod.cmd_logout_button(u_cl, ctx())
            check("claude logout calls the real 'claude auth logout' subcommand",
                  run_mock.call_args[0][0][-2:] == ["auth", "logout"])
            check("claude logout clears the setup flag",
                  "claude" not in mod._setup_state())
            check("claude logout confirms success",
                  "logged out" in q_cl.edit_message_text.call_args[0][0].lower()
                  or "sudah logout" in q_cl.edit_message_text.call_args[0][0].lower())

    # --- Claude: the same stale-flag bug, mirrored -- claude_signed_in() (a
    # LIVE check) already says signed out, but the setup_state flag is still
    # set from earlier. Must be cleared here too, not just when the live
    # subcommand actually runs. ---
    mod._mark_setup("claude", OWNER)
    with patch.object(mod, "claude_signed_in", return_value=False):
        u_cl3, q_cl3 = qupd("logout:claude")
        await mod.cmd_logout_button(u_cl3, ctx())
        check("claude logout clears the stale flag even when claude_signed_in() already says False",
              "claude" not in mod._setup_state())

    # --- Claude: the subcommand fails -> surfaced, not swallowed ---
    with patch.object(mod.subprocess, "run") as run_mock2:
        run_mock2.return_value = SimpleNamespace(returncode=1, stdout="", stderr="some real error")
        with patch.object(mod, "claude_signed_in", return_value=True):
            u_cl2, q_cl2 = qupd("logout:claude")
            await mod.cmd_logout_button(u_cl2, ctx())
            check("a failed claude logout is reported, not silently OK",
                  "failed" in q_cl2.edit_message_text.call_args[0][0].lower()
                  or "gagal" in q_cl2.edit_message_text.call_args[0][0].lower())

    # --- permission gating: a non-owner, non-admin gets refused ---
    u_np = upd()
    u_np.effective_user = SimpleNamespace(id=999)
    await mod.cmd_logout(u_np, ctx())
    check("a non-owner in DM gets silently refused (no card shown)",
          u_np.message.reply_text.call_args is None
          or "which one" not in u_np.message.reply_text.call_args[0][0].lower())

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
