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
import re
import sys
from pathlib import Path

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

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
