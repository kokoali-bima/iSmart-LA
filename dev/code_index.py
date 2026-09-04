#!/usr/bin/env python3
"""Write a traceable index of this codebase, and append what changed since
the last time it ran.

Built after a real incident: a new helper named `_msg(lang, detail)` was added
without noticing that `_msg(update)` had existed for weeks and is called by
every reply path. Python simply replaced it. Every message the bot sent then
failed with "TypeError: _msg() missing 1 required positional argument", twice
per turn, and the delivery retry made it look like slowness rather than an
error -- it surfaced only because a concurrency test measured how long a turn
took. Nothing about writing that function looked wrong at the time, which is
the whole problem: a name that is already taken is invisible at the point you
type it.

So this produces two things:

  SYMBOLS.md -- every function and module constant: name, line, signature,
                and the first line of its docstring. Sorted by name, so
                checking whether a name is free is one search. DUPLICATES ARE
                LISTED FIRST, loudly, because that is the failure this exists
                to prevent.

  TRACE.md   -- appended, never rewritten: one dated section per run listing
                what was added, removed, moved or re-signed since the previous
                one. A running log of the code's shape over time, so a later
                change can be traced back to when and why a thing appeared.

Run it after every change:  python3 dev/code_index.py lite_agent.py
"""
import ast
import datetime as _dt
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path(os.environ.get(
    "ISLA_INDEX_DIR", r"C:\laragon\www\infrasoft\lab\home-ai\ismart-la"))


def _sig(node) -> str:
    a = node.args
    parts = [x.arg for x in list(a.posonlyargs) + list(a.args)]
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    if a.kwonlyargs:
        if not a.vararg:
            parts.append("*")
        parts += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return f"({', '.join(parts)})"


def _summary(node) -> str:
    doc = ast.get_docstring(node) or ""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first[:150]


