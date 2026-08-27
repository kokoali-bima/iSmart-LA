#!/usr/bin/env python3
"""
Run one registered scheduled task, by name.

Every cron line the bot installs calls THIS, never the task command directly.
That indirection is what makes the registry authoritative: a task cannot change
what it does, or how much access it has, by editing a crontab line -- the crontab
only carries a name, and everything else is looked up here.

WRITE ACCESS
    A scheduled task runs with nobody watching, so by default it gets the same
    restricted key the bot uses when locked. A task the operator explicitly
    approved with write access (a single button press at creation time, recorded
    in the registry) gets the full key for the duration of that run only, via a
    tiny `ssh` shim placed first on PATH.

    The shim is deliberately narrow: it adds `-i <write key>` and nothing else.
    It exists for the run and is discarded afterwards, so nothing about the
    machine's normal state changes, and a task that was never approved for write
    access simply cannot reach the write key at all.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEDULES_FILE = BASE_DIR / "schedules.json"
SSH_WRITE_KEY = Path(os.environ.get("SSH_RW_KEY", str(Path.home() / ".ssh/agent_write")))


def log(msg: str) -> None:
    from datetime import datetime
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def main() -> int:
    if len(sys.argv) < 2:
        log("usage: run_scheduled.py <task-name>")
        return 2
    name = sys.argv[1]

    try:
        tasks = json.loads(SCHEDULES_FILE.read_text())
    except Exception as exc:
        log(f"cannot read {SCHEDULES_FILE}: {exc}")
        return 1

    task = next((t for t in tasks if t.get("name") == name), None)
    if task is None:
        # The registry is authoritative: an entry removed with /unschedule but
        # still present in cron must NOT keep running.
        log(f"task '{name}' is not in the registry -- refusing to run")
        return 1

    env = os.environ.copy()
    shim_dir: str | None = None

    if task.get("needs_write"):
        if not SSH_WRITE_KEY.exists():
            log(f"task '{name}' needs write access but {SSH_WRITE_KEY} is missing -- refusing")
            return 1
        shim_dir = tempfile.mkdtemp(prefix="ismart-sched-")
        shim = Path(shim_dir) / "ssh"
        shim.write_text(
            "#!/bin/sh\n"
            f'exec /usr/bin/ssh -i "{SSH_WRITE_KEY}" "$@"\n'
        )
        shim.chmod(0o700)
        env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
        log(f"task '{name}': write access granted for this run only")

    log(f"running '{name}': {task['run']}")
    try:
        proc = subprocess.run(
            task["run"], shell=True, cwd=str(BASE_DIR), env=env, timeout=3600,
        )
        log(f"task '{name}' finished with exit code {proc.returncode}")
        return proc.returncode
    except subprocess.TimeoutExpired:
        log(f"task '{name}' exceeded its 1 hour limit and was killed")
        return 1
    finally:
        if shim_dir:
            shutil.rmtree(shim_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
