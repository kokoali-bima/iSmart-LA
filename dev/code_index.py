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


def render_symbols(syms: dict, src: Path, version: str) -> str:
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

    out.append("## All names, A–Z")
    out.append("")
    out.append("| name | kind | line | what it is |")
    out.append("|---|---|---|---|")
    for name in sorted(syms, key=str.lower):
        for e in syms[name]:
            doc = e["doc"].replace("|", "\\|") or "—"
            out.append(f"| `{name}{e['sig']}` | {e['kind']} | {e['line']} | {doc} |")
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
    # Only write where the notes live. The first version of this guard checked
    # OUT_DIR.parent.exists() and did not work: on Linux a Windows path like
    # "C:\laragon\..." contains no separators at all, so it is ONE filename,
    # its parent is "." which of course exists, and the run created a directory
    # literally named C:\laragon\www\... inside the checkout -- the exact stray
    # directory the guard was meant to prevent. is_absolute() is the honest
    # test: that Windows path is not absolute on a POSIX host.
    if not OUT_DIR.is_absolute() or not OUT_DIR.parent.exists():
        dupes = [k for k, v in syms.items() if len(v) > 1]
        print(f"index skipped -- {OUT_DIR} is not a usable path on this "
              f"machine; {len(syms)} names, {len(dupes)} duplicate(s)"
              + (f": {', '.join(dupes)}" if dupes else ""))
        sys.exit(1 if dupes else 0)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    (OUT_DIR / "SYMBOLS.md").write_text(
        render_symbols(syms, src, version), encoding="utf-8")

    state_file = OUT_DIR / ".index-state.json"
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

    trace = OUT_DIR / "TRACE.md"
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

    print(f"SYMBOLS.md + TRACE.md updated in {OUT_DIR}")
    print(f"  {len(syms)} names, {len(dupes)} duplicate(s)"
          + (f": {', '.join(dupes)}" if dupes else ""))
    if d["added"] or d["removed"] or d["changed"]:
        print(f"  +{len(d['added'])} -{len(d['removed'])} "
              f"~{len(d['changed'])} since last run")


if __name__ == "__main__":
    main()
