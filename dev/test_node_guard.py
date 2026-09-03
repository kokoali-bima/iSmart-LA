#!/usr/bin/env python3
"""Tests for making the write gate real, and for proving it rather than assuming it.

This encodes a live incident. A "delete VM 8006" request in a group produced a
reply saying "click the approval button below" -- no button appeared, no PIN
was asked, and the next message caused the VM to actually be destroyed. The
Proxmox task log settled it:

    qmstop     vmid=8006  root@pam  2026-09-01 09:56:53 WIB
    qmdestroy  vmid=8006  root@pam  2026-09-01 09:57:00 WIB

Two independent failures, and both were silent:

  1. ~/.ssh/agent_readonly and agent_write did not exist, so _keys_configured()
     was False, so the ENTIRE approval/PIN gate was inert -- no button could
     ever be rendered no matter what the model emitted.
  2. The node's authorized_keys entry had no command= restriction, so the key
     was ordinary unrestricted root. Proven at the time with a harmless write:

         === HARMLESS write test as the AGENT key ===
         WRITE_SUCCEEDED_NOT_BLOCKED

Both had been documented as manual README steps, while /addserver -- the path
the bot itself recommends -- generated one unrestricted key and appended it
plainly. The dangerous configuration was the default one.

So the tests below care about two things above all: that a fresh deployment
ends up with a live gate without anyone reading documentation, and that
"protected" is never reported unless a write was actually attempted with the
read-only key and actually refused.
"""
import atexit
import shutil as _shutil
import importlib.util, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SRC = sys.argv[1]
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
HOME = tempfile.mkdtemp(prefix="isla_guard_")
# Tests must not litter the machine they run on: 485 stale
# isla_* directories were found on a real server after a few days
# of runs. Registered rather than done at the end, so a failing
# assertion still cleans up.
atexit.register(_shutil.rmtree, str(HOME), ignore_errors=True)
os.environ["HOME"] = HOME
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

def proc(stdout="", stderr="", rc=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)

HAVE_KEYGEN = shutil.which("ssh-keygen") is not None

# --- 1. a fresh deployment gets a live gate, unattended --------------------
if HAVE_KEYGEN:
    check("before setup the gate is inert, exactly as the incident found it",
          mod._keys_configured() is False)
    check("ensure_write_mode_keys() reports success", mod.ensure_write_mode_keys() is True)
    check("...the read-only key now exists", mod.SSH_RO_KEY.exists())
    check("...the write key now exists", mod.SSH_RW_KEY.exists())
    check("...and the gate is no longer inert (THE fix for 'no button ever')",
          mod._keys_configured() is True)
    check("the active key starts pointed at the READ-ONLY key (locked default)",
          Path(os.readlink(mod.SSH_ACTIVE_KEY)).name == mod.SSH_RO_KEY.name
          if mod.SSH_ACTIVE_KEY.is_symlink() else False)
    before = mod.SSH_RO_KEY.read_bytes()
    check("re-running is idempotent and does NOT rotate existing keys",
          mod.ensure_write_mode_keys() is True and mod.SSH_RO_KEY.read_bytes() == before)
else:
    print("SKIP - ssh-keygen unavailable, key-generation cases skipped")

# --- 2. verification must actually observe a refusal ----------------------
GUARD_MSG = "pve-ro-guard: refused -- this key is read-only."

def fake_unguarded(key, host, user, port, cmd, timeout=30):
    if "hostname" in cmd:
        return proc(stdout="node1\n")
    return proc(stdout="WRITE_WENT_THROUGH\n")          # the incident, exactly

def fake_guarded(key, host, user, port, cmd, timeout=30):
    if "hostname" in cmd:
        return proc(stdout="node1\n")
    return proc(stderr=GUARD_MSG, rc=126)

def fake_dead(key, host, user, port, cmd, timeout=30):
    return proc(stderr="Permission denied (publickey).", rc=255)

with patch.object(mod, "_ssh_as", side_effect=fake_unguarded):
    ok, detail = mod.verify_node_guard("h", "root", 22)
check("verification FAILS when the read-only key can still write (THE incident)",
      ok is False)
check("...and says plainly that the node is unprotected",
      "unprotected" in detail.lower())

with patch.object(mod, "_ssh_as", side_effect=fake_guarded):
    ok, detail = mod.verify_node_guard("h", "root", 22)
check("verification PASSES only when a write was refused by the guard", ok is True)
check("...and reports what it actually observed", "refused" in detail.lower())

