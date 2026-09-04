#!/usr/bin/env python3
"""Tests for /setchatscope -- a role per chat, on one deployment.

/setscope is one shared setting by design, and for "what is this bot for" that
is right. It cannot serve the case this exists for: one deployment where a
network-engineering group and a research group talk to the same bot and need
genuinely different roles.

THE DESIGN DECISION UNDER TEST is that this is a LAYER, not a per-chat copy of
the brief. Hard boundaries live INSIDE the brief -- write_boundaries() rewrites
the bullet list in SOUL.md and GEMINI.md -- so a per-chat brief file that
REPLACED them would mean /addboundary silently not reaching any chat that had
one, with nothing to indicate it. The shape here cannot have that bug: the
shared brief is still sent in full every turn, and the chat's role is appended
after it. Several tests below exist only to keep it that way, because the
replacement design is the obvious one to reach for and would pass any test that
only checked "the role differs per chat".

The isolation half follows the rule /remember already established (v0.2b.49,
MEMORY.md going per-chat): a chat id that is not an optionally-negative integer
never becomes a filename, and a write with no usable id is REFUSED rather than
falling back to somewhere shared -- which would hand one room's role to every
other, the exact leak that release fixed.

Behavioural, not just structural: the module is imported against a scratch HOME
and BASE_DIR, real files are written, and the prompt actually handed to agy is
inspected. The Claude side is checked at the same level by reading what
_run_claude_once() would pass, with subprocess.run patched.
"""
import ast
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile

SRC = pathlib.Path(sys.argv[1]).resolve()

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


# --- module under test, on a scratch install --------------------------------
scratch = pathlib.Path(tempfile.mkdtemp(prefix="isla_scope_"))
import atexit
atexit.register(shutil.rmtree, scratch, ignore_errors=True)

work = scratch / "install"
work.mkdir()
shutil.copy(SRC, work / SRC.name)
# The module puts its own tools/ on sys.path and imports cli_login from it, so
# a scratch install needs one. Copied rather than pointed at the real tree: the
# whole point of this scratch dir is that BASE_DIR is not the repo.
shutil.copytree(SRC.parent / "tools", work / "tools")
(work / "SOUL.md").write_text(
    "You are an infrastructure assistant for ACME.\n\n"
    "## HARD BOUNDARIES -- never do these without explicit human confirmation:\n"
    "- never delete a production VM\n",
    encoding="utf-8")
(work / "GEMINI.md").write_text(
    "You are an infrastructure assistant for ACME.\n\n"
    "## HARD BOUNDARIES -- never do these without explicit human confirmation:\n"
    "- never delete a production VM\n",
    encoding="utf-8")

os.environ["HOME"] = str(scratch / "home")
os.environ["USERPROFILE"] = str(scratch / "home")
(scratch / "home").mkdir()
os.environ["TELEGRAM_BOT_TOKEN"] = "t"
os.environ["ALLOWED_USER_IDS"] = "1"
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la_scope", work / SRC.name)
la = importlib.util.module_from_spec(spec)
sys.modules["la_scope"] = la
spec.loader.exec_module(la)

OPS, RESEARCH = "-100111", "-100222"

# Checked before anything calls them. Without this the suite dies on the first
# AttributeError when run against a source that has no per-chat scope at all --
# which is exactly the run that has to be readable, since a detector nobody can
# read in the failing direction is not a detector.
REQUIRED = ("CHAT_SCOPE_DIR", "_chat_scope_file", "chat_scope_text",
            "set_chat_scope", "clear_chat_scope", "_chat_scope_block")
for _name in REQUIRED:
    check(f"the module provides {_name}", hasattr(la, _name))
if [n for n in REQUIRED if not hasattr(la, n)]:
    _failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(_failed)}/{len(results)} passed")
    print("FAILED:", _failed)
    print("per-chat scope is absent from this source -- every remaining check "
          "needs it, so they cannot be evaluated")
    sys.exit(1)

# --- 1. round-trip and isolation -------------------------------------------
check("a chat with nothing set has no role of its own", la.chat_scope_text(OPS) == "")
check("setting a chat's role reports success", la.set_chat_scope(OPS, "network engineer") is True)
check("...and reads back", la.chat_scope_text(OPS) == "network engineer")
check("another chat is unaffected by it", la.chat_scope_text(RESEARCH) == "")
la.set_chat_scope(RESEARCH, "ML research assistant")
check("two chats hold different roles at once",
      la.chat_scope_text(OPS) == "network engineer"
      and la.chat_scope_text(RESEARCH) == "ML research assistant")
check("each is a separate file under chatscope/",
      (la.CHAT_SCOPE_DIR / f"{OPS}.md").exists()
      and (la.CHAT_SCOPE_DIR / f"{RESEARCH}.md").exists())
check("the directory is owner-only (it holds one room's role, like memory/)",
      os.name == "nt" or (la.CHAT_SCOPE_DIR.stat().st_mode & 0o077) == 0)

# --- 2. a bad chat id is refused, never redirected somewhere shared ---------
for bad in ("../../etc/passwd", "abc", "", None, "12; rm -rf /", "1.5"):
    ok = la.set_chat_scope(bad, "anything")
    check(f"a write with an unusable chat id ({bad!r}) is refused, not redirected", ok is False)
check("...and no stray file appeared from any of them",
      sorted(p.name for p in la.CHAT_SCOPE_DIR.glob("*")) == [f"{OPS}.md", f"{RESEARCH}.md"])
check("reading with an unusable id returns nothing rather than someone else's",
      la.chat_scope_text("../-100111") == "" and la.chat_scope_text("abc") == "")

