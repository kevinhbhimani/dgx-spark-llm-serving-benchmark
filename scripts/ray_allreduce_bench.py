#!/usr/bin/env python3
"""Two-node NCCL all-reduce microbenchmark over the shared Ray cluster.

It needs no SSH or MPI between the nodes. This script runs inside
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
import os
import subprocess
import sys
import time

import ray
from ray.exceptions import GetTimeoutError


def _transport_label(debug_path: str) -> str:
    """Require channel evidence: merely opening an IB plugin proves nothing."""
    try:
        with open(debug_path, encoding="utf-8", errors="replace") as handle:
            nccl_log = handle.read()
    except OSError:
        return "unknown"
    ib = "via NET/IB" in nccl_log
    socket = "via NET/Socket" in nccl_log
    return "mixed" if ib and socket else "IB" if ib else "Socket" if socket else "unknown"


def _validated_results(all_results, sizes, expected_transport):
    """Reject incomplete/invalid ranks and retain the slower rank per size."""
    if len(all_results) != 2:
        raise ValueError("expected exactly two rank results")
    labels = [label for label, _ in all_results]
    if labels != [expected_transport, expected_transport]:
        raise ValueError(
            f"expected {expected_transport} channels on both ranks, got {labels}")
    for _, rank_results in all_results:
        if [row[0] for row in rank_results] != sizes:
            raise ValueError("a rank returned an incomplete size sweep")
        for _, avg, minimum in rank_results:
            if not (math.isfinite(avg) and math.isfinite(minimum)
                    and 0 < minimum <= avg):
                raise ValueError("a rank returned invalid timings")
    return [
        (size, max(rank[1][index][1] for rank in all_results),
         max(rank[1][index][2] for rank in all_results))
        for index, size in enumerate(sizes)
    ]


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
    # Observe each Ray worker's existing policy. The driver environment alone
    # cannot change the containers on both nodes, so prove the actual data path.
    debug_path = f"/tmp/nccl_allreduce_rank{rank}_{os.getpid()}.log"
    os.environ["NCCL_DEBUG"] = "INFO"
    os.environ["NCCL_DEBUG_SUBSYS"] = "INIT,NET"
    os.environ["NCCL_DEBUG_FILE"] = debug_path
    try:
        os.remove(debug_path)
    except FileNotFoundError:
        pass
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
        transport = _transport_label(debug_path)
        # The wrapper saves stderr beside the CSV, preserving the channel and
        # device evidence that justified this gate for later paper auditing.
        try:
            with open(debug_path, encoding="utf-8", errors="replace") as handle:
                evidence = "".join(line for line in handle if "NET/" in line)
        except OSError:
            evidence = ""
        return transport, results, evidence
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
        min_bw = float(sys.argv[5]) if len(sys.argv) > 5 else 16.0
        max_latency = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0001
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
    expected_transport = "Socket" if os.environ.get("NCCL_IB_DISABLE", "0") == "1" else "IB"
    for rank, (label, _, evidence) in enumerate(all_results):
        sys.stderr.write(f"[microbench] rank {rank} observed NET/{label}\n{evidence}")
    try:
        results = _validated_results(
            [(label, rows) for label, rows, _ in all_results], sizes,
            expected_transport,
        )
    except ValueError as exc:
        sys.stderr.write(f"[microbench] {exc}\n")
        return 1

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
