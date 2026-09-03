#!/usr/bin/env python3
"""A minimal, read-only, path-locked MCP server -- stdlib only, no pip
install, no npx, no Node.js. The reference default for /addmcp, chosen
specifically to match this project's own principles: it costs nothing to
run (pure local stdio, no external API), it's fast (no network round-trip),
and its security model is one sentence -- everything stays inside the one
directory it was pointed at, nothing outside it is ever reachable, and
nothing is ever written.

Usage (this is exactly what /addmcp's own help text recommends):

    /addmcp reports python3 tools/mcp_readonly_fs.py /root/lite-agent/reports

Gives the model two tools, "list_files" and "read_file", scoped to whatever
directory is passed as argv[1]. Every path resolves to a real, absolute
path and is checked against the root with Path.is_relative_to() -- a
symlink pointing outside the root, or a ".." segment, is refused the same
way a path that's simply wrong is: politely, in the tool's own error
result, never as a crash.

Speaks MCP's stdio JSON-RPC transport: one JSON object per line on stdin,
one JSON object per line on stdout. No third-party MCP SDK -- the protocol
surface this needs (initialize, notifications/initialized, tools/list,
tools/call) is small enough that depending on one would be exactly the kind
of extra layer this project's own README argues against.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
MAX_READ_BYTES = 200_000  # a generous single file, not a way to exfiltrate a whole tree
MAX_LIST_ENTRIES = 500


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(rid, payload: dict) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "result": payload})


def _error(rid, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _tool_error(rid, message: str) -> None:
    # A tool-level failure (bad path, file too big) is reported INSIDE a
    # successful JSON-RPC result with isError -- not a protocol-level error --
    # so the model sees it as "that didn't work, try something else" rather
    # than the tool call itself blowing up.
    _result(rid, {"content": [{"type": "text", "text": message}], "isError": True})


def _safe_path(root: Path, rel: str) -> Path | None:
    """None if `rel` would ever resolve outside `root` -- a ".." segment, an
    absolute path elsewhere, or a symlink that escapes. Never raises."""
    try:
        candidate = (root / rel).resolve()
        root_resolved = root.resolve()
        if candidate == root_resolved or candidate.is_relative_to(root_resolved):
            return candidate
    except (OSError, ValueError):
        pass
    return None


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: mcp_readonly_fs.py <root-directory>", file=sys.stderr)
        sys.exit(2)
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    TOOLS = [
        {
            "name": "list_files",
            "description": (
                f"List files and folders under {root} (or a subdirectory of it). "
                "Never reaches outside this one directory."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Subdirectory to list, relative to the root. Empty or omitted lists the root itself."},
                },
            },
        },
        {
            "name": "read_file",
            "description": (
                f"Read one text file's contents from inside {root}. Refuses anything "
                f"outside that directory, and anything over {MAX_READ_BYTES:,} bytes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "File path, relative to the root."},
                },
                "required": ["path"],
            },
        },
    ]

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")

        if method == "initialize":
            _result(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "isla-readonly-fs", "version": "1.0"},
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _result(rid, {"tools": TOOLS})
        elif method == "tools/call":
            params = req.get("params", {}) or {}
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            rel = args.get("path", "") or ""

            if name == "list_files":
                target = _safe_path(root, rel)
                if target is None:
                    _tool_error(rid, f"'{rel}' is outside the allowed directory -- refused.")
                elif not target.is_dir():
                    _tool_error(rid, f"'{rel}' is not a directory (or does not exist).")
                else:
                    entries = sorted(target.iterdir())[:MAX_LIST_ENTRIES]
                    lines = [f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in entries]
                    _result(rid, {"content": [{"type": "text",
                                              "text": "\n".join(lines) or "(empty)"}]})
            elif name == "read_file":
                target = _safe_path(root, rel)
                if target is None:
                    _tool_error(rid, f"'{rel}' is outside the allowed directory -- refused.")
                elif not target.is_file():
                    _tool_error(rid, f"'{rel}' is not a file (or does not exist).")
                elif target.stat().st_size > MAX_READ_BYTES:
                    _tool_error(rid, f"'{rel}' is {target.stat().st_size:,} bytes, over the "
                                     f"{MAX_READ_BYTES:,}-byte limit -- too large to read whole.")
                else:
                    try:
                        text = target.read_text(encoding="utf-8", errors="replace")
                    except OSError as exc:
                        _tool_error(rid, f"could not read '{rel}': {exc}")
                    else:
                        _result(rid, {"content": [{"type": "text", "text": text}]})
            else:
                _error(rid, -32601, f"unknown tool: {name}")
        elif method is not None and rid is not None:
            _error(rid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()