with patch.object(mod, "_ssh_as", side_effect=fake_dead):
    ok, detail = mod.verify_node_guard("h", "root", 22)
check("a host the read-only key cannot even read is not called protected",
      ok is False and "cannot even read" in detail)

# --- 3. the legacy-key migration path -------------------------------------
if HAVE_KEYGEN:
    legacy = Path(HOME) / ".ssh" / "ismart_agent"
    legacy.write_text("x")
    def only_legacy_works(key, host, user, port, cmd, timeout=30):
        if Path(key).name == "ismart_agent":
            return proc(stdout="ISMART_ADMIN_OK\n")
        return proc(stderr="Permission denied", rc=255)
    with patch.object(mod, "_ssh_as", side_effect=only_legacy_works):
        chosen = mod._admin_key_for("h", "root", 22)
    check("migration: falls back to the legacy unrestricted key when the write "
          "key is not authorised yet (so an existing host needs no manual redo)",
          chosen is not None and Path(chosen).name == "ismart_agent")

    def rw_works(key, host, user, port, cmd, timeout=30):
        return proc(stdout="ISMART_ADMIN_OK\n")
    with patch.object(mod, "_ssh_as", side_effect=rw_works):
        chosen = mod._admin_key_for("h", "root", 22)
    check("...but prefers the proper write key whenever it works",
          chosen is not None and Path(chosen).name == mod.SSH_RW_KEY.name)

    def nothing_works(key, host, user, port, cmd, timeout=30):
        return proc(stderr="Permission denied", rc=255)
    with patch.object(mod, "_ssh_as", side_effect=nothing_works):
        check("no usable admin key -> None, rather than a confident wrong answer",
              mod._admin_key_for("h", "root", 22) is None)

# --- 4. ordering safety: never retire the old key on a failed verify ------
calls = []
def install_ok_verify_bad(key, host, user, port, cmd, timeout=30):
    calls.append(cmd)
    if "ISMART_ADMIN_OK" in cmd:
        return proc(stdout="ISMART_ADMIN_OK\n")
    if "ISMART_GUARD_INSTALLED" in cmd:
        return proc(stdout="ISMART_GUARD_INSTALLED\n")
    if "hostname" in cmd:
        return proc(stdout="node1\n")
    return proc(stdout="WRITE_WENT_THROUGH\n")     # verification will fail

if HAVE_KEYGEN:
    calls.clear()
    with patch.object(mod, "_ssh_as", side_effect=install_ok_verify_bad):
        ok, detail = mod.secure_server("h", "root", 22)
    check("secure_server reports failure when verification does not hold", ok is False)
    check("...and NEVER deletes the legacy key on a failed verify "
          "(a wrong order here locks the operator out of their own node)",
          not any("sed -i" in c for c in calls))

    # Retiring is a SEPARATE step on purpose. Until ~/.ssh/config points at the
    # active-key symlink, the legacy key is still the credential actually in
    # use -- removing it from a node inside secure_server() would lock the
    # agent out of the very host it had just secured.
    def all_good(key, host, user, port, cmd, timeout=30):
        calls.append(cmd)
        if "ISMART_ADMIN_OK" in cmd:
            return proc(stdout="ISMART_ADMIN_OK\n")
        if "ISMART_GUARD_INSTALLED" in cmd:
            return proc(stdout="ISMART_GUARD_INSTALLED\n")
        if "hostname" in cmd:
            return proc(stdout="node1\n")
        return proc(stderr=GUARD_MSG, rc=126)

    calls.clear()
    with patch.object(mod, "_ssh_as", side_effect=all_good):
        ok, detail = mod.secure_server("h", "root", 22)
    check("a fully successful secure_server still does NOT retire the old key "
          "(that waits until ~/.ssh/config has been flipped)",
          ok is True and not any("sed -i" in c for c in calls))
    check("retire_legacy_key exists as the separate, later step",
          callable(getattr(mod, "retire_legacy_key", None)))

    calls.clear()
    def retire_ok(key, host, user, port, cmd, timeout=30):
        calls.append(cmd)
        return proc(stdout="LEGACY_KEY_REMOVED\n")
    with patch.object(mod, "_ssh_as", side_effect=retire_ok):
        check("retire_legacy_key reports success when the old key was removed",
              mod.retire_legacy_key("h", "root", 22) is True)
    check("...and it is the step that actually edits authorized_keys",
          any("sed -i" in c for c in calls))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
