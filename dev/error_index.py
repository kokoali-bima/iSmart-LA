#!/usr/bin/env python3
"""Render ERRORS.md, and refuse to pass if a past failure lost its guard.

The symbol index answers "does this name exist and who calls it". This answers
a different question, and the one that keeps costing real nights: "has this
gone wrong before, and what stops it happening again?"

The rendering is the small half. The useful half is the check: every entry in
known_errors.py names a guard test, and if that file is missing this exits
non-zero. A test can be deleted or renamed in a refactor without anyone
noticing that a shipped bug just lost the only thing standing between it and a
second appearance -- that is the failure this tool exists to prevent.

Written to the same notes folder as SYMBOLS.md and TRACE.md, falling back into
the checkout when that folder is not reachable on this machine (the Linux host
runs the suite and cannot see a Windows path).

  python3 dev/error_index.py lite_agent.py
"""
import datetime as _dt
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from known_errors import ERRORS
except Exception as exc:            # pragma: no cover - import-time only
    print(f"could not load known_errors.py: {exc}")
    sys.exit(2)

OUT_DIR = Path(os.environ.get(
    "ISLA_INDEX_DIR", r"C:\laragon\www\infrasoft\lab\home-ai\ismart-la"))
DUPLICATE_EXIT = 3          # same convention as code_index.py
MISSING_GUARD_EXIT = 3


def out_dir() -> tuple[Path, str]:
    if OUT_DIR.is_absolute() and OUT_DIR.parent.exists():
        return OUT_DIR, "notes"
    return HERE / "index", "fallback"


def render(errors, missing) -> str:
    by_area = Counter(e["area"] for e in errors)
    lines = [
        "# Error history — iSmart-LA",
        "",
        f"Generated {_dt.datetime.now():%Y-%m-%d %H:%M} · {len(errors)} shipped "
        f"failures across {len(by_area)} areas",
        "",
        "Every entry here reached a user or a production host. Each names the "
        "test that would fail if it came back; `error_index.py` fails the run "
        "when one of those tests goes missing.",
        "",
    ]
    if missing:
        lines += [
            "## ⚠️ UNGUARDED",
            "",
            "These past failures no longer have the test that was guarding them. "
            "Restore the test or write a new one before shipping.",
            "",
        ]
        for e in missing:
            lines.append(f"- **{e['id']}** ({e['area']}) — guard `{e['guard']}` is missing")
        lines.append("")

    lines += ["## By area", ""]
    for area, n in by_area.most_common():
        lines.append(f"- **{area}** — {n}")
    lines.append("")

    lines += ["## Entries", ""]
    for e in sorted(errors, key=lambda x: x["id"], reverse=True):
        lines += [
            f"### {e['id']} · {e['area']} · {e['date']} · {e.get('release', '—')}",
            "",
            f"**Seen:** {e['symptom']}",
            "",
            f"**Cause:** {e['cause']}",
            "",
            f"**Fixed by:** {e['fix']}",
            "",
            f"**Guard:** `{e['guard']}`",
            "",
        ]
    return "\n".join(lines) + "\n"


def main() -> None:
    dev = HERE
    missing = []
    for e in ERRORS:
        guard = str(e.get("guard") or "")
        if guard.startswith("none:"):
            continue
        if not (dev / guard).exists():
            missing.append(e)

    target, where = out_dir()
    target.mkdir(parents=True, exist_ok=True)
    (target / "ERRORS.md").write_text(render(ERRORS, missing), encoding="utf-8")

    print(f"ERRORS.md updated in {target}"
          + (" (fallback)" if where == "fallback" else ""))
    areas = Counter(e["area"] for e in ERRORS)
    print(f"  {len(ERRORS)} shipped failures, {len(areas)} areas, "
          f"{len(missing)} unguarded")
    if missing:
        for e in missing:
            print(f"  UNGUARDED {e['id']} ({e['area']}) -- "
                  f"guard {e['guard']} not found")
        sys.exit(MISSING_GUARD_EXIT)


main()
