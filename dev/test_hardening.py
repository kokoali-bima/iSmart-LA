#!/usr/bin/env python3
"""Tests that basic hardening is part of INSTALLING, not a doc page someone
gets to later.

This host is worth more than any single machine it manages: it holds the
Telegram bot token, the PIN hash, and the SSH keys that reach every managed
node. Before this, a fresh install left the service at `systemd-analyze
security` **9.6 UNSAFE**, and on a real deployment sessions.json and
spend.jsonl sat at mode 644 -- every chat's conversation state and token
history readable by any account on the box.

Every directive asserted here was verified against the real workload on a
live Linux host before being shipped, using a scratch unit and a probe that
exercised what the bot actually does (write its own directory, read AND
rewrite ~/.ssh for the write-mode key swap, DNS, outbound TLS, spawn both
CLIs, tmux, the `sudo -n systemctl` hop /update depends on) -- 10/10 under
the final template, scoring 5.8 MEDIUM. Two initial assumptions were wrong
and the measurements corrected them, which is why the "deliberately not set"
list below is asserted too: MemoryDenyWriteExecute was assumed to break the
Node-based CLIs and does not (a real Claude turn and a real agy turn both
succeeded under it, so it is ON), while ProtectHome was assumed merely
awkward and in fact breaks the install outright on a root deployment.

Source inspection, deliberately: these are the files an installer runs, and
the point is that the shipped artefacts still say what was verified.
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "systemd" / "lite-agent.service.template"
INSTALL = ROOT / "install.sh"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

unit = UNIT.read_text(encoding="utf-8") if UNIT.exists() else ""
install = INSTALL.read_text(encoding="utf-8") if INSTALL.exists() else ""

check("the systemd unit template exists", bool(unit))
check("install.sh exists", bool(install))

# --- the sandbox half, each one measured against the real workload ---------
REQUIRED = {
    "ProtectSystem=full": "/usr, /boot and /etc read-only",
    "PrivateDevices=yes": "no device access",
    "ProtectKernelTunables=yes": "no kernel tunables",
    "ProtectKernelModules=yes": "no module loading",
    "ProtectKernelLogs=yes": "no kernel log access",
    "ProtectControlGroups=yes": "no cgroup writes",
    "ProtectClock=yes": "cannot set the system clock",
    "ProtectHostname=yes": "cannot change the hostname",
    "RestrictSUIDSGID=yes": "cannot create setuid/setgid files",
    "RestrictRealtime=yes": "no realtime scheduling",
    "LockPersonality=yes": "cannot change execution domain",
    "RemoveIPC=yes": "no leftover SysV IPC",
    "MemoryDenyWriteExecute=yes": "no W+X pages (measured safe for both CLIs)",
    "UMask=0077": "its own state files are owner-only, not world-readable",
    "SystemCallArchitectures=native": "native ABI only",
    "RestrictNamespaces=yes": "cannot create namespaces",
}
for directive, why in REQUIRED.items():
    check(f"unit sets {directive} -- {why}", directive in unit)

check("the socket families are restricted to what is actually used",
      re.search(r"^RestrictAddressFamilies=.*AF_INET\b", unit, re.M) is not None)
check("...including AF_NETLINK, which glibc's getaddrinfo() needs -- dropping "
      "it breaks DNS in a way that looks like a network fault",
      "AF_NETLINK" in unit)

# --- the directives that must stay OFF, each for a measured reason ---------
def active(directive: str) -> bool:
    """Present as a real setting, not merely mentioned in a comment."""
    return re.search(rf"^{re.escape(directive)}=", unit, re.M) is not None

check("NoNewPrivileges is NOT set -- it removes exactly the setuid escalation "
      "`sudo -n systemctl restart` needs, so /update would leave the bot down "
      "on any non-root install", not active("NoNewPrivileges"))
check("ProtectHome is NOT set -- measured to break BOTH the install directory "
      "and the ~/.ssh write-mode key swap on a root deployment (OSError 30)",
      not active("ProtectHome"))
check("PrivateTmp is NOT set -- it hides the sign-in tmux sessions from an "
      "operator trying to see what a stuck login is showing",
      not active("PrivateTmp"))
check("...and each omission is explained in the file itself, so the absence "
      "reads as a decision rather than an oversight",
      "NoNewPrivileges" in unit and "ProtectHome" in unit and "PrivateTmp" in unit)

# --- the file-permission half ----------------------------------------------
check("install.sh has a hardening step of its own",
      "hardening" in install.lower())
check("...that locks the state files down (UMask only governs NEW files, so "
      "anything an earlier version wrote keeps its old mode)",
      "chmod 600" in install)
for f in ("sessions.json", "spend.jsonl", "pin.json", ".env", "mcp_servers.json"):
    check(f"...covering {f}", f in install)
check("...and the per-chat memory directory", "chmod 700" in install)
check("...and ~/.ssh", ".ssh" in install)
check("the firewall note tells the operator this bot needs NO inbound ports",
      "ufw" in install and ("inbound" in install.lower() or "incoming" in install.lower()))

# --- the unit actually RUNNING gets refreshed, not just the one in the repo -
# /update fast-forwards the checkout, but the unit systemd runs was copied to
# /etc at install time. Without this, a release that hardens the unit lands
# the new template in the repo while the service keeps running unhardened --
# and the operator would reasonably believe otherwise.
SRC = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "lite_agent.py")
scratch = Path(tempfile.mkdtemp(prefix="isla_unitref_"))
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""
sys.path.insert(0, str(Path(SRC).resolve().parent / "tools"))
spec = importlib.util.spec_from_file_location("la_h", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la_h"] = mod
spec.loader.exec_module(mod)

check("/update refreshes the installed systemd unit, not only the repo copy",
      hasattr(mod, "refresh_systemd_unit"))

src = Path(SRC).read_text(encoding="utf-8")
check("...and it runs BEFORE the restart, so systemd loads the new unit",
      re.search(r"refresh_systemd_unit\(\)[\s\S]{0,600}?sudo.{0,40}systemctl", src) is not None)

# A template with no [Service]/ExecStart must be refused. This is OUR check:
# `systemd-analyze verify` was measured to exit 0 on a unit containing an
# invented directive, so it cannot be what stands between a bad render and a
# bot that never comes back.
mod.SERVICE_TEMPLATE = scratch / "bad.template"
mod.SERVICE_UNIT_PATH = scratch / "fake.service"
mod.SERVICE_TEMPLATE.write_text("[Unit]\nDescription=no service section\n")
check("a malformed template is refused rather than installed",
      "malformed" in mod.refresh_systemd_unit())
check("...and nothing was written over the installed unit",
      not mod.SERVICE_UNIT_PATH.exists())

mod.SERVICE_TEMPLATE = scratch / "missing.template"
check("a template that isn't in this checkout at all is a clean no-op",
      "no template" in mod.refresh_systemd_unit())

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