def collect(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    syms = {}
    order = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            key = node.name
            entry = {"kind": kind, "line": node.lineno, "sig": _sig(node),
                     "doc": _summary(node)}
        elif isinstance(node, ast.ClassDef):
            key = node.name
            entry = {"kind": "class", "line": node.lineno, "sig": "",
                     "doc": _summary(node)}
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = ([node.target] if isinstance(node, ast.AnnAssign)
                       else node.targets)
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not names:
                continue
            for n in names:
                if n.isupper() or n.startswith("_") and n.upper() == n.lstrip("_").upper():
                    syms.setdefault(n, []).append(
                        {"kind": "const", "line": node.lineno, "sig": "", "doc": ""})
                    order.append(n)
            continue
        else:
            continue
        syms.setdefault(key, []).append(entry)
        order.append(key)
    return syms


def call_graph(path: Path, syms: dict) -> dict:
    """Who calls what, among this file's own top-level names.

    The index answered "does this name exist" but not "what happens if I
    change it", which is the question you actually have when picking a
    function apart. Without it, tracing means reading -- and reading 9,694
    lines is exactly what this whole exercise is trying to avoid.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    known = set(syms)
    callers = {n: set() for n in known}
    calls = {n: set() for n in known}
    # Module-level code counts as a caller too, under the name "<module>".
    # Without this the orphan list cried wolf on its very first run:
    # _parse_tiers, _tier_summary and _load_allowed_groups_file are all used
    # at import time, and a list that reports healthy code as dead is a list
    # people stop reading.
    module_body = [n for n in tree.body
                   if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for stmt in module_body:
        for node in ast.walk(stmt):
            name = None
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            if name and name in known:
                callers[name].add("<module>")

    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            name = None
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                # A function passed by reference (a callback, a patch target)
                # is a use too -- and those are the ones a rename breaks
                # silently, because nothing calls them by name at that point.
                name = node.id
            if name and name in known and name != fn.name:
                callers[name].add(fn.name)
                calls[fn.name].add(name)
    return {"callers": callers, "calls": calls}


def area_of(name: str) -> str:
    """Group by the prefix the codebase already uses, so the map reflects how
    the file is really organised rather than an ordering imposed on it."""
    n = name.lstrip("_").lower()
    for area, keys in (
        ("Google Drive", ("gdrive", "rclone", "drive_")),
        ("MCP", ("mcp",)),
        ("Sign-in / CLI", ("agy", "claude", "cli_login", "login", "logout", "reauth")),
        ("Write gate / PIN", ("pin", "unlock", "lock", "write_mode", "guard",
                              "secure_server", "snapshot")),
        ("Sessions / memory", ("session", "memory", "learned", "remember", "graduate")),
        ("Telegram plumbing", ("reply", "tg_", "telegram", "chunk", "msg", "media")),
        ("Servers / schedules", ("server", "schedule", "cron", "boundar")),
        ("Update / hardening", ("update", "harden", "systemd", "refresh_")),
        ("Spend / ledger", ("ledger", "spend", "token", "cost")),
    ):
        if any(k in n for k in keys):
            return area
    if n.startswith("cmd_"):
        return "Commands"
    return "Core / other"


def render_symbols(syms: dict, src: Path, version: str, graph: dict = None) -> str:
    dupes = {k: v for k, v in syms.items() if len(v) > 1}
    out = [f"# Symbol index — {src.name}",
           "",
           f"Generated {_dt.datetime.now():%Y-%m-%d %H:%M} · {version} · "
           f"{sum(len(v) for v in syms.values())} top-level names",
           "",
           "Search this file before naming a new function or constant. A name "
           "that is already taken is invisible at the point you type it — "
           "Python just replaces the old one, and the failure surfaces "
           "somewhere else entirely.",
           ""]

    out.append("## ⚠️ Duplicate names")
    out.append("")
    if dupes:
        out.append("**Defined more than once. The later definition wins and "
                   "the earlier one is silently gone.**")
        out.append("")
        for name, entries in sorted(dupes.items()):
            where = ", ".join(f"line {e['line']} `{name}{e['sig']}`" for e in entries)
            out.append(f"- **`{name}`** — {where}")
    else:
        out.append("None. Every top-level name is defined exactly once.")
    out.append("")

    callers = (graph or {}).get("callers", {})

    # Never referenced anywhere in this file. Entry points reached from
    # outside belong here (handlers, main) -- but a helper nobody calls is
    # either dead, or was orphaned by an edit that only half-landed. That
    # happened here this week: `gdrive_ops` was used by a line that went in
    # while the line defining it never did.
    orphans = sorted(n for n, c in callers.items()
                     if not c and not n.startswith("cmd_")
                     and n != "main" and syms[n][0]["kind"] != "const")
    out.append("## Referenced by nothing in this file")
    out.append("")
    if orphans:
        out.append("Handlers and `main` are reached from outside and belong "
                   "here. Anything else is either dead, or was orphaned by an "
                   "edit that only half-landed.")
        out.append("")
        out.append(", ".join(f"`{n}`" for n in orphans))
    else:
        out.append("None.")
    out.append("")

    out.append("## By area")
    out.append("")
    out.append("Grouped by what each name belongs to, so a change can start "
               "from its own neighbourhood instead of from line 1. The "
               "**used by** column answers the question you actually have "
               "when picking something apart: what breaks if I change this.")
    out.append("")
    areas = {}
    for name in syms:
        areas.setdefault(area_of(name), []).append(name)
    for area in sorted(areas):
        names = sorted(areas[area], key=str.lower)
        out.append(f"### {area} ({len(names)})")
        out.append("")
        out.append("| name | line | used by | what it is |")
        out.append("|---|---|---|---|")
        for name in names:
            for e in syms[name]:
                doc = e["doc"].replace("|", "\\|") or "—"
                used = callers.get(name, set())
                if not used:
                    who = "—"
                elif len(used) <= 3:
                    who = ", ".join(f"`{u}`" for u in sorted(used))
                else:
                    who = f"{len(used)} callers"
                out.append(f"| `{name}{e['sig']}` | {e['line']} | {who} | {doc} |")
        out.append("")
    return "\n".join(out)


def diff(prev: dict, now: dict) -> dict:
    added = sorted(set(now) - set(prev))
    removed = sorted(set(prev) - set(now))
    changed = []
    for name in sorted(set(prev) & set(now)):
        p, n = prev[name][0], now[name][0]
        if p.get("sig") != n.get("sig"):
            changed.append((name, p.get("sig", ""), n.get("sig", "")))
    moved = [name for name in sorted(set(prev) & set(now))
             if prev[name][0].get("line") != now[name][0].get("line")]
    return {"added": added, "removed": removed, "changed": changed,
            "moved": len(moved)}


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "lite_agent.py")
    if not src.exists():
        print(f"no such file: {src}")
        sys.exit(2)
    try:
        version = subprocess.run(["git", "describe", "--tags", "--always"],
                                 capture_output=True, text=True,
                                 cwd=str(src.parent)).stdout.strip() or "(no tag)"
    except Exception:
        version = "(no git)"

    syms = collect(src)
    # Where to write. The configured path is the operator's notes folder on
    # Windows; is_absolute() is the honest test for "usable here", because on
    # Linux a Windows path like "C:\laragon\..." contains no separators at
    # all, so it is ONE filename whose parent is "." -- an earlier guard
    # checked only parent.exists() and cheerfully created a directory literally
    # named C:\laragon\www\... inside the checkout.
    #
    # It used to SKIP when the path was unusable. That quietly disabled the
    # whole index on Linux -- which is the machine the full suite actually runs
    # on, so the duplicate-name check that this tool exists for was doing
    # nothing exactly where it mattered. Now it falls back into the checkout
    # instead: the index always gets built, and the notes copy is a bonus.
    out_dir, where = OUT_DIR, "notes"
    if not OUT_DIR.is_absolute() or not OUT_DIR.parent.exists():
        out_dir, where = src.parent / "dev" / "index", "fallback"
    out_dir.mkdir(parents=True, exist_ok=True)

    graph = call_graph(src, syms)
    (out_dir / "SYMBOLS.md").write_text(
        render_symbols(syms, src, version, graph), encoding="utf-8")

    state_file = out_dir / ".index-state.json"
    prev = {}
    if state_file.exists():
        try:
            prev = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    d = diff(prev, syms)
    body = src.read_text(encoding="utf-8")
    digest = hashlib.sha256(body.encode()).hexdigest()[:12]

    if prev and not (d["added"] or d["removed"] or d["changed"]):
        note = "no top-level names added, removed or re-signed"
    else:
        note = ""

    trace = out_dir / "TRACE.md"
    if not trace.exists():
        trace.write_text(
            "# Trace — every change to this codebase's shape\n\n"
            "Appended, never rewritten. One section per run of "
            "`dev/code_index.py`, so a name can be traced back to when it "
            "appeared and what it replaced.\n\n", encoding="utf-8")

    lines = [f"## {_dt.datetime.now():%Y-%m-%d %H:%M} · {version} · "
             f"{len(syms)} names · sha {digest}", ""]
    if not prev:
        lines.append(f"First index. {len(syms)} top-level names recorded as the baseline.")
    elif note:
        lines.append(note + ".")
    else:
        if d["added"]:
            lines.append(f"**Added ({len(d['added'])}):** "
                         + ", ".join(f"`{n}`" for n in d["added"]))
        if d["removed"]:
            lines.append(f"**Removed ({len(d['removed'])}):** "
                         + ", ".join(f"`{n}`" for n in d["removed"]))
        if d["changed"]:
            lines.append("**Signature changed:** " + ", ".join(
                f"`{n}{a}` → `{n}{b}`" for n, a, b in d["changed"]))
        if d["moved"]:
            lines.append(f"_{d['moved']} existing name(s) moved line._")
    dupes = [k for k, v in syms.items() if len(v) > 1]
    if dupes:
        lines.append("")
        lines.append("**⚠️ DUPLICATE NAMES: " + ", ".join(f"`{k}`" for k in dupes)
                     + " — the later definition silently replaces the earlier one.**")
    lines.append("")
    with io.open(trace, "a", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")

    state_file.write_text(json.dumps(syms, indent=1), encoding="utf-8")

    print(f"SYMBOLS.md + TRACE.md updated in {out_dir}"
          + (" (fallback -- notes folder not reachable here)"
             if where == "fallback" else ""))
    print(f"  {len(syms)} names, {len(dupes)} duplicate(s)"
          + (f": {', '.join(dupes)}" if dupes else ""))
    if d["added"] or d["removed"] or d["changed"]:
        print(f"  +{len(d['added'])} -{len(d['removed'])} "
              f"~{len(d['changed'])} since last run")
    # Exit 3 -- and ONLY 3 -- when there are duplicate names, so the caller can
    # tell "this codebase has a collision" apart from "this tool fell over".
    # Those must not be conflated: a missing or broken index tool has nothing
    # to say about the code, and failing the test run over it would stop
    # everything for a reason that is not a defect in the product.
    DUPLICATE_EXIT = 3
    if dupes:
        sys.exit(DUPLICATE_EXIT)


if __name__ == "__main__":
    main()
