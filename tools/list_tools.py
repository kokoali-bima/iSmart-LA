#!/usr/bin/env python3
"""
list_tools -- print the graduated-skill registry compactly.

Read this BEFORE solving an infrastructure question: if one of these already
answers it, run that script instead of re-deriving the answer with a dozen
exploratory tool calls. That is the whole point of graduating a skill (see
/graduate in lite_agent.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent / "registry.json"


def main() -> None:
    if not REGISTRY.exists():
        print("(registry is empty -- no skills have been graduated yet)")
        return
    try:
        data = json.loads(REGISTRY.read_text())
    except Exception as exc:
        print(f"ERROR: registry.json is unreadable: {exc}", file=sys.stderr)
        sys.exit(1)

    tools = data.get("tools", [])
    if not tools:
        print("(registry is empty -- no skills have been graduated yet)")
        return

    print(f"GRADUATED SKILLS ({len(tools)}) -- use these before manual exploration:\n")
    for t in tools:
        print(f"* {t['name']}")
        print(f"    run:      {t['usage']}")
        print(f"    does:     {t['description']}")
        answers = t.get("answers") or []
        if answers:
            print(f"    answers:  {'; '.join(answers)}")
        print()


if __name__ == "__main__":
    main()
