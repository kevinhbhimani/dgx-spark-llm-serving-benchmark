#!/usr/bin/env python3
"""Two-node NCCL all-reduce microbenchmark over the shared Ray cluster.

The nccl-tests path (scripts/interconnect_microbench.sh) needs mpirun with SSH
from head to worker, which this cluster does not have. This script runs inside
the serving container on the head node (see scripts/ray_allreduce_bench.sh),
pins one actor per Ray node, and times torch.distributed all-reduce across the
ConnectX link for sizes from 8 B to 1 GiB. The numbers feed the paper's
communication model for TP2 (per-layer all-reduce latency) and PP2 (per-stage
activation transfer bandwidth).

Run it while NO model is being served. Each task reserves its node's GPU through
Ray, and the shell wrapper also rejects a live vLLM process.

Prints one CSV row per size:  bytes,avg_s,min_s,algbw_GBps,busbw_GBps
(busbw equals algbw for all-reduce at world size 2: 2(n-1)/n = 1).

Usage (inside the container):
    python3 ray_allreduce_bench.py HEAD_IP WORKER_IP IFACE [PORT [MIN_BW [MAX_LATENCY]]]
"""
from __future__ import annotations
import math
import subprocess
import sys
import time

import ray
from ray.exceptions import GetTimeoutError


def _gpu_processes() -> list[str]:
    """Return local compute processes without creating a CUDA context."""
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {completed.stderr.strip()}")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _worker(rank, world, head_ip, iface, port, sizes, iters, warmup):
    import datetime
    import os

    os.environ["NCCL_SOCKET_IFNAME"] = iface
    os.environ["GLOO_SOCKET_IFNAME"] = iface
    os.environ.setdefault(
        "NCCL_IB_DISABLE", os.environ.get("NCCL_IB_DISABLE", "1")
    )
    import torch
    import torch.distributed as dist

    initialized = False
    try:
        dist.init_process_group(
            "nccl",
            init_method=f"tcp://{head_ip}:{port}",
            rank=rank,
            world_size=world,
            timeout=datetime.timedelta(minutes=10),
        )
        initialized = True
        dev = torch.device("cuda:0")
        results = []
        for size in sizes:
            n = max(1, size // 4)
            buf = torch.ones(n, dtype=torch.float32, device=dev)
            for _ in range(warmup):
                dist.all_reduce(buf)
            torch.cuda.synchronize(dev)
            dist.barrier()
            times = []
            for _ in range(iters):
                torch.cuda.synchronize(dev)
                t0 = time.perf_counter()
                dist.all_reduce(buf)
                torch.cuda.synchronize(dev)
                times.append(time.perf_counter() - t0)
            avg = sum(times) / len(times)
            results.append((size, avg, min(times)))
        return results
    finally:
        if initialized:
            dist.destroy_process_group()

def main() -> int:
    if not 4 <= len(sys.argv) <= 7:
        sys.stderr.write(
            "usage: ray_allreduce_bench.py HEAD_IP WORKER_IP IFACE "
            "[PORT [MIN_BW [MAX_LATENCY]]]\n"
        )
        return 2
    head_ip, worker_ip, iface = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        port = int(sys.argv[4]) if len(sys.argv) > 4 else 29517
        min_bw = float(sys.argv[5]) if len(sys.argv) > 5 else 5.0
        max_latency = float(sys.argv[6]) if len(sys.argv) > 6 else 0.005
    except ValueError as exc:
        sys.stderr.write(f"[microbench] invalid numeric argument: {exc}\n")
        return 2
    if not 1 <= port <= 65535:
        sys.stderr.write("[microbench] PORT must be in [1, 65535]\n")
        return 2
    if (not math.isfinite(min_bw) or min_bw <= 0
            or not math.isfinite(max_latency) or max_latency <= 0):
        sys.stderr.write("[microbench] thresholds must be finite and positive\n")
        return 2

    sizes = [1 << exponent for exponent in range(3, 31)]
    iters, warmup = 20, 5

    ray.init(address="auto", log_to_driver=True)
    alive = sorted(
        node["NodeManagerAddress"] for node in ray.nodes() if node.get("Alive")
    )
    expected = sorted([head_ip, worker_ip])
    if alive != expected:
        sys.stderr.write(
            f"[microbench] expected exactly Ray nodes {expected}, got {alive}\n"
        )
        return 1

    probes = []
    for ip in [head_ip, worker_ip]:
        probe = ray.remote(
            num_cpus=0, resources={f"node:{ip}": 0.001}
        )(_gpu_processes)
        probes.append(probe.remote())
    try:
        processes = ray.get(probes, timeout=60)
    except GetTimeoutError:
        for ref in probes:
            ray.cancel(ref, force=True)
        sys.stderr.write("[microbench] timed out checking both GPUs for idleness\n")
        return 1
    busy = [
        f"{ip}: {', '.join(lines)}"
        for ip, lines in zip([head_ip, worker_ip], processes)
        if lines
    ]
    if busy:
        sys.stderr.write(
            "[microbench] both GPUs must be idle; compute processes found: "
            + "; ".join(busy) + "\n"
        )
        return 1

    remotes = []
    for rank, ip in enumerate([head_ip, worker_ip]):
        fn = ray.remote(
            num_cpus=0, num_gpus=1, resources={f"node:{ip}": 0.001}
        )(_worker)
        remotes.append(
            fn.remote(rank, 2, head_ip, iface, port, sizes, iters, warmup)
        )
    try:
        all_results = ray.get(remotes, timeout=1800)
    except GetTimeoutError:
        for ref in remotes:
            ray.cancel(ref, force=True)
        sys.stderr.write("[microbench] all-reduce tasks timed out after 1800s\n")
        return 1
    if any([row[0] for row in rank_results] != sizes
           for rank_results in all_results):
        sys.stderr.write("[microbench] a rank returned an incomplete size sweep\n")
        return 1
    results = all_results[0]

    print("bytes,avg_s,min_s,algbw_GBps,busbw_GBps")
    rows = {}
    for size, avg, mn in results:
        bw = size / avg / 1e9
        rows[size] = (avg, mn, bw)
        print(f"{size},{avg:.6e},{mn:.6e},{bw:.3f},{bw:.3f}")

    latency = rows.get(8, (math.nan, math.nan, math.nan))[1]
    bandwidth = rows.get(1 << 30, (math.nan, math.nan, math.nan))[2]
    sys.stderr.write(
        f"[microbench] 8B min latency {latency * 1e6:.1f} us "
        f"(limit {max_latency * 1e6:.1f} us); "
        f"1GiB bus bandwidth {bandwidth:.3f} GB/s "
        f"(minimum {min_bw:.3f} GB/s)\n"
    )
    if (not math.isfinite(latency) or latency > max_latency
            or not math.isfinite(bandwidth) or bandwidth < min_bw):
        sys.stderr.write(
            "[microbench] interconnect acceptance thresholds were not met\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
