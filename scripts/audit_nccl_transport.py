#!/usr/bin/env python3
"""Audit which NCCL transport every dual-node server launch actually used.

NCCL_IB_DISABLE=0 only *permits* RDMA: NCCL silently falls back to TCP
sockets when the RoCE handshake fails. With NCCL_DEBUG=INFO and
NCCL_DEBUG_SUBSYS=INIT,NET on both Ray containers (the connectx_env.sh default), NCCL
logs one "via NET/<transport>" line per channel into the server log, plus the
RDMA devices it opened. This report reads every tp2/pp2 server log in a result
directory, compares the observed transport with the one the launch's serving
policy requested, and summarizes device and GPUDirect RDMA use, which is the
evidence a reviewer asking "was this really RDMA?" needs.

    python3 scripts/audit_nccl_transport.py results/benchmark-2026-10-22

Exits 1 when any launch is unverified or shows the wrong transport, or no
dual-node logs were found. Use --allow-unverified only for historical,
exploratory reports that may lack NCCL channel evidence or serving policies.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.campaign_output import (
    campaign_root_from_input,
    resolve_campaign_logs_dir,
    resolve_campaign_output_path,
    resolve_campaign_results_dir,
)

LOG_RE = re.compile(
    r"server_(?P<label>.+)_(?P<config>single|tp2|pp2)_(?P<fp>[0-9a-f]{16})_"
    r"(?P<ts>\d{8}T\d{6}Z)_pid\d+\.log"
)
IB_CHANNEL = re.compile(r"via NET/IB")
SOCKET_CHANNEL = re.compile(r"via NET/Socket")
GDR_CHANNEL = re.compile(r"via NET/IB\S*/GDRDMA")
# e.g. "NET/IB : Using [0]rocep1s0f0:1/RoCE [1]roceP2p1s0f0:1/RoCE ; OOB ..."
IB_DEVICES = re.compile(r"NET/IB\S* : Using (.*?)(?:;|$)")
DEVICE = re.compile(r"\[\d+\]([A-Za-z0-9_.-]+):\d+/(RoCE|IB)")


def policy_expectations(out_dir: Path) -> dict[tuple[str, str, str], str]:
    """(label, config, fingerprint prefix) -> requested transport."""
    expected = {}
    for path in out_dir.glob("serving_policy_*.json"):
        try:
            policy = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        fingerprint = str(policy.get("serving_fingerprint") or "")
        name = path.name[len("serving_policy_"):-len(".json")]
        suffix = f"_{policy.get('config')}_{fingerprint}"
        if not fingerprint or not name.endswith(suffix):
            continue
        label = name[: -len(suffix)]
        mode = str(policy.get("nccl_ib_disable"))
        if mode not in {"0", "1"}:
            continue
        transport = "Socket" if mode == "1" else "IB"
        expected[(label, str(policy.get("config")), fingerprint[:16])] = transport
    return expected


def audit_log(path: Path) -> dict:
    ib = socket = gdr = 0
    devices: set[str] = set()
    with path.open(errors="replace") as handle:
        for line in handle:
            if "NET/" not in line:
                continue
            if IB_CHANNEL.search(line):
                ib += 1
                if GDR_CHANNEL.search(line):
                    gdr += 1
            elif SOCKET_CHANNEL.search(line):
                socket += 1
            using = IB_DEVICES.search(line)
            if using:
                devices.update(name for name, _ in DEVICE.findall(using.group(1)))
    return {"ib": ib, "socket": socket, "gdr": gdr, "devices": sorted(devices)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir")
    parser.add_argument("--md", help="also write the report as markdown")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="allow absent logs/evidence/policies for exploratory reports")
    args = parser.parse_args(argv)
    out_dir = campaign_root_from_input(args.out_dir)
    if args.md:
        args.md = str(resolve_campaign_output_path(args.md, out_dir))
    results_dir = resolve_campaign_results_dir(out_dir)
    logs_dir = resolve_campaign_logs_dir(out_dir)
    expected = policy_expectations(results_dir)

    rows = []
    for path in sorted(logs_dir.glob("server_*.log")):
        match = LOG_RE.fullmatch(path.name)
        if not match or match["config"] == "single":
            continue
        want = expected.get((match["label"], match["config"], match["fp"]), "?")
        found = audit_log(path)
        wrong = found["socket"] if want == "IB" else found["ib"] if want == "Socket" else 0
        right = found["ib"] if want == "IB" else found["socket"] if want == "Socket" else 0
        if wrong:
            verdict = "WRONG TRANSPORT"
        elif right:
            verdict = "ok"
        elif found["ib"] or found["socket"]:
            verdict = "no policy"
        else:
            verdict = "no evidence"
        rows.append({"log": path.name, "label": match["label"],
                     "config": match["config"], "requested": want,
                     **found, "verdict": verdict})

    header = ("| launch log | model | config | requested | NET/IB channels | "
              "GDRDMA | NET/Socket channels | RDMA devices | verdict |")
    lines = ["# NCCL transport audit", "",
             f"Result directory: `{out_dir}`. One row per dual-node server launch.",
             "", header, "|---|---|---|---|--:|--:|--:|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['log']} | {row['label']} | {row['config']} | "
            f"NET/{row['requested']} | {row['ib']} | {row['gdr']} | "
            f"{row['socket']} | {len(row['devices'])} "
            f"({', '.join(row['devices']) or '-'}) | {row['verdict']} |")
    verdicts = [row["verdict"] for row in rows]
    device_counts = sorted({len(row["devices"]) for row in rows if row["devices"]})
    summary = (f"{len(rows)} dual-node launch(es): {verdicts.count('ok')} verified, "
               f"{verdicts.count('no evidence')} without NCCL channel lines, "
               f"{verdicts.count('WRONG TRANSPORT')} with the wrong transport, "
               f"{verdicts.count('no policy')} without a matching serving policy. "
               f"RDMA devices per launch: {device_counts or 'n/a'}.")
    lines += ["", summary]
    print(summary)
    for row in rows:
        if row["verdict"] != "ok":
            print(f"  {row['verdict']}: {row['log']}")
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text("\n".join(lines) + "\n")
        print(f"wrote {args.md}")
    failed = ("WRONG TRANSPORT" in verdicts
              or not args.allow_unverified and (not rows or any(v != "ok" for v in verdicts)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
