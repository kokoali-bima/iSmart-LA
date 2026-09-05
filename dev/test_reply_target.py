#!/usr/bin/env python3
"""No code reachable from a button or an edited message may use update.message.

Three crashes in one night were the same bug wearing different function names:
offer_unlock, _reply_chunked, and the failure notice inside _run_turn_inner all
reached for update.message, and the update was a callback or an edited message,
where it is None. Fixing the three that happened to fire is not fixing the
class -- an audit afterwards found six more functions one call away from the
same shape, including the two wizards that read update.message.text while
handle_message explicitly admits edited messages (allow_edited=True, then
reads effective_message).

So this walks the call graph from every entry point where update.message is not
guaranteed, and fails if anything reachable from there touches it directly
instead of going through _msg(). It is a whole-class guard, not a spot check:
the next function that gets this wrong fails here before it reaches a chat.
"""
import ast
import sys
from pathlib import Path

SRC = Path(sys.argv[1])
tree = ast.parse(SRC.read_text(encoding="utf-8"))

funcs = {n.name: n for n in tree.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


# Entry points that can arrive WITHOUT update.message: every button handler,
# the PIN keypad, the unlock-and-resume flow, and the turn runner it calls.
roots = [n for n in funcs if n.startswith("cmd_") and ("button" in n or "key" in n)]
roots += ["_do_unlock_and_resume", "_run_turn", "_run_turn_inner",
          "_pin_verified", "offer_unlock", "handle_message"]
roots = sorted(r for r in roots if r in funcs)
check(f"found the unguarded entry points ({len(roots)})", len(roots) >= 8)
check("handle_message is one of them -- it accepts edited messages on purpose",
      "handle_message" in roots)


def callees(fn):
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in funcs:
                out.add(f.id)
            elif isinstance(f, ast.Attribute) and f.attr in funcs:
                out.add(f.attr)
    return out


seen, stack = set(), list(roots)
while stack:
    cur = stack.pop()
    if cur in seen:
        continue
    seen.add(cur)
    stack.extend(callees(funcs[cur]) - seen)
check(f"walked the call graph from them ({len(seen)} functions reachable)",
      len(seen) > 50)


def direct_uses(fn):
    """update.message.<attr> -- used as an object, not merely tested for None."""
    hits = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "message"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "update"):
            hits.append((node.lineno, node.attr))
    return hits


risky = []
for name in sorted(seen):
    hits = direct_uses(funcs[name])
    if hits:
        risky.append((name, hits))

if risky:
    print()
    for name, hits in risky:
        where = ", ".join(f"L{ln}:.{a}" for ln, a in hits[:5])
        print(f"       {name}: {where}")
    print()
check("nothing reachable from an unguarded entry point touches "
      "update.message directly", not risky)

# The helper itself must keep covering all three shapes, or the guard above is
# passing everything through a hole.
msg_src = ast.get_source_segment(SRC.read_text(encoding="utf-8"), funcs["_msg"]) or ""
check("_msg still handles a typed message", "update.message is not None" in msg_src)
check("_msg still handles a button callback", "callback_query" in msg_src)
check("_msg still handles an edited message", "effective_message" in msg_src)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