# --- 3. what actually reaches the model ------------------------------------
ops_prompt = la._build_agy_prompt("check node 3", include_env=True, chat_id=OPS)
res_prompt = la._build_agy_prompt("check node 3", include_env=True, chat_id=RESEARCH)
check("the chat's role reaches agy's prompt", "network engineer" in ops_prompt)
check("...and the OTHER chat's role does not", "ML research assistant" not in ops_prompt)
check("the other chat gets its own", "ML research assistant" in res_prompt
      and "network engineer" not in res_prompt)

# The point of the whole design: the shared brief is still there, in full.
check("the shared brief is STILL sent alongside it",
      "assistant for ACME" in ops_prompt)
check("...including the hard boundaries, which a per-chat brief FILE would have "
      "replaced and silently dropped",
      "never delete a production VM" in ops_prompt)
check("the injected block says the boundaries are not relaxed by it",
      "HARD BOUNDARIES" in ops_prompt and "never relaxed" in ops_prompt)
check("...and says it takes precedence over the shared role, so the model is not "
      "left to guess between two role sentences",
      "takes precedence" in ops_prompt)

# Every turn, not only conversation-opening ones -- otherwise changing a room's
# role would apply to whichever conversation happens to start next.
resumed = la._build_agy_prompt("and node 4?", include_env=False, chat_id=OPS)
check("it is sent on a RESUMED turn too, so a role change applies immediately",
      "network engineer" in resumed)
check("...while the ~2.5k-token brief is still only sent when opening one",
      "assistant for ACME" not in resumed)

check("a chat with no role set gets no block at all (no empty scaffolding)",
      "takes precedence" not in la._build_agy_prompt("hi", include_env=True, chat_id="-100999"))

# --- 3b. THE property this design exists for, proved rather than asserted ---
# A boundary added AFTER a chat has its own role must still reach that chat.
# Under a per-chat brief FILE this is precisely what would silently stop
# happening: write_boundaries() rewrites SOUL.md/GEMINI.md, and a chat reading
# its own copy would never see the new rule. So it is checked by adding one for
# real and looking at what that chat's next prompt actually contains.
la.write_boundaries(["never delete a production VM",
                     "never touch the billing database"])
after = la._build_agy_prompt("check node 3", include_env=True, chat_id=OPS)
check("a boundary added AFTER this chat got its own role still reaches it",
      "never touch the billing database" in after)
check("...and the chat's own role survived that rewrite",
      "network engineer" in after)
check("...and the boundary reaches a chat with NO role of its own too",
      "never touch the billing database"
      in la._build_agy_prompt("hi", include_env=True, chat_id="-100999"))
check("a turn with no chat id at all is unaffected",
      "takes precedence" not in la._build_agy_prompt("hi", include_env=True, chat_id=None))

# --- 4. the Claude side carries it too, or the cheap tier alone would --------
captured = {}
class _Proc:
    returncode = 0
    stdout = '{"result": "ok"}'
    stderr = ""
def _fake_run(cmd, **kw):
    captured["cmd"] = cmd
    return _Proc()
la.subprocess.run = _fake_run
la._run_claude_once("check node 3", None, "s", "m", chat_id=OPS)
cmd = captured.get("cmd", [])
check("claude is still handed the SHARED brief file, not a per-chat one",
      "--system-prompt-file" in cmd
      and cmd[cmd.index("--system-prompt-file") + 1].endswith("SOUL.md"))
appended = cmd[cmd.index("--append-system-prompt") + 1] if "--append-system-prompt" in cmd else ""
check("the chat's role rides in --append-system-prompt", "network engineer" in appended)
check("...and the other chat's role does not", "ML research assistant" not in appended)

# --- 5. clearing --------------------------------------------------------------
check("clearing a chat that has one reports it", la.clear_chat_scope(OPS) is True)
check("...and it really is gone", la.chat_scope_text(OPS) == "")
check("...leaving the other chat alone", la.chat_scope_text(RESEARCH) == "ML research assistant")
check("clearing a chat that has none is a no-op, not an error",
      la.clear_chat_scope("-100999") is False)

# --- 6. structural: the layer must not quietly become a replacement ----------
source = SRC.read_text(encoding="utf-8")
tree = ast.parse(source, filename=str(SRC))

def func(name, *, code_only=False):
    """Unparsed source of one function. code_only drops the docstring, so a
    check on what the code DOES is not satisfied (or broken) by prose that
    merely mentions the same name -- which is exactly what happened here on
    the first run: _brief_files()'s docstring names CHAT_SCOPE_DIR to explain
    why it deliberately does NOT use it."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            body = n.body
            if code_only and body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return "\n".join(ast.unparse(s) for s in body)
    return ""

brief_files = func("_brief_files", code_only=True)
check("_brief_files() -- what /addboundary and append_learned() write to -- still "
      "returns only the two shared briefs",
      "SYSTEM_PROMPT_FILE" in brief_files and "GEMINI_PROMPT_FILE" in brief_files
      and "CHAT_SCOPE_DIR" not in brief_files)
check("the per-chat role is never passed as --system-prompt-file",
      "--system-prompt-file" in source
      and "_chat_scope_file" not in func("_run_claude_once", code_only=True))
check("chatscope/ is hardened alongside memory/ rather than left world-readable",
      "CHAT_SCOPE_DIR" in func("harden_state_files"))
check("the command is gated like /setscope (owner or that group's admin), not left open",
      "_is_group_admin" in func("cmd_setchatscope")
      and "_may_run_setup" in func("cmd_setchatscope"))

registered = "CommandHandler('setchatscope', cmd_setchatscope)" in ast.unparse(tree) \
    or '"setchatscope", cmd_setchatscope' in source
check("the command is actually registered", registered)
check("it is listed in /help, in both languages",
      source.count("/setchatscope <") >= 2)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
