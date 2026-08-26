#!/usr/bin/env python3
"""
cluster-snapshot -- one-shot, pre-digested Proxmox cluster status. EXAMPLE /
reference implementation of the "graduated skill" pattern for a Proxmox VE
target -- copy this into your own tools/ dir and edit NODE_IPS for your cluster.

Why this exists: letting the LLM discover cluster state on its own can take
a dozen-plus sequential tool calls (one pvesh/ssh call per node, plus trial-
and-error on jq/field names). Every one of those round-trips re-sends the
whole growing conversation, which is what actually burns tokens -- not the
reasoning.

`pvesh get /cluster/resources` already returns EVERYTHING (all nodes, all
VMs/LXCs, all storages) in a single call from any one node. This script makes
that one call, digests the large raw JSON down to a compact summary, and
caches it, so the agent spends ONE small tool call instead of many large ones.

Output is deliberately terse and pre-computed (percentages already
calculated, already sorted) so the model doesn't have to do arithmetic over
raw byte counts -- that tends to trigger re-querying and second-guessing.

Usage:
    cluster_snapshot.py              # cached if fresh, else collect
    cluster_snapshot.py --force      # ignore cache
    cluster_snapshot.py --json       # machine-readable instead of text table
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

CACHE_FILE = Path("/tmp/cluster-snapshot.json")
CACHE_TTL_SECONDS = 120

# Any cluster member works -- /cluster/resources is cluster-wide.
# EDIT THIS for your own cluster's node IPs.
COLLECTOR_NODE = "10.0.0.11"

NODE_IPS = {
    "node-a": "10.0.0.11",
    "node-b": "10.0.0.12",
    "node-c": "10.0.0.13",
}


def _gb(n: float) -> float:
    return round(n / 1024 ** 3, 1)


def _pct(used: float, total: float) -> float:
    return round(used / total * 100, 1) if total else 0.0


def collect() -> dict:
    """One SSH round-trip for the whole cluster."""
    proc = subprocess.run(
        ["ssh", COLLECTOR_NODE, "pvesh get /cluster/resources --output-format json"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pvesh failed: {proc.stderr[-300:]}")

    # pvesh prints harmless 'ignore invalid privilege' warnings before the JSON.
    raw = proc.stdout
    start = raw.find("[")
    if start < 0:
        raise RuntimeError(f"no JSON array in pvesh output: {raw[:200]}")
    resources = json.loads(raw[start:])

    nodes, guests, storages = [], [], []
    for r in resources:
        rtype = r.get("type")
        if rtype == "node":
            nodes.append({
                "node": r["node"],
                "status": r.get("status"),
                "cpu_pct": round(r.get("cpu", 0) * 100, 1),
                "cpu_cores": r.get("maxcpu"),
                "ram_pct": _pct(r.get("mem", 0), r.get("maxmem", 0)),
                "ram_used_gb": _gb(r.get("mem", 0)),
                "ram_total_gb": _gb(r.get("maxmem", 0)),
            })
        elif rtype in ("qemu", "lxc"):
            if r.get("template"):
                continue
            guests.append({
                "vmid": r.get("vmid"),
                "name": r.get("name"),
                "node": r.get("node"),
                "type": rtype,
                "status": r.get("status"),
                # cpu is a 0..1 fraction OF THE GUEST'S OWN vCPUs, not of the host
                "cpu_pct": round(r.get("cpu", 0) * 100, 1),
                "vcpus": r.get("maxcpu"),
                "ram_pct": _pct(r.get("mem", 0), r.get("maxmem", 0)),
                "ram_used_gb": _gb(r.get("mem", 0)),
            })
        elif rtype == "storage":
            total = r.get("maxdisk", 0)
            storages.append({
                "id": r.get("id"),
                "node": r.get("node"),
                "storage": r.get("storage"),
                "type": r.get("plugintype"),
                "shared": bool(r.get("shared")),
                "used_pct": _pct(r.get("disk", 0), total),
                "used_gb": _gb(r.get("disk", 0)),
                "total_gb": _gb(total),
            })

    # Shared storages (PBS etc.) repeat once per node -- keep one entry each.
    seen_shared = set()
    deduped = []
    for s in sorted(storages, key=lambda x: -x["used_pct"]):
        key = s["storage"] if s["shared"] else s["id"]
        if key in seen_shared:
            continue
        seen_shared.add(key)
        deduped.append(s)

    running = [g for g in guests if g["status"] == "running"]
    return {
        "collected_at": int(time.time()),
        "nodes": sorted(nodes, key=lambda n: n["node"]),
        "storages": deduped,
        "top_cpu": sorted(running, key=lambda g: -g["cpu_pct"])[:10],
        "top_ram": sorted(running, key=lambda g: -g["ram_used_gb"])[:10],
        "counts": {
            "nodes": len(nodes),
            "guests_total": len(guests),
            "guests_running": len(running),
            "guests_stopped": len(guests) - len(running),
        },
    }


def load_cached(force: bool = False) -> dict:
    if not force and CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            if time.time() - data.get("collected_at", 0) < CACHE_TTL_SECONDS:
                data["from_cache"] = True
                return data
        except Exception:
            pass
    data = collect()
    try:
        CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass  # cache is an optimization, never a hard requirement
    data["from_cache"] = False
    return data


def render(d: dict) -> str:
    age = int(time.time() - d["collected_at"])
    out = [f"CLUSTER SNAPSHOT (age {age}s, {'cached' if d.get('from_cache') else 'fresh'})", ""]

    out.append("NODES  node | cpu% | ram% (used/total GB) | status")
    for n in d["nodes"]:
        out.append(
            f"  {n['node']:<10} {n['cpu_pct']:>5}% (of {n['cpu_cores']}c) | "
            f"{n['ram_pct']:>5}% ({n['ram_used_gb']}/{n['ram_total_gb']}GB) | {n['status']}"
        )

    out.append("")
    out.append("STORAGE (deduped, sorted by fullest; shared listed once)")
    for s in d["storages"]:
        flag = "  <-- CRITICAL" if s["used_pct"] >= 90 else ("  <-- watch" if s["used_pct"] >= 80 else "")
        scope = "shared" if s["shared"] else s["node"]
        out.append(
            f"  {s['storage']:<14} [{scope:<10}] {s['used_pct']:>5}% "
            f"({s['used_gb']}/{s['total_gb']}GB, {s['type']}){flag}"
        )

    out.append("")
    out.append("TOP CPU (running guests; % is of that guest's own vCPUs)")
    for g in d["top_cpu"][:5]:
        out.append(f"  {g['cpu_pct']:>5}%  {g['name']} (vmid {g['vmid']}, {g['vcpus']}c) @ {g['node']}")

    out.append("")
    out.append("TOP RAM (running guests, by GB used)")
    for g in d["top_ram"][:5]:
        out.append(f"  {g['ram_used_gb']:>6}GB ({g['ram_pct']}%)  {g['name']} (vmid {g['vmid']}) @ {g['node']}")

    c = d["counts"]
    out.append("")
    out.append(f"COUNTS  nodes={c['nodes']} guests={c['guests_total']} "
               f"running={c['guests_running']} stopped={c['guests_stopped']}")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore cache")
    ap.add_argument("--json", action="store_true", help="raw JSON instead of text table")
    args = ap.parse_args()

    try:
        data = load_cached(force=args.force)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(data, indent=2) if args.json else render(data))


if __name__ == "__main__":
    main()
