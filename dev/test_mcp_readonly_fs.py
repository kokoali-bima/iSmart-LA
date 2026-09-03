#!/usr/bin/env python3
"""Tests for tools/mcp_readonly_fs.py -- the reference default MCP server
/addmcp recommends. Chosen specifically to match this project's own
principles when asked for a secure, responsive, free default: stdlib only
(no pip install, no npx/Node.js this deployment doesn't already have), pure
local stdio (no external API to pay for or wait on), and a security model
that's one sentence -- everything stays inside the one directory it was
pointed at.

Drives the real subprocess over its actual stdio JSON-RPC transport (one
JSON object per line each way) rather than importing its functions directly,
because the transport framing is exactly the part most likely to be subtly
wrong, and mocking it out would test nothing real. The escape-attempt cases
(`../`, an absolute path, a symlink pointing outside the root) are the ones
that matter most: a "read-only" server that can be walked outside its own
directory is not read-only in any sense that matters.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "tools" / "mcp_readonly_fs.py"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


class Session:
    """One live subprocess, one JSON-RPC id counter."""
    def __init__(self, root: Path):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self.n = 0

    def call(self, method: str, params: dict | None = None, want_reply: bool = True):
        self.n += 1
        req = {"jsonrpc": "2.0", "method": method}
        if want_reply:
            req["id"] = self.n
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        if not want_reply:
            return None
        line = self.proc.stdout.readline()
        return json.loads(line) if line.strip() else None

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        self.proc.wait(timeout=5)


# --- fixture: a small directory tree with something to escape TOWARD -------
sandbox = Path(tempfile.mkdtemp(prefix="isla_mcpfs_"))
root = sandbox / "root"
outside = sandbox / "outside"
root.mkdir()
outside.mkdir()
(root / "notes.txt").write_text("hello from inside the root\n", encoding="utf-8")
(root / "sub").mkdir()
(root / "sub" / "deeper.txt").write_text("nested file\n", encoding="utf-8")
(outside / "secret.txt").write_text("SHOULD NEVER BE READABLE\n", encoding="utf-8")
big = root / "big.txt"
big.write_text("x" * 250_000, encoding="utf-8")   # over the 200,000-byte cap
symlink_out = None
try:
    symlink_out = root / "escape_link"
    symlink_out.symlink_to(outside)
except OSError:
    symlink_out = None  # e.g. no symlink privilege on this OS/account -- case 3 is skipped, not failed

s = Session(root)

# --- 1. handshake ------------------------------------------------------------
init = s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}})
check("initialize responds with the protocol version",
      init and init.get("result", {}).get("protocolVersion") == "2024-11-05")
s.call("notifications/initialized", {}, want_reply=False)

# --- 2. tools/list -----------------------------------------------------------
listed = s.call("tools/list")
tool_names = {t["name"] for t in listed["result"]["tools"]} if listed and "result" in listed else set()
check("tools/list advertises exactly list_files and read_file",
      tool_names == {"list_files", "read_file"})

# --- 3. list_files: root and a subdirectory ---------------------------------
r1 = s.call("tools/call", {"name": "list_files", "arguments": {}})
text1 = r1["result"]["content"][0]["text"]
check("list_files() on the root sees the real entries",
      "notes.txt" in text1 and "sub" in text1 and "big.txt" in text1)
check("...and does NOT leak anything from outside the root",
      "secret.txt" not in text1)

r2 = s.call("tools/call", {"name": "list_files", "arguments": {"path": "sub"}})
check("list_files() into a real subdirectory works",
      "deeper.txt" in r2["result"]["content"][0]["text"])

# --- 4. read_file: the normal case ------------------------------------------
r3 = s.call("tools/call", {"name": "read_file", "arguments": {"path": "notes.txt"}})
check("read_file() returns the real file content",
      "hello from inside the root" in r3["result"]["content"][0]["text"])
check("...with no error flag on a genuinely successful read",
      not r3["result"].get("isError"))

r4 = s.call("tools/call", {"name": "read_file", "arguments": {"path": "sub/deeper.txt"}})
check("read_file() reaches a nested file too",
      "nested file" in r4["result"]["content"][0]["text"])

# --- 5. THE security cases: every way out must be refused -------------------
r5 = s.call("tools/call", {"name": "read_file", "arguments": {"path": "../outside/secret.txt"}})
check("a '../' escape attempt is refused, not answered",
      r5["result"].get("isError") is True
      and "SHOULD NEVER BE READABLE" not in json.dumps(r5))

r6 = s.call("tools/call", {"name": "read_file", "arguments": {"path": str(outside / "secret.txt")}})
check("an absolute path pointing elsewhere is refused",
      r6["result"].get("isError") is True
      and "SHOULD NEVER BE READABLE" not in json.dumps(r6))

if symlink_out is not None:
    r7 = s.call("tools/call", {"name": "read_file", "arguments": {"path": "escape_link/secret.txt"}})
    check("a symlink pointing OUTSIDE the root is refused, not followed "
          "(THE case that matters most for a 'read-only' server)",
          r7["result"].get("isError") is True
          and "SHOULD NEVER BE READABLE" not in json.dumps(r7))
else:
    print("SKIP - could not create a symlink on this OS/account to test escape-via-symlink")

# --- 6. oversized file is refused, not silently truncated -------------------
r8 = s.call("tools/call", {"name": "read_file", "arguments": {"path": "big.txt"}})
check("a file over the size cap is refused with a clear reason, not dumped",
      r8["result"].get("isError") is True and "byte" in r8["result"]["content"][0]["text"])

# --- 7. a nonexistent file/dir fails cleanly, not with a crash -------------
r9 = s.call("tools/call", {"name": "read_file", "arguments": {"path": "nope.txt"}})
check("a missing file is a clean tool error, not a protocol-level crash",
      "error" not in r9 and r9["result"].get("isError") is True)

s.close()

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
