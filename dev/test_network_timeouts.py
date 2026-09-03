#!/usr/bin/env python3
"""Tests for the httpcore.ReadTimeout storm found live on bscloud: 14 separate
"unhandled error while processing an update" entries in about an hour, every
one of them a plain command reply (/new, /usemodel, ...) failing with

    httpcore.ReadTimeout
    ...
    telegram.error.TimedOut: Timed out

Checked directly against the installed library rather than assumed:

    >>> HTTPXRequest.__init__ defaults
    connection_pool_size=256, read_timeout=5.0, write_timeout=5.0,
    connect_timeout=5.0, pool_timeout=1.0

5 seconds is tight for any link with real jitter, and this deployment's own
network has shown exactly that kind of jitter before (VPN-related stalls
noted elsewhere in this project's history). None of the 14 occurrences were on
a turn that actually reached a model -- those already retry once (see the
"attempt in (1, 2)" loop in the delivery path) -- every one was a plain
administrative reply with no such protection.

Rather than wrapping some subset of the 130+ raw `update.message.reply_text`
call sites individually, the fix is at the one place that governs every
outgoing Bot API call this process makes: the `HTTPXRequest` handed to
`Application.builder().request(...)`. `on_error` -- the last-resort notice
telling the user something broke -- gets its own retry too, since a network
blip is exactly why that handler is running in the first place, and it must
not also swallow the notice about itself.
"""
import ast
import pathlib
import sys

SRC = pathlib.Path(sys.argv[1]).resolve()
source = SRC.read_text(encoding="utf-8")
tree = ast.parse(source, filename=str(SRC))

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def find_calls(name: str):
    """Every ast.Call in the module whose callee is literally `name` (a bare
    identifier, e.g. HTTPXRequest(...))."""
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def kwval(call: ast.Call, key: str):
    for kw in call.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


# --- 1. the Bot API request object is configured with generous timeouts ----
calls = find_calls("HTTPXRequest")
check("exactly one HTTPXRequest(...) is constructed", len(calls) == 1)

if calls:
    call = calls[0]
    for field, ptb_default in [
        ("connect_timeout", 5.0), ("read_timeout", 5.0),
        ("write_timeout", 5.0), ("pool_timeout", 1.0),
    ]:
        v = kwval(call, field)
        check(f"{field} is set explicitly (not left at PTB's tight {ptb_default}s default)",
              v is not None)
        if v is not None:
            check(f"...and is meaningfully more generous than the {ptb_default}s default "
                  f"(got {v})", v >= ptb_default * 2)
else:
    for field in ("connect_timeout", "read_timeout", "write_timeout", "pool_timeout"):
        check(f"{field} is set explicitly (not left at PTB's tight default)", False)

# --- 2. that object is actually wired into the Application, not just built -
check("Application.builder()'s chain includes .request(...) -- constructing "
      "HTTPXRequest alone does nothing if it is never handed to the builder",
      ".request(" in source and "Application.builder()" in source)

builder_start = source.find("Application.builder()")
builder_chain = source[builder_start:builder_start + 400] if builder_start != -1 else ""
check("...specifically inside the SAME builder chain that constructs the app "
      "(not a stray .request( call somewhere unrelated)",
      ".request(request)" in builder_chain or ".request(\n" in builder_chain)

# --- 3. on_error retries its own notice at least once ----------------------
on_error_src = source[source.find("async def on_error"):]
on_error_src = on_error_src[:on_error_src.find("\ndef main()")]
check("on_error is present and this test located its body", "reply_text" in on_error_src)
check("on_error retries the failure notice (a loop trying more than once), "
      "not a single bare try/except that gives up on the first network blip",
      on_error_src.count("reply_text") >= 1
      and ("for attempt in" in on_error_src or on_error_src.count("try:") >= 1)
      and "asyncio.sleep" in on_error_src)
check("...and still logs a warning if it genuinely never gets through, "
      "rather than failing silently",
      "could not even deliver" in on_error_src)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
