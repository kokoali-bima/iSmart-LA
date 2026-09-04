#!/usr/bin/env python3
"""Tests for newagent.sh -- provisioning an ADDITIONAL deployment on one host.

Adding an agent means creating a Linux user with shell access, its own SSH
directory and its own subscription logins. That is a strictly larger capability
than anything the bot gates behind a PIN, which is why it is a script an
operator runs with root rather than a Telegram command. These tests cover the
two things that decide whether it is safe.

**1. The name is validated, not quoted.** It becomes a username, a directory
and a systemd unit name in the same breath, so `../etc`, `x;rm -rf /`, spaces
and uppercase are refused outright -- and refused BEFORE the root check, so a
typo is reported as a typo instead of sending someone to find sudo first only
to be turned away again.

**2. The permanent sudo grant is `systemctl restart <this unit>` and nothing
else.** /update ends by restarting its own service, and without that the bot
updates itself and never comes back. But refresh_systemd_unit() also wants to
`cp` a rendered unit into /etc/systemd/system, and granting THAT would let the
service user rewrite its own unit with `User=root` and own the host on the next
restart -- making every deployment on the box root-equivalent and reducing the
per-user isolation to decoration. The bot already treats that write as
best-effort (it logs and carries on without restarting), so refusing it costs a
convenience, not correctness. Several tests below exist purely to stop that
grant being widened later by someone fixing the log line.

The broad rights install.sh genuinely needs (apt, writing the unit) are granted
only for the duration of the install and removed by a trap on every exit path,
including Ctrl-C and a failed install.
"""
import pathlib
import re
import shutil
import subprocess
import sys

SRC = pathlib.Path(sys.argv[1]).resolve()
ROOT = SRC.parent
SCRIPT = ROOT / "newagent.sh"

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


check("newagent.sh exists", SCRIPT.is_file())
if not SCRIPT.is_file():
    print("\n0/1 passed")
    print("FAILED: ['newagent.sh exists']")
    print("the provisioning script is absent from this source -- every check "
          "below needs it")
    sys.exit(1)

text = SCRIPT.read_text(encoding="utf-8")
BASH = shutil.which("bash")


def run(*args):
    """newagent.sh as a real subprocess. Never with root, so nothing here can
    create a user or write a unit -- every path exercised below refuses before
    reaching anything privileged."""
    return subprocess.run([BASH, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=60)


# --- 1. it parses at all ----------------------------------------------------
if BASH:
    syntax = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    check("the script is syntactically valid", syntax.returncode == 0)
else:
    check("bash is available to run the script (skipped check otherwise)", False)

# --- 2. the name is refused before anything privileged ----------------------
if BASH:
    for bad, why in (
        ("Ops", "uppercase"),
        ("a b", "a space"),
        ("../etc", "path traversal"),
        ("x;rm -rf /", "a shell metacharacter"),
        ("café", "a non-ascii character"),
        ("a" * 21, "over the length limit"),
    ):
        r = run(bad)
        out = r.stdout + r.stderr
        check(f"a name with {why} is refused", r.returncode != 0)
        check(f"...and the message names the NAME, not root ({why})",
              ("name must be" in out or "too long" in out) and "must run as root" not in out)

    r = run("ops")
    out = r.stdout + r.stderr
    check("a VALID name gets past validation and stops at the root check",
          r.returncode != 0 and "must run as root" in out)

    check("no argument at all prints usage", "usage:" in (run().stdout + run().stderr))
    check("an unknown option is refused",
          "unknown option" in (run("ops", "--wat").stdout + run("ops", "--wat").stderr))


# --- 3. the permanent sudo grant is narrow ---------------------------------
# The runtime rule is written by a heredoc; take the block it emits.
runtime = re.search(r'cat > "\$SUDOERS_RUNTIME" <<EOF(.*?)\nEOF', text, re.S)
check("a permanent sudoers rule is written", runtime is not None)
rule = runtime.group(1) if runtime else ""
grant = "\n".join(ln for ln in rule.splitlines() if "NOPASSWD" in ln)

check("the permanent grant is systemctl restart", "systemctl restart" in grant)
check("...scoped to THIS deployment's unit, not any unit",
      "${SERVICE_NAME}" in grant)
check("...and is not a blanket ALL", not re.search(r"NOPASSWD:\s*ALL", grant))
check("the permanent grant does NOT include cp, which would let the service "
      "user rewrite its own unit as root",
      " cp " not in grant and "/bin/cp" not in grant)
check("...nor any write into /etc/systemd/system",
      "/etc/systemd/system" not in grant)
check("...nor daemon-reload, which is only reachable after the cp it does not have",
      "daemon-reload" not in grant)
check("the rule explains the refusal, so the next person to 'fix' the log line "
      "sees why it is deliberate",
      "rewrite" in rule and "root" in rule)
check("the generated rule is validated with visudo before being trusted",
      "visudo -cf" in text)
check("...and an invalid one is removed rather than left in /etc/sudoers.d",
      re.search(r'visudo -cf "\$SUDOERS_RUNTIME".*rm -f "\$SUDOERS_RUNTIME"', text, re.S) is not None)

# --- 4. the broad install-time grant cannot outlive the install ------------
check("the temporary install rule is a separate file from the runtime one",
      "SUDOERS_INSTALL=" in text and "SUDOERS_RUNTIME=" in text
      and "ismart-${NAME}-install" in text)
check("a trap removes it on EXIT, INT and TERM -- Ctrl-C and a failed install "
      "included, which is the only reason granting it is defensible",
      re.search(r"trap cleanup EXIT INT TERM", text) is not None)
cleanup = re.search(r"cleanup\(\)\s*\{(.*?)\n\}", text, re.S)
check("...and the trap actually deletes that file",
      cleanup is not None and "$SUDOERS_INSTALL" in cleanup.group(1)
      and "rm -f" in cleanup.group(1))

# --- 5. nothing existing is ever overwritten -------------------------------
# An existing deployment holds a PIN hash, sessions, SSH keys and connected
# Drive accounts. "Provision" must never be able to mean "destroy those".
for what, needle in (
    ("an existing user", 'id -u "$USER_NAME"'),
    ("an existing install directory", '[ -e "$INSTALL_DIR" ]'),
    ("an existing systemd unit", '[ -e "$UNIT_PATH" ]'),
):
    check(f"it refuses when {what} is already there", needle in text)
check("...and says so rather than continuing quietly",
      text.count("refusing to") >= 3)

# --- 6. the deployment it creates is actually separate ---------------------
check("the new deployment gets its own SERVICE_NAME, so it does not fight the "
      "other deployments over one unit file",
      'SERVICE_NAME="lite-agent-${NAME}"' in text
      and 'SERVICE_NAME=${SERVICE_NAME}' in text)
check("install.sh is run AS the new user, not as root -- otherwise the service "
      "would run as root and share nothing",
      re.search(r'sudo -u "\$USER_NAME".*install\.sh', text, re.S) is not None)
check("the user gets a real home directory, which is what actually separates "
      "~/.ssh, the CLI logins and rclone's config",
      "--create-home" in text)
check("it warns that each deployment needs its own bot token",
      "409" in text)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
