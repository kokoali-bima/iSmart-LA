#!/usr/bin/env python3
"""/addserver must not be blocked by our own read-only guard.

The operator installed the SSH key on the Kota Bima Proxmox and /addserver
still failed, over and over, with:

    pve-ro-guard: refused -- this key is read-only.

The key was fine. Verified live on that host: `hostname`, `uname -a` and
`cat` all answered normally with the read-only key, and only the probe was
refused. The probe was:

    echo ISMART_OK && uname -sr

and pve-ro-guard denies any `&`, `;`, backtick, redirect or newline outright,
before it ever looks at which verbs were used. So the sentinel that existed to
prove the command had run was the exact reason it could not run. Reproduced on
both Proxmox hosts in this fleet; the single server that ever registered got in
before the guard was installed on it.

`uname -sr` needs no sentinel -- it either answers "Linux <release>" or it did
not run.
"""
import atexit
import importlib.util
import os
import shutil as _shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SRC = sys.argv[1]
scratch = Path(tempfile.mkdtemp(prefix="isla_probe_"))
atexit.register(_shutil.rmtree, str(scratch), ignore_errors=True)
os.environ["HOME"] = str(scratch)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_USER_IDS", "111")
os.environ["ALLOWED_GROUP_IDS"] = ""

spec = importlib.util.spec_from_file_location("la", SRC)
mod = importlib.util.module_from_spec(spec)
sys.modules["la"] = mod
spec.loader.exec_module(mod)
mod.LEDGER_FILE = scratch / "spend.jsonl"

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


captured = {}


def run_probe(stdout="Linux 6.8.12-4-pve", stderr="", rc=0):
    def fake_run(argv, **kw):
        captured["argv"] = argv
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    key = scratch / "k"
    key.write_text("x", encoding="utf-8")
    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        return mod.test_server_ssh("10.0.0.1", "root", 22, key_path=str(key))


ok, detail = run_probe()
remote_cmd = captured["argv"][-1]

# The shell operators pve-ro-guard rejects outright, before looking at verbs.
BANNED = ["&&", "||", "&", ";", "`", "$(", ">", "<", "\n"]
present = [c for c in BANNED if c in remote_cmd]
check(f"the probe carries no shell operator the guard rejects "
      f"(command: {remote_cmd!r})", not present)
check("...specifically not the `&&` that caused this", "&&" not in remote_cmd)
check("...and no ISMART_OK sentinel that needs one", "ISMART_OK" not in remote_cmd)
check("the probe still identifies the machine", "uname" in remote_cmd)

check("a normal answer registers the server", ok and "Linux" in detail)

# The exact failure shape seen on the live host: the guard answers on stdout
# with exit 0, so returncode alone cannot tell success from refusal.
ok2, detail2 = run_probe(
    stdout="pve-ro-guard: refused -- this key is read-only.", rc=0)
check("a guard refusal is NOT mistaken for a successful probe", not ok2)

ok3, _ = run_probe(stdout="", stderr="Permission denied (publickey).", rc=255)
check("a real key failure still fails", not ok3)

ok4, _ = run_probe(stdout="   \n", rc=0)
check("an empty answer is not treated as success", not ok4)

# Discovery runs with the same read-only key and must stay guard-safe too.
src_text = Path(SRC).read_text(encoding="utf-8")
start = src_text.index("def discover_proxmox")
end = src_text.index("\ndef ", start + 10)
disco = src_text[start:end]
check("discovery uses `pvesh get`, which the guard allows",
      "pvesh get" in disco)
# Only the strings handed to ssh matter -- Python's own `;` and `&&` in the
# surrounding code are irrelevant. Pull out the quoted remote commands.
import re as _re
remote_cmds = _re.findall(r'run\("([^"]+)"\)', disco)
bad = [c for c in remote_cmds if "&&" in c or ";" in c or "`" in c]
check(f"...and none of its {len(remote_cmds)} remote commands chain with "
      f"&& or ; (which the guard rejects outright)", not bad)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
