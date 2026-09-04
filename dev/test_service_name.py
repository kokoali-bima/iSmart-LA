#!/usr/bin/env python3
"""Tests for the one thing two deployments on one host still shared: the
systemd unit file.

Everything else was already separated. State is BASE_DIR-relative (SOUL.md,
GEMINI.md, MEMORY.md, pin.json, sessions.json, servers.json, spend.jsonl...),
so two clones already have separate briefs, memory, PINs and ledgers.
SERVICE_NAME was already settable from the environment and already used for
the `systemctl restart` in /update. Only the PATH never followed it:

    SERVICE_UNIT_PATH = Path("/etc/systemd/system/lite-agent.service")

That is not a cosmetic clash. refresh_systemd_unit() renders the template with
THIS deployment's user and BASE_DIR and writes it to that path, and
apply_hardening_on_start() calls it on every start -- so with two installs:

  - install B rewrites install A's unit, pointing it at B's directory
  - A starts, sees a unit that is not its own render, refreshes it, restarts
  - B starts, sees the same thing from the other side, refreshes, restarts

...and the two go on restarting each other. The failure is silent in the sense
that matters: both bots answer, so nothing looks broken until someone reads
`systemctl show -p NRestarts`.

Also covers the link that makes the whole thing work, and which is easy to
leave out because nothing fails immediately without it: install.sh must write
SERVICE_NAME into .env whenever it is not the default. Without that line the
process reads the default, so it restarts a unit that does not exist and
rewrites the OTHER deployment's file -- exactly the bug this change removes,
reintroduced through the installer instead of the source.

Pure source inspection, like dev/test_network_timeouts.py: no systemd, no
root, and identical behaviour on any OS the repo is edited from.
"""
import ast
import pathlib
import re
import sys

SRC = pathlib.Path(sys.argv[1]).resolve()
ROOT = SRC.parent
source = SRC.read_text(encoding="utf-8")
tree = ast.parse(source, filename=str(SRC))

INSTALL_SH = ROOT / "install.sh"
install = INSTALL_SH.read_text(encoding="utf-8") if INSTALL_SH.exists() else ""

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def assign_node(name: str):
    """The module-level `name = ...` node, or None."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node
    return None


def names_in(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


# --- 1. the unit path is derived, not hardcoded ----------------------------
unit = assign_node("SERVICE_UNIT_PATH")
check("SERVICE_UNIT_PATH is assigned at module level", unit is not None)
check("SERVICE_UNIT_PATH is built from SERVICE_NAME, not a fixed literal",
      unit is not None and "SERVICE_NAME" in names_in(unit.value))
check("SERVICE_UNIT_PATH still points into /etc/systemd/system",
      unit is not None
      and any(isinstance(n, ast.Constant) and isinstance(n.value, str)
              and "/etc/systemd/system" in n.value
              for n in ast.walk(unit.value)))

# The literal that WAS there. Its absence is the fix; keep it named so a
# future edit that reintroduces it fails here rather than on a live host.
check("no hardcoded /etc/systemd/system/lite-agent.service anywhere in the module",
      "/etc/systemd/system/lite-agent.service" not in source)

# --- 2. definition order (this bites at import, not at call time) ----------
svc = assign_node("SERVICE_NAME")
check("SERVICE_NAME is assigned at module level", svc is not None)
check("SERVICE_NAME reads the environment with a default",
      svc is not None and "environ" in ast.unparse(svc.value) and "lite-agent" in ast.unparse(svc.value))
check("SERVICE_NAME is defined BEFORE SERVICE_UNIT_PATH uses it",
      svc is not None and unit is not None and svc.lineno < unit.lineno)

# --- 3. every systemctl call targets the configured name -------------------
restarts = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and any(isinstance(a, ast.Constant) and a.value == "restart" for a in n.args
                    if not isinstance(a, ast.Starred))
            or (isinstance(n, ast.Call) and any(
                isinstance(a, ast.List) and any(
                    isinstance(e, ast.Constant) and e.value == "restart" for e in a.elts)
                for a in n.args))]
restart_srcs = [ast.unparse(n) for n in restarts]
check("at least one systemctl restart call is present",
      any("systemctl" in s for s in restart_srcs))
check("every systemctl restart passes SERVICE_NAME rather than a literal unit name",
      all("SERVICE_NAME" in s for s in restart_srcs if "systemctl" in s))

# --- 4. the unit backup follows the unit it backs up -----------------------
check("the saved previous unit is named after SERVICE_NAME too",
      re.search(r'\.\{SERVICE_NAME\}\.service\.bak', source) is not None)
gitignore = (ROOT / ".gitignore")
check(".gitignore still covers that backup once it is no longer a fixed name",
      gitignore.exists()
      and any(line.strip() in (".*.service.bak", "*.service.bak")
              for line in gitignore.read_text(encoding="utf-8").splitlines()))

# --- 5. install.sh: the other half -----------------------------------------
check("install.sh exists", bool(install))
check("install.sh takes SERVICE_NAME from the environment with a default",
      re.search(r'SERVICE_NAME="\$\{SERVICE_NAME:-lite-agent\}"', install) is not None)
check("install.sh writes the unit to /etc/systemd/system/${SERVICE_NAME}.service",
      re.search(r'SERVICE_FILE="/etc/systemd/system/\$\{SERVICE_NAME\}\.service"', install) is not None)
check("install.sh no longer hardcodes the unit filename",
      "/etc/systemd/system/lite-agent.service" not in install)

# The link that makes a custom name actually work end to end.
env_persist = re.search(r'if\s+\[\s+"\$SERVICE_NAME"\s+!=\s+"lite-agent"\s+\]', install)
check("install.sh persists a NON-default SERVICE_NAME into .env",
      env_persist is not None and re.search(r'SERVICE_NAME=\$\{SERVICE_NAME\}', install) is not None)
check("...and does not write it when it is merely the default "
      "(the .env convention here is overrides only)",
      env_persist is not None)

check("the enable/journalctl hints name the service that was actually installed",
      "systemctl enable --now ${SERVICE_NAME}" in install
      and "journalctl -u ${SERVICE_NAME}" in install)

# --- 6. the mangled line continuation in the hardening loop ----------------
# The whole `for f in ...` list had been collapsed onto one physical line with
# one surviving literal backslash-n in the middle of it. Unquoted, sh reads
# that as the word `n`, so the loop chmod'd a phantom $INSTALL_DIR/n. Harmless
# by luck -- no file is named `n` -- but it is a broken continuation, and the
# next person to add a filename to that list would be editing damaged text.
loop = re.search(r'for f in (.*?); do', install, re.S)
check("the hardening loop is present in install.sh", loop is not None)
check("the hardening loop contains no literal backslash-n "
      "(a mangled line continuation, read by sh as a file named 'n')",
      loop is not None and "\\n" not in loop.group(1))

# --- 7. the two hardening lists have not drifted apart ---------------------
# install.sh covers files already on disk at install time; HARDEN_600 covers
# them again on every start and every /update. They are meant to be the same
# set, and a file added to one and forgotten in the other is a state file that
# stays world-readable on whichever path is not taken.
harden = assign_node("HARDEN_600")
py_files = set()
if harden is not None:
    py_files = {n.value for n in ast.walk(harden.value)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
sh_files = set(loop.group(1).split()) - {"\\"} if loop else set()
check("HARDEN_600 is present in the module", bool(py_files))
check("install.sh and HARDEN_600 harden exactly the same files "
      f"(only in .sh: {sorted(sh_files - py_files)}; only in .py: {sorted(py_files - sh_files)})",
      bool(py_files) and py_files == sh_files)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
